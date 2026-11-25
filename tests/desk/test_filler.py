"""The desk fill engine: fills once at a recorded price, never on an unknown price, rails exit, restarts are idempotent, book_view values open positions."""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_desk_plugin import NOW, call, seat, write_json  # noqa: E402

from sniper_office.desk import filler as F, paper as P  # noqa: E402

MINT, POOL, ACTOR = 'MintAAAA1111', 'PoolBBBB2222', 'ag_judge'
POLICY = dict(clip_usd=25., max_concurrent=3, daily_loss_usd=100., sl_pct=30., tp_pct=100., trail_pct=20., catastrophe_pct=60., max_hold_s=3600., entry_timeout_s=60., slippage_bps=50., fee_bps=100.)


class Market:
    def __init__(self):
        self.price, self.liq, self.status, self.calls = 1.0, 100000., 200, 0

    def __call__(self, url):
        self.calls += 1
        if self.status != 200:
            return self.status, None
        return 200, dict(pairs=[dict(chainId='solana', pairAddress=POOL, dexId='pumpswap', baseToken=dict(address=MINT), priceUsd=str(self.price), liquidity=dict(usd=self.liq))])


class Rig:
    def __init__(self, tmp):
        self.tmp, self.now, self.market = tmp, NOW, Market()
        conn = sqlite3.connect(tmp / 'o.db')
        conn.executescript(P.SCHEMA + 'CREATE TABLE IF NOT EXISTS x(a);')
        conn.commit(); conn.close()
        self.cfg = dict(office_db=str(tmp / 'o.db'), ledger_db=str(tmp / 'ledger.db'), book_policy=write_json(tmp / 'pol.json', dict(policy=POLICY)), clock_file=write_json(tmp / 'clock.json', dict(offset_s=0., at=NOW)),
                        books=[dict(book_id='ep', actor_ids=[ACTOR], capital_usd=1000.)], mark_every_s=0.001)
        self.f = self.start()

    def start(self):
        return F.Filler(self.cfg, clock=lambda: self.now, fetch=self.market)

    def request(self, kind='order', rid='req1', order_id=None, rails=None, at=None, **detail):
        conn = sqlite3.connect(self.tmp / 'o.db')
        conn.execute('INSERT INTO desk_paper_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', (rid, kind, 'ep', ACTOR, 1, at or self.now, 't1', 'solana', MINT, POOL, order_id, 'because', json.dumps(dict(rails=rails or {}, **detail))))
        conn.commit(); conn.close()

    def tick(self, dt=5.):
        self.now += dt
        return self.f.tick()

    def q(self, sql, *a):
        with sqlite3.connect(self.cfg['ledger_db']) as c:
            return c.execute(sql, a).fetchall()


def test_entry_fills_once_at_the_fetched_price_with_slippage_fee_and_proof(tmp_path):
    r = Rig(tmp_path)
    r.request()
    assert r.tick()['results'][0] == 'filled'
    r.tick(); r.tick()
    (order,) = r.q("SELECT order_id,state,spend_usd,costs_usd,qty,entry_price_usd,fill_proof FROM orders")
    assert order[:4] == ('req1', 'open', 25., 0.25)
    slip = 50 + 2 * 25 / 100000 * 1e4   # base 50 bps + constant-product impact 5 bps
    assert abs(order[5] - 1.0 * (1 + slip / 1e4)) < 1e-12 and abs(order[4] - 25 / order[5]) < 1e-9
    proof = json.loads(order[6])
    assert proof['source'] == F.SOURCE and proof['price_usd'] == 1.0 and proof['fetched_at'] and proof['fee_bps'] == 100. and proof['slippage_bps'] == slip
    assert r.q('SELECT COUNT(*) FROM cashflows')[0][0] == 1 and r.q("SELECT outcome FROM request_outcomes") == [('filled',)]


def test_unknown_price_never_fills_and_the_reason_is_recorded_then_censored(tmp_path):
    r = Rig(tmp_path)
    r.market.status = 503
    r.request()
    assert r.tick()['results'] == ['deferred:provider_unavailable']
    assert r.q('SELECT COUNT(*) FROM orders')[0][0] == 0 and r.q('SELECT reason FROM fill_attempts') == [('provider_unavailable',)]
    r.market.status = 200
    r.market.liq = None   # price known, liquidity not: still no fill, nothing assumed
    assert r.tick()['results'] == ['deferred:liquidity_unknown']
    r.tick(120.)   # past entry_timeout_s: the request is closed as censored with the last reason, never filled late
    assert r.q('SELECT COUNT(*) FROM orders')[0][0] == 0 and r.q('SELECT outcome,reason FROM request_outcomes') == [('censored', 'liquidity_unknown')]


def test_unknown_clock_does_not_tick(tmp_path):
    r = Rig(tmp_path)
    r.request()
    r.now += 7200   # the clock sample is now too old
    assert r.f.tick() == dict(state='clock_unknown') and r.market.calls == 0


