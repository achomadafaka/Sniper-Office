"""Book math: Polymarket fill, fee, exit and settlement; perps fee, liquidation price, funding, rails, max hold; stocks session rules; unknown stays unknown."""
import json

import pytest

from conftest import COND, NO, NOW, YES, put_market, put_perp, put_quote, put_stock
from sniper_office.markets import books as B, common as C

PLAN = dict(thesis='the data says so', disproof='the data reverses', exit_plan='sell at target or stop')
POL = B.policy()


def pm_order(led, feed, **kw):
    args = dict(condition_id=COND, side='YES', usd=100.0, tp_price=0.7, sl_price=0.3, **PLAN)
    return B.place(led, feed, 'pm-x', 'polymarket', {**args, **kw}, NOW, POL)


def test_pm_fee_formula_and_fill_at_ask(dbs):
    feed, led, _ = dbs
    put_market(feed); put_quote(feed, YES, 0.48, 0.50); put_quote(feed, NO, 0.50, 0.52)
    assert B.pm_fee(0.05, 1.0, 0.5) == pytest.approx(0.0125)
    r = pm_order(led, feed)
    assert r['state'] == 'filled'
    p = led.execute('SELECT * FROM bk_positions').fetchone()
    assert p['entry_price'] == 0.50 and p['qty'] == pytest.approx(195.12)      # floor(100 / (0.5 + 0.0125), 2 d.p.)
    assert p['entry_fee'] == pytest.approx(195.12 * 0.0125)
    assert led.execute('SELECT cash FROM bk_books').fetchone()[0] == pytest.approx(1000 - 195.12 * 0.5 - 195.12 * 0.0125)
    assert json.loads(p['proof_json'])['source'] == 'clob:book' and json.loads(p['proof_json'])['quote_at'] == NOW


def test_pm_buys_no_at_the_no_ask(dbs):
    feed, led, _ = dbs
    put_market(feed); put_quote(feed, YES, 0.48, 0.50); put_quote(feed, NO, 0.50, 0.52)
    assert pm_order(led, feed, side='NO', tp_price=0.8, sl_price=0.3)['state'] == 'filled'
    assert led.execute('SELECT instrument,entry_price FROM bk_positions').fetchone()[:] == (NO, 0.52)


@pytest.mark.parametrize('kw,reason', [
    (dict(thesis=''), 'plan_missing'), (dict(side='MAYBE'), 'side_must_be_YES_or_NO'), (dict(tp_price=0.4), 'order_fields'), (dict(sl_price=0.6), 'order_fields'),
    (dict(condition_id='0xnope'), 'market_unknown'), (dict(usd=0), 'order_fields'), (dict(usd=300.0), 'margin_above_cap'), (dict(strategy='no-such'), 'strategy_unknown')])
def test_pm_refusals(dbs, kw, reason):
    feed, led, _ = dbs
    put_market(feed); put_quote(feed, YES, 0.48, 0.50, ask_size=5000.)
    assert pm_order(led, feed, **kw)['reason'] == reason
    assert led.execute('SELECT COUNT(*) FROM bk_positions').fetchone()[0] == 0
    assert led.execute('SELECT COUNT(*) FROM bk_books').fetchone()[0] == 0        # a refusal leaves no book behind


def test_pm_stale_quote_wide_spread_extreme_price_and_depth(dbs):
    feed, led, _ = dbs
    put_market(feed)
    assert pm_order(led, feed)['reason'] == 'quote_unknown_or_stale'              # no quote at all: unknown, not a guess
    put_quote(feed, YES, 0.48, 0.50, at=NOW - 500)
    assert pm_order(led, feed)['reason'] == 'quote_unknown_or_stale'
    put_quote(feed, YES, 0.40, 0.50)
    assert pm_order(led, feed)['reason'] == 'price_or_spread_outside_rails'
    put_quote(feed, YES, 0.01, 0.02)
    assert pm_order(led, feed, sl_price=0.005, tp_price=0.5)['reason'] == 'price_or_spread_outside_rails'
    put_quote(feed, YES, 0.49, 0.50, ask_size=10.0)
    assert pm_order(led, feed)['reason'] == 'insufficient_depth_at_best_ask'


