"""The learning loop is structural: a losing close blocks the next order until a lesson with cause and change exists, every order names a registered active strategy,
a strategy past its machine kill rule is retired, and a coordinator can halt a lane book."""
import json
import sqlite3

import pytest

from conftest import NOW, put_perp
from test_router_tools import FakeTools, PLAN, call
from sniper_office.markets import books as B, common as C, strategies as ST, tools as T

POL = B.policy()
ORDER = dict(coin='BTC', side='long', margin_usd=100, leverage=2, tp_pct=5, sl_pct=3, strategy='perps-level-break', **PLAN)
SPEC = dict(hypothesis='h', entry_rule='e', exit_rule='x', kill_criterion='mean loss')


def place(led, feed, now=NOW, role='r', pol=POL, **kw):
    return B.place(led, feed, 'perps-r', 'perps', {**ORDER, **kw}, now, pol, None, role)


def lose(led, feed, now=NOW, **kw):
    """Open at 100, stop out at 97: a closed loss."""
    put_perp(feed, mid=100.0, at=now)
    r = place(led, feed, now, **kw)
    assert r['state'] == 'filled', r
    put_perp(feed, mid=97.0, at=now + 5)
    assert B.engine_tick(led, feed, now + 5, POL) == {'stop_loss': 1}
    return r['pos_id']


def review(led, pos_id, role='r'):
    led.execute('INSERT INTO bk_lessons(role,by_role,at,pos_id,lesson,cause,change) VALUES(?,?,0,?,?,?,?)', (role, role, pos_id, 'l', 'too early', 'wait for the retest'))


def test_losing_close_blocks_next_order_until_reviewed(dbs):
    feed, led, _ = dbs
    pid = lose(led, feed)
    r = place(led, feed, NOW + 10)
    assert r['state'] == 'refused' and r['reason'] == 'review_pending' and 'lesson_add' in r['next']
    u = r['unreviewed'][0]
    assert u['pos_id'] == pid and u['instrument'] == 'hyperliquid:BTC' and u['side'] == 'long' and u['realized'] < 0 and u['exit_reason'] == 'stop_loss' and u['thesis'] == PLAN['thesis'] and u['disproof'] == PLAN['disproof']
    assert u['strategy_id'] == 'perps-level-break' and u['entry_price'] and u['exit_price']
    review(led, pid, role='someone_else')                       # another seat's lesson does not lift it
    assert place(led, feed, NOW + 10)['reason'] == 'review_pending'
    led.execute("INSERT INTO bk_lessons(role,by_role,at,pos_id,lesson) VALUES('r','r',0,?,'no cause or change')", (pid,))     # a bare lesson row does not either
    assert place(led, feed, NOW + 10)['reason'] == 'review_pending'
    review(led, pid)
    put_perp(feed, mid=100.0, at=NOW + 10)
    assert place(led, feed, NOW + 10)['state'] == 'filled'


def test_review_gate_can_be_switched_off_and_a_halt_still_wins(dbs):
    feed, led, _ = dbs
    lose(led, feed)
    put_perp(feed, mid=100.0, at=NOW + 10)
    assert place(led, feed, NOW + 10, pol=B.policy(dict(review_gate=False)))['state'] == 'filled'
    led.execute('UPDATE bk_books SET halted=1')
    assert place(led, feed, NOW + 10)['reason'] == 'book_halted'


def test_lesson_add_needs_cause_and_change_on_a_closed_position(tmp_path, dbs):
    feed, led, _ = dbs
    pid = lose(led, feed)
    t = FakeTools(tmp_path, role='r')
    assert call('lesson_add', t, pos_id=pid, lesson='x')[0]['reason'] == 'cause_and_change_required'
    assert call('lesson_add', t, pos_id=pid, cause='early', change=' ')[0]['reason'] == 'cause_and_change_required'
    assert call('lesson_add', t, pos_id='nope', cause='c', change='d')[0]['reason'] == 'pos_unknown_or_open'
    assert call('lesson_add', t, lesson='free note')[0]['state'] == 'recorded'            # no pos_id: a free note, as before
    assert call('lesson_add', t, pos_id=pid, cause='early', change='wait for the retest')[0]['state'] == 'recorded'
    row = led.execute('SELECT * FROM bk_lessons WHERE pos_id=?', (pid,)).fetchone()
    assert row['cause'] == 'early' and row['change'] == 'wait for the retest' and row['by_role'] == 'r'
    put_perp(feed, mid=100.0, at=NOW + 10)
    assert place(led, feed, NOW + 10)['state'] == 'filled'


def test_order_must_name_a_registered_active_strategy(dbs):
    feed, led, _ = dbs
    put_perp(feed)
    for bad in (None, '', 'none fits', 'no-such', 'pm-news-scalp'):                      # the last is another lane's strategy
        r = place(led, feed, strategy=bad)
        assert r['reason'] == 'strategy_unregistered' and 'strategy_register' in r['rule']
    sid = ST.register(led, 'r', 'perps', SPEC, NOW)['strategy_id']
    assert place(led, feed, strategy=sid)['state'] == 'filled'
    led.execute("UPDATE bk_strategies SET status='retired'")
    assert place(led, feed, strategy=sid)['reason'] == 'strategy_retired'


