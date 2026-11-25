"""Desk wake sources: ticket (rate limited, filter aware, once), close, position alert. Once-only state survives a restart."""
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_desk_plugin import NOW, desk, ticket, write_json  # noqa: E402

from sniper_office.desk import filler as FL, wakes as WK  # noqa: E402


class FakeSeat:
    def __init__(self, tmp, role='judge', now=NOW, **cfg):
        self.office = sqlite3.connect(tmp / 'o.db', isolation_level=None)
        self.actor, self.now = 'ag_' + role, now
        self.clock = lambda: self.now
        self.cfg = dict(role=role, epoch='ep', ticket_wakes=True, clock_file=write_json(tmp / 'clock.json', dict(offset_s=0., at=NOW)), **cfg)


def reopen(old):
    """A restart: a new connection to the same office db."""
    new = FakeSeat.__new__(FakeSeat)
    new.__dict__.update(old.__dict__, office=sqlite3.connect(old.office.execute('PRAGMA database_list').fetchone()[2], isolation_level=None))
    return new


def book(tmp, orders=(), marks=()):
    path = tmp / 'l.db'
    c = sqlite3.connect(path)
    c.executescript(FL.SCHEMA)
    for o in orders:
        c.execute("INSERT INTO orders(order_id,book_id,epoch_id,mint,pool,state,requested_at,filled_at,closed_at,spend_usd,exit_usd,exit_reason,qty,entry_price_usd,peak_value_usd,policy_json,exit_proof) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", o)
    for m in marks:
        c.execute('INSERT INTO marks(order_id,at,price_usd,value_usd,source) VALUES(?,?,?,?,?)', m)
    c.execute("INSERT INTO cashflows(order_id,kind,at,usd) VALUES('c1','entry',?,-25.25),('c1','exit',?,27.5)", (NOW - 900, NOW - 100))
    c.commit(); c.close()
    return str(path)


POL = json.dumps(dict(trail_pct=20., sl_pct=30.))


def test_a_new_pass_wakes_once_per_seat_names_the_ticket_and_survives_a_restart(tmp_path):
    s = FakeSeat(tmp_path, desk_db=desk(tmp_path / 'd.db', [ticket('t1', 20)]))
    got = WK.source(s, False)
    assert got['type'] == 'new_ticket' and got['urgent'] is False and 'ticket_id=t1' in got['prompt'] and 'New feed ticket t1' in got['prompt']
    assert WK.source(s, False) is None and WK.source(reopen(s), False) is None
    other = FakeSeat(tmp_path, role='scout', desk_db=s.cfg['desk_db'])
    other.office = sqlite3.connect(tmp_path / 'o.db', isolation_level=None)
    assert WK.source(other, False)['type'] == 'new_ticket'   # once per ticket PER SEAT


def test_ticket_wakes_are_rate_limited_batched_and_honour_the_off_switch_and_no_feed_roles(tmp_path):
    path = desk(tmp_path / 'd.db', [ticket('t1', 20), ticket('t2', 10)])
    s = FakeSeat(tmp_path, desk_db=path)
    got = WK.source(s, False)
    assert 'ticket_id=t2' in got['prompt'] and '1 other new ticket' in got['prompt']   # newest named, the rest batched, not left as a backlog
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO desk_tickets(ticket_id,network_id,mint,pool,stage,verdict,reason,ts,inputs_json) VALUES('t3','solana','m3','p3','hard_filter','PASS','pass',?,'{}')", (time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime(NOW - 5)),))
    assert WK.source(s, False) is None                # inside ticket_wake_min_s (default 300)
    s.now = NOW + 301
    s.cfg['clock_file'] = write_json(tmp_path / 'clock.json', dict(offset_s=0., at=NOW + 301))
    assert 'ticket_id=t3' in WK.source(s, False)['prompt']
    for role, flag in (('judge', False), ('researcher', True), ('narrative', True)):
        x = FakeSeat(tmp_path, role=role, desk_db=desk(tmp_path / (role + '.db'), [ticket('z' + role, 5)]))
        x.cfg['ticket_wakes'] = flag
        x.office = sqlite3.connect(tmp_path / (role + 'o.db'), isolation_level=None)
        assert WK.source(x, False) is None, role


def test_a_ticket_the_seats_own_filter_hides_does_not_wake(tmp_path):
    s = FakeSeat(tmp_path, desk_db=desk(tmp_path / 'd.db', [ticket('whale', 5, top10=60.)]))
    WK.source(s, False)   # creates the tables
    s.office.execute("DELETE FROM desk_wakes")
    s.office.execute("INSERT INTO desk_feed_filters(epoch_id,actor_id,at,rules_json,reason) VALUES('ep',?,0,?,'r')", (s.actor, json.dumps([dict(path='holders.top10_pct', op='gt', value=50)])))
    assert WK.source(s, False) is None