def rails_exit(tmp_path, rails, path, expect):
    r = Rig(tmp_path)
    r.request(rails=rails)
    r.tick()
    for price in path:
        r.market.price = price
        r.tick()
    (row,) = r.q("SELECT state,exit_reason,exit_fill_kind,exit_usd,costs_usd,exit_proof FROM orders")
    assert row[:3] == ('closed', expect, 'priced_at_trigger_fetch') and json.loads(row[5])['source'] == F.SOURCE
    return r, row


def test_stop_rail_exits_and_books_the_loss(tmp_path):
    r, row = rails_exit(tmp_path, {}, [0.9, 0.6], 'stop')   # -40% on net value breaches sl 30
    net = r.q('SELECT SUM(usd) FROM cashflows')[0][0]
    assert 1000 + net < 1000 - 25 * .3 and row[3] < 25


def test_tp_and_trail_rails(tmp_path):
    rails_exit(tmp_path, {}, [1.5, 2.2], 'tp')
    (tmp_path / 'b').mkdir()
    rails_exit(tmp_path / 'b', {'tp_pct': 150., 'trail_pct': 15.}, [1.4, 1.15], 'trail')


def test_exit_request_closes_at_the_next_priced_tick_once(tmp_path):
    r = Rig(tmp_path)
    r.request()
    r.tick()
    r.request('exit', rid='x1', order_id='req1')
    r.tick(); r.tick(); r.tick()
    assert r.q('SELECT state,exit_reason FROM orders') == [('closed', 'requested:because')]
    assert r.q("SELECT outcome FROM request_outcomes WHERE request_id='x1'") == [('accepted',)] and r.q("SELECT COUNT(*) FROM cashflows WHERE kind='exit'")[0][0] == 1


def test_restart_does_not_fill_or_exit_twice(tmp_path):
    r = Rig(tmp_path)
    r.request()
    r.tick()
    r.f = r.start()   # restart with the order open
    r.tick(); r.tick()
    assert r.q('SELECT COUNT(*) FROM orders')[0][0] == 1 and r.q('SELECT COUNT(*) FROM cashflows')[0][0] == 1 and r.q('SELECT SUM(usd) FROM capital_flows')[0][0] == 1000.
    r.market.price = 0.5
    r.tick()
    r.f = r.start()   # restart after the exit
    r.request('exit', rid='x1', order_id='req1')
    for _ in range(3):
        r.f = r.start(); r.tick(); r.tick()
    assert r.q("SELECT state FROM orders") == [('closed',)] and r.q('SELECT COUNT(*) FROM cashflows')[0][0] == 2
    assert r.q("SELECT outcome,reason FROM request_outcomes WHERE request_id='x1'") == [('refused', 'order_not_open')]


def test_book_view_values_an_open_position_and_goes_unknown_when_the_mark_is_stale(tmp_path, monkeypatch):
    r = Rig(tmp_path)
    write_json(tmp_path / 'clock.json', dict(offset_s=0., at=NOW - 100))
    r.now = NOW - 20   # the seat's clock is NOW; the filler's fill and mark are 20 s old
    r.request()
    r.tick(1.)
    t = seat(tmp_path, monkeypatch, cfg=dict(ledger_db=r.cfg['ledger_db'], book_policy=r.cfg['book_policy'], ledger_epoch='ep'))
    err, view = call(t, 'book_view')
    cash = 1000 - 25 - 0.25
    mark = r.q('SELECT value_usd FROM marks ORDER BY id DESC LIMIT 1')[0][0]
    assert 24 < mark < 25   # modelled net exit value after exit slippage and fee: below the 25 spent
    assert not err and len(view['positions']) == 1 and view['equity']['state'] == 'known' and abs(view['equity']['usd'] - (cash + mark)) < 1e-9
    assert abs(view['equity']['cash_usd'] - cash) < 1e-9 and view['equity']['unpriced_count'] == 0
    with sqlite3.connect(r.cfg['ledger_db']) as c:   # the only mark is now older than mark_max_age_s
        c.execute('UPDATE marks SET at=at-1000')
    _, view = call(t, 'book_view')
    assert view['equity']['state'] == 'unknown' and view['equity']['reason'] == 'position_unpriced' and view['equity']['usd'] is None and view['equity']['cash_usd'] == cash


def test_max_entry_price_defers_then_fills_when_price_comes_back(tmp_path):
    r = Rig(tmp_path)
    r.request(max_entry_price_usd=1.0)   # fill price is 1.0 plus slippage: above the cap
    assert r.tick()['results'] == ['deferred:above_max_entry_price']
    r.market.price = 0.9
    assert r.tick()['results'][0] == 'filled'
    assert json.loads(r.q('SELECT fill_proof FROM orders')[0][0])['max_entry_price_usd'] == 1.0