def test_pm_exit_at_bid_with_fee_and_realized(dbs):
    feed, led, _ = dbs
    put_market(feed); put_quote(feed, YES, 0.48, 0.50)
    pid = pm_order(led, feed)['pos_id']
    put_quote(feed, YES, 0.60, 0.62, at=NOW + 10)
    v = B.book_view(led, feed, 'pm-x', NOW + 10, POL)
    live = v['open'][0]['live']
    exit_fee = 195.12 * B.pm_fee(0.05, 1.0, 0.60)
    assert live['unrealized'] == pytest.approx(195.12 * 0.60 - exit_fee - 195.12 * 0.5 - 195.12 * 0.0125)
    r = B.manual_exit(led, feed, pid, 'pm-x', 'target', NOW + 10, POL)
    assert r['state'] == 'closed' and r['realized'] == pytest.approx(live['unrealized'])
    v = B.book_view(led, feed, 'pm-x', NOW + 10, POL)
    assert v['open'] == [] and v['equity'] == pytest.approx(1000 + r['realized']) and v['wins'] == 1
    assert B.manual_exit(led, feed, pid, 'pm-x', 'again', NOW + 11, POL)['reason'] == 'already_closed'


@pytest.mark.parametrize('resolved,side,payout', [(1.0, 'YES', 1.0), (0.0, 'YES', 0.0), (0.0, 'NO', 1.0)])
def test_pm_settles_at_resolution_without_fee(dbs, resolved, side, payout):
    feed, led, _ = dbs
    put_market(feed); put_quote(feed, YES, 0.48, 0.50); put_quote(feed, NO, 0.50, 0.52)
    ask = 0.50 if side == 'YES' else 0.52
    pid = pm_order(led, feed, side=side, tp_price=0.99 if side == 'YES' else 0.99, sl_price=0.01)['pos_id']
    p = led.execute('SELECT * FROM bk_positions').fetchone()
    put_market(feed, closed=1, resolved=resolved, accepting=0)
    assert B.engine_tick(led, feed, NOW + 100, POL) == {'resolved': 1}
    p = led.execute('SELECT * FROM bk_positions WHERE pos_id=?', (pid,)).fetchone()
    assert p['status'] == 'closed' and p['exit_reason'] == 'resolved' and p['exit_fee'] == 0.0
    assert p['realized'] == pytest.approx(p['qty'] * payout - p['qty'] * ask - p['entry_fee'])


def test_pm_closed_but_unresolved_market_stays_open(dbs):
    feed, led, _ = dbs
    put_market(feed); put_quote(feed, YES, 0.48, 0.50)
    pm_order(led, feed)
    put_market(feed, closed=1, resolved=None, accepting=0)           # closed, price not 0 or 1 yet: unknown
    assert B.engine_tick(led, feed, NOW + 100, POL) == {}
    assert led.execute("SELECT status FROM bk_positions").fetchone()[0] == 'open'


def test_pm_rails_take_profit_and_stop_loss(dbs):
    feed, led, _ = dbs
    put_market(feed); put_quote(feed, YES, 0.48, 0.50)
    pm_order(led, feed)
    put_quote(feed, YES, 0.71, 0.72, at=NOW + 60)
    assert B.engine_tick(led, feed, NOW + 60, POL) == {'take_profit': 1}
    put_quote(feed, YES, 0.48, 0.50, at=NOW + 100)
    pm_order(led, feed)     # at NOW; re-open after the first closed
    put_quote(feed, YES, 0.28, 0.30, at=NOW + 200)
    assert B.engine_tick(led, feed, NOW + 200, POL) == {'stop_loss': 1}


def test_unpriced_position_stays_open_and_equity_is_unknown(dbs):
    feed, led, _ = dbs
    put_market(feed); put_quote(feed, YES, 0.48, 0.50)
    pm_order(led, feed)
    assert B.engine_tick(led, feed, NOW + 10_000, POL) == {}          # quote is hours old
    v = B.book_view(led, feed, 'pm-x', NOW + 10_000, POL)
    assert v['state'] == 'partial' and v['equity'] is None and v['open'][0]['live']['state'] == 'unknown' and v['cash'] < 1000