def test_no_ticket_wake_from_a_stale_collector_or_an_untrusted_clock(tmp_path):
    s = FakeSeat(tmp_path, desk_db=desk(tmp_path / 'd.db', [ticket('old', 5000)]))
    assert WK.source(s, False) is None
    s2 = FakeSeat(tmp_path, desk_db=desk(tmp_path / 'd2.db', [ticket('x', 5)]))
    s2.cfg['clock_file'] = str(tmp_path / 'missing.json')
    assert WK.source(s2, False) is None


def test_a_closed_order_wakes_its_owner_once_with_plan_prices_result_and_reason(tmp_path):
    ledger = book(tmp_path, [('c1', 'ep', 'ep', 'MINTX', 'p', 'closed', NOW - 950, NOW - 900, NOW - 100, 25., 27.5, 'trail', 100., 0.25, 30., POL, json.dumps(dict(price_usd=0.31)))])
    s = FakeSeat(tmp_path, ledger_db=ledger)
    WK.source(s, False)   # tables
    s.office.execute("INSERT INTO desk_paper_requests VALUES('c1','order','ep','ag_judge',1,0,'tk','solana','MINTX','p',NULL,'r',?)", (json.dumps(dict(plan=dict(thesis='T', disproof='D', exit_plan='E'))),))
    s.office.execute('DELETE FROM desk_wakes')
    got = WK.source(s, False)
    assert got['type'] == 'order_closed' and got['urgent'] is False
    for needle in ('c1', 'Exit reason: trail', 'Entry price USD 0.25', 'exit price USD 0.31', 'Net result USD 2.25', '"thesis": "T"', '"disproof": "D"', '"exit_plan": "E"'):
        assert needle in got['prompt'], needle
    assert WK.source(s, False) is None and WK.source(reopen(s), False) is None
    other = FakeSeat(tmp_path, role='judge_gpt', ledger_db=ledger, ledger_epoch='office-v2-explore-gpt-1')
    other.office = sqlite3.connect(tmp_path / 'o.db', isolation_level=None)
    assert WK.source(other, False) is None             # not that book's owner
    scout = FakeSeat(tmp_path, role='scout', ledger_db=ledger)
    scout.office = sqlite3.connect(tmp_path / 'o.db', isolation_level=None)
    assert WK.source(scout, False) is None             # only order roles


def open_order(spend=25., peak=25.):
    return ('o1', 'ep', 'ep', 'MINTY', 'p', 'open', NOW - 900, NOW - 800, None, spend, None, None, 100., 0.25, peak, POL, None)


def test_position_alerts_fire_once_per_trigger_only_on_a_fresh_mark_and_only_as_urgent(tmp_path):
    ledger = book(tmp_path, [open_order()], [('o1', NOW - 30, 0.2, 20., 'x')])    # -20%: loss trigger
    s = FakeSeat(tmp_path, ledger_db=ledger)
    assert WK.source(s, False) is None                                    # not a non-urgent reason
    got = WK.source(s, True)
    assert got['type'] == 'position_alert' and got['urgent'] is True and 'alert loss' in got['prompt'] and 'o1' in got['prompt'] and '-20.0%' in got['prompt']
    assert WK.source(s, True) is None and WK.source(reopen(s), True) is None
    c = sqlite3.connect(ledger)
    c.execute('INSERT INTO marks(order_id,at,price_usd,value_usd,source) VALUES(?,?,?,?,?)', ('o1', NOW - 5, 0.4, 40., 'x'))   # +60%: a second trigger fires on its own
    c.execute('UPDATE orders SET peak_value_usd=31 WHERE order_id=?', ('o1',))   # peak +24%: trail armed
    c.commit(); c.close()
    assert 'alert gain' in WK.source(s, True)['prompt']
    assert 'alert trail_armed' in WK.source(s, True)['prompt'] and WK.source(s, True) is None


def test_a_stale_mark_or_a_quiet_position_raises_no_alert(tmp_path):
    s = FakeSeat(tmp_path, ledger_db=book(tmp_path, [open_order()], [('o1', NOW - 4000, 0.1, 10., 'x')]))
    assert WK.source(s, True) is None
    (tmp_path / 'q').mkdir()
    q = FakeSeat(tmp_path / 'q', ledger_db=book(tmp_path / 'q', [open_order()], [('o1', NOW - 5, 0.25, 26., 'x')]))
    assert WK.source(q, True) is None


def test_registration_adds_one_wake_source(tmp_path):
    from sniper_office import plugins as P
    assert P.load(dict(plugins=['sniper_office.desk'])).wake_sources == [WK.source]