def test_register_takes_min_n_and_machine_kill_fields(dbs):
    feed, led, _ = dbs
    assert ST.register(led, 'r', 'perps', dict(SPEC, min_n=0), NOW)['reason'] == 'min_n'
    assert ST.register(led, 'r', 'perps', dict(SPEC, kill_winrate_lt=2), NOW)['reason'] == 'kill_winrate_lt'
    r = ST.register(led, 'r', 'perps', dict(SPEC, min_n=3, kill_mean_lt=-1.5), NOW)
    assert r['min_n'] == 3
    assert ST.register(led, 'r', 'perps', dict(SPEC, hypothesis='h2'), NOW)['min_n'] == 10                # default


@pytest.mark.parametrize('field,value', [('kill_mean_lt', -1.0), ('kill_winrate_lt', 0.5)])
def test_strategy_retires_after_min_n_under_its_kill_rule(dbs, field, value):
    feed, led, _ = dbs
    sid = ST.register(led, 'r', 'perps', dict(SPEC, min_n=2, **{field: value}), NOW)['strategy_id']
    for i in range(2):
        review_all = led.execute("SELECT pos_id FROM bk_positions WHERE status='closed'").fetchall()
        for (p,) in review_all:
            review(led, p)
        lose(led, feed, NOW + 100 * i, strategy=sid)
        assert led.execute('SELECT status FROM bk_strategies').fetchone()[0] == ('retired' if i == 1 else 'testing')      # n < min_n: still testing
    st = next(s for s in ST.results(led, 'perps')['strategies'] if s['strategy_id'] == sid)
    assert st['status'] == 'retired' and st['retired_stats']['n'] == 2 and st['retired_stats']['mean_realized'] < 0 and st['min_n'] == 2 and st[field] == value
    assert led.execute("SELECT COUNT(*) FROM bk_events WHERE kind='strategy_retired'").fetchone()[0] == 1
    for (p,) in led.execute("SELECT pos_id FROM bk_positions").fetchall():
        review(led, p)
    put_perp(feed, mid=100.0, at=NOW + 500)
    assert place(led, feed, NOW + 500, strategy=sid)['reason'] == 'strategy_retired'


def test_strategy_without_a_kill_field_or_above_it_is_not_retired(dbs):
    feed, led, _ = dbs
    a = ST.register(led, 'r', 'perps', dict(SPEC, min_n=1), NOW)['strategy_id']                   # prose kill criterion only: never auto-retired
    b = ST.register(led, 'r', 'perps', dict(SPEC, hypothesis='h2', min_n=1, kill_mean_lt=-1000), NOW)['strategy_id']
    review(led, lose(led, feed, NOW, strategy=a))
    review(led, lose(led, feed, NOW + 100, strategy=b))
    assert [r[0] for r in led.execute('SELECT status FROM bk_strategies')] == ['testing', 'testing']


def test_lane_halt_tool_halts_resumes_and_records_events(tmp_path, dbs):
    feed, led, _ = dbs
    put_perp(feed)
    t, seat = FakeTools(tmp_path, role='coordinator'), FakeTools(tmp_path, role='r')
    assert call('perp_order', seat, **ORDER)[0]['state'] == 'filled'
    bid = 'perps-r'
    assert {x[0] for x in T.TOOLS if x[1] == 'lane_halt'} == {'office'}
    assert call('lane_halt', t, book_id=bid, action='pause', reason='x')[0]['reason'] == 'action_and_reason_required'
    assert call('lane_halt', t, book_id=bid, action='halt', reason=' ')[0]['reason'] == 'action_and_reason_required'
    assert call('lane_halt', t, book_id='nope', action='halt', reason='x')[0]['reason'] == 'book_unknown'
    assert call('lane_halt', t, book_id=bid, action='halt', reason='drawdown')[0]['state'] == 'halt'
    d, err = call('perp_order', seat, **ORDER)
    assert err and d['reason'] == 'book_halted'
    assert call('lane_halt', t, book_id=bid, action='resume', reason='ok')[0]['state'] == 'resume'
    assert call('perp_order', seat, **ORDER)[0].get('reason') != 'book_halted'
    ev = led.execute("SELECT kind,detail_json FROM bk_events WHERE kind IN ('halt','resume') ORDER BY id").fetchall()
    assert [e[0] for e in ev] == ['halt', 'resume'] and json.loads(ev[0][1])['reason'] == 'drawdown'


def test_migration_adds_columns_to_an_old_ledger(tmp_path):
    old = sqlite3.connect(tmp_path / 'old.db')
    old.executescript("CREATE TABLE bk_strategies(strategy_id TEXT PRIMARY KEY,lane TEXT NOT NULL,owner_role TEXT NOT NULL,hypothesis TEXT,entry_rule TEXT,exit_rule TEXT,kill_criterion TEXT,expected_triggers_per_day REAL,status TEXT NOT NULL,created_at REAL NOT NULL);"
                      "CREATE TABLE bk_lessons(id INTEGER PRIMARY KEY AUTOINCREMENT,role TEXT NOT NULL,by_role TEXT NOT NULL,at REAL NOT NULL,pos_id TEXT,lesson TEXT NOT NULL);")
    old.close()
    led = C.connect(tmp_path / 'old.db', C.LEDGER_SCHEMA)
    assert {'min_n', 'kill_mean_lt', 'kill_winrate_lt', 'retired_at', 'stats_json'} <= {r[1] for r in led.execute('PRAGMA table_info(bk_strategies)')}
    assert {'cause', 'change'} <= {r[1] for r in led.execute('PRAGMA table_info(bk_lessons)')}