def test_max_open_halt_and_cash(dbs):
    feed, led, _ = dbs
    put_market(feed); put_quote(feed, YES, 0.48, 0.50)
    pol = B.policy(dict(max_open=1))
    assert B.place(led, feed, 'pm-x', 'polymarket', dict(condition_id=COND, side='YES', usd=50, tp_price=.7, sl_price=.3, **PLAN), NOW, pol)['state'] == 'filled'
    assert B.place(led, feed, 'pm-x', 'polymarket', dict(condition_id=COND, side='YES', usd=50, tp_price=.7, sl_price=.3, **PLAN), NOW, pol)['reason'] == 'max_open'
    led.execute('UPDATE bk_books SET halted=1')
    assert B.place(led, feed, 'pm-x', 'polymarket', dict(condition_id=COND, side='YES', usd=50, tp_price=.7, sl_price=.3, **PLAN), NOW, B.policy())['reason'] == 'book_halted'


# ---- perps -----------------------------------------------------------------------------------------------------------------

def perp_order(led, feed, **kw):
    args = dict(coin='BTC', side='long', margin_usd=100.0, leverage=5, tp_pct=5.0, sl_pct=2.0, **PLAN)
    return B.place(led, feed, 'perps-x', 'perps', {**args, **kw}, NOW, POL)


def test_liquidation_price_formula():
    # 10x long on a 20x coin: l = 1/40; liq = entry*(1 - (0.1-0.025)/(1-0.025))
    assert B.perp_liq_price(100.0, 'long', 10, 20) == pytest.approx(100 * (1 - 0.075 / 0.975))
    assert B.perp_liq_price(100.0, 'short', 10, 20) == pytest.approx(100 * (1 + 0.075 / 1.025))
    assert B.perp_liq_price(100.0, 'long', 1, 20) < B.perp_liq_price(100.0, 'long', 10, 20) < 100


def test_leverage_clamp_stop_may_cost_at_most_half_the_margin():
    assert B.clamp_leverage(10, 5, 2.0, 50.0) == 5 and B.clamp_leverage(5, 20, 20.0, 50.0) == 2 and B.clamp_leverage(5, 20, 80.0, 50.0) == 1


def test_perp_fill_fee_liq_and_applied_leverage(dbs):
    feed, led, _ = dbs
    put_perp(feed)
    r = perp_order(led, feed, leverage=50, sl_pct=2.0)       # asked 50x: cap 5 wins
    assert r['state'] == 'filled'
    p = led.execute('SELECT * FROM bk_positions').fetchone()
    assert p['leverage'] == 5 and p['entry_price'] == pytest.approx(100.02) and p['notional'] == pytest.approx(500)
    assert p['entry_fee'] == pytest.approx(500 * 4.5e-4) and p['qty'] == pytest.approx(500 / 100.02)
    assert p['liq_price'] == pytest.approx(B.perp_liq_price(100.02, 'long', 5, 20))
    assert p['sl_price'] == pytest.approx(100.02 * 0.98) and p['tp_price'] == pytest.approx(100.02 * 1.05)
    assert json.loads(p['meta_json'])['leverage_asked'] == 50
    assert led.execute('SELECT cash FROM bk_books').fetchone()[0] == pytest.approx(1000 - 100 - 0.225)


def test_perp_short_unrealized_and_pnl_sign(dbs):
    feed, led, _ = dbs
    put_perp(feed)
    perp_order(led, feed, side='short', leverage=2)
    put_perp(feed, mid=95.0, at=NOW + 5)
    lv = B.book_view(led, feed, 'perps-x', NOW + 5, POL)['open'][0]['live']
    assert lv['gross_pnl'] > 0 and lv['price'] == pytest.approx(95.0 * (1 + 2e-4))
    assert B.pnl('short', 100, 95, 2) == 10 and B.pnl('long', 100, 95, 2) == -10


def test_perp_funding_accrues_long_pays_short_receives(dbs):
    feed, led, _ = dbs
    put_perp(feed, funding=0.0001)
    perp_order(led, feed)
    perp_order(led, feed, side='short', coin='BTC')
    put_perp(feed, funding=0.0001, at=NOW + 3600)
    B.engine_tick(led, feed, NOW + 3600, POL)
    long_, short = [led.execute('SELECT * FROM bk_positions WHERE side=?', (s,)).fetchone() for s in ('long', 'short')]
    assert long_['funding_paid'] == pytest.approx(long_['qty'] * 100 * 0.0001) and short['funding_paid'] == pytest.approx(-long_['funding_paid'], rel=0.01)
    assert long_['liq_price'] > B.perp_liq_price(long_['entry_price'], 'long', 5, 20)      # paying funding moves liquidation closer
    assert led.execute('SELECT COUNT(*) FROM bk_events WHERE kind=?', ('funding',)).fetchone()[0] == 2


def test_perp_eight_hour_funding_is_prorated(dbs):
    feed, led, _ = dbs
    put_perp(feed, coin='ETHUSDT', venue='binance', funding=0.0008, funding_h=8.0)
    perp_order(led, feed, coin='ETHUSDT', venue='binance')
    put_perp(feed, coin='ETHUSDT', venue='binance', funding=0.0008, funding_h=8.0, at=NOW + 3600)
    B.engine_tick(led, feed, NOW + 3600, POL)
    p = led.execute('SELECT * FROM bk_positions').fetchone()
    assert p['funding_paid'] == pytest.approx(p['qty'] * 100 * 0.0001)


def test_perp_liquidated_loses_the_whole_margin(dbs):
    feed, led, _ = dbs
    put_perp(feed, funding=0.0)
    perp_order(led, feed, leverage=5, sl_pct=10.0, tp_pct=30.0)       # wide stop so the liquidation fires first
    liq = led.execute('SELECT liq_price FROM bk_positions').fetchone()[0]
    put_perp(feed, mid=liq - 0.5, funding=0.0, at=NOW + 30)
    assert B.engine_tick(led, feed, NOW + 30, POL) == {'liquidated': 1}
    p = led.execute('SELECT * FROM bk_positions').fetchone()
    assert p['exit_price'] == pytest.approx(liq) and p['realized'] == pytest.approx(-100 - p['entry_fee'])
    assert led.execute('SELECT cash FROM bk_books').fetchone()[0] == pytest.approx(1000 - 100 - p['entry_fee'])


def test_perp_stop_target_and_max_hold(dbs):
    feed, led, _ = dbs
    put_perp(feed)
    perp_order(led, feed)
    put_perp(feed, mid=97.0, at=NOW + 30)
    assert B.engine_tick(led, feed, NOW + 30, POL) == {'stop_loss': 1}
    perp_order_at = lambda: B.place(led, feed, 'perps-x', 'perps', dict(coin='BTC', side='long', margin_usd=100, leverage=2, tp_pct=5, sl_pct=3, **PLAN), NOW + 40, POL)
    put_perp(feed, mid=100.0, at=NOW + 40); perp_order_at()
    put_perp(feed, mid=100.0, funding=0.0, at=NOW + 40 + 4 * 3600 + 1)
    assert B.engine_tick(led, feed, NOW + 40 + 4 * 3600 + 1, B.policy(dict(quote_max_age_s=1e9))) == {'max_hold': 1}


def test_perp_unknown_coin_and_bad_fields(dbs):
    feed, led, _ = dbs
    put_perp(feed)
    assert perp_order(led, feed, coin='NOPE')['reason'] == 'quote_unknown_or_stale'
    assert perp_order(led, feed, sl_pct=50)['reason'] == 'order_fields'
    assert perp_order(led, feed, side='flat')['reason'] == 'side_must_be_long_or_short'


# ---- stocks ----------------------------------------------------------------------------------------------------------------

def stock_order(led, feed, now=NOW, **kw):
    args = dict(symbol='NVDA', side='long', margin_usd=100.0, leverage=3, tp_pct=3.0, sl_pct=1.0, **PLAN)
    return B.place(led, feed, 'stocks-x', 'stocks', {**args, **kw}, now, POL)


def test_stock_fill_cost_and_leverage_clamp(dbs):
    feed, led, _ = dbs
    put_stock(feed)
    assert stock_order(led, feed, leverage=9)['state'] == 'filled'
    p = led.execute('SELECT * FROM bk_positions').fetchone()
    assert p['leverage'] == 5 and p['entry_price'] == pytest.approx(100.02) and p['qty'] == pytest.approx(500 / 100.02) and p['entry_fee'] == 0
    assert p['liq_price'] == pytest.approx(100.02 * (1 - 0.9 / 5))
    assert B.stock_liq_price(100.0, 'short', 5, 90) == pytest.approx(118.0)


def test_stock_market_hours(dbs):
    feed, led, _ = dbs
    put_stock(feed, start=NOW + 3600, end=NOW + 7 * 3600)
    assert stock_order(led, feed)['reason'] == 'market_closed'
    put_stock(feed, start=NOW - 3600, end=NOW + 1200)                  # 20 minutes to the close
    assert stock_order(led, feed)['reason'].startswith('no_new_positions')
    put_stock(feed, sym='AMD', at=NOW - 1000)                           # last trade 1000 s ago: stale
    assert stock_order(led, feed, symbol='AMD')['reason'] == 'quote_unknown_or_stale'


def test_stock_flat_before_close_and_rails(dbs):
    feed, led, _ = dbs
    put_stock(feed, end=NOW + 4 * 3600)
    stock_order(led, feed)
    t = NOW + 4 * 3600 - 200
    put_stock(feed, price=100.5, at=t, end=NOW + 4 * 3600)
    assert B.engine_tick(led, feed, t, POL) == {'session_flat': 1}
    p = led.execute('SELECT * FROM bk_positions').fetchone()
    assert p['exit_reason'] == 'session_flat' and p['realized'] > 0
    put_stock(feed, sym='AMD', price=100.0, at=NOW + 10, end=NOW + 4 * 3600)
    stock_order(led, feed, now=NOW + 10, side='short', symbol='AMD')
    put_stock(feed, sym='AMD', price=101.5, at=NOW + 20, end=NOW + 4 * 3600)
    assert B.engine_tick(led, feed, NOW + 20, POL) == {'stop_loss': 1}


def test_stock_short_pnl_and_closed_market_exit_refused(dbs):
    feed, led, _ = dbs
    put_stock(feed)
    pid = stock_order(led, feed, side='short')['pos_id']
    put_stock(feed, price=98.0, at=NOW + 5)
    assert B.book_view(led, feed, 'stocks-x', NOW + 5, POL)['open'][0]['live']['gross_pnl'] > 0
    put_stock(feed, price=98.0, at=NOW + 6, start=NOW + 3600, end=NOW + 9000)
    assert B.manual_exit(led, feed, pid, 'stocks-x', 'x', NOW + 6, POL)['reason'] == 'market_closed'


def test_strategy_registry_and_verdicts(dbs):
    feed, led, _ = dbs
    from sniper_office.markets import strategies as ST
    put_perp(feed)
    assert perp_order(led, feed, strategy='perps-level-break')['state'] == 'filled'
    assert perp_order(led, feed, strategy='none fits')['reason'] == 'strategy_reason_required'
    spec = dict(hypothesis='h', entry_rule='e', exit_rule='x', kill_criterion='k')
    r = ST.register(led, 'perps_trader', 'perps', spec, NOW)
    assert r['state'] == 'registered' and ST.register(led, 'perps_trader', 'perps', spec, NOW)['state'] == 'known'
    assert ST.register(led, 'perps_trader', 'perps', dict(spec, hypothesis=''), NOW)['reason'] == 'fields_required'
    assert perp_order(led, feed, strategy=r['strategy_id'])['state'] == 'filled'
    res = ST.results(led, 'perps')
    row = next(s for s in res['strategies'] if s['strategy_id'] == 'perps-level-break')
    assert row['open_n'] == 1 and row['verdict'] == 'no settled result yet' and ST.verdict(3, 5.0).startswith('too few') and ST.verdict(10, 2.0) == 'works' and ST.verdict(10, -2.0) == 'does not work'
    for i in range(5):
        ST.register(led, 'busy', 'perps', dict(spec, hypothesis='h%d' % i), NOW)
    assert ST.register(led, 'busy', 'perps', dict(spec, hypothesis='h6'), NOW)['reason'] == 'daily_limit'
