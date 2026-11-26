"""Paper books, one set per seat, in an Office-owned ledger (separate book ids). Paper only: no key, no order to any venue; every fill is a model priced from the newest fetched tick, with its source and time kept.

Lanes
  polymarket  buy YES or NO shares at the fetched best ask (taker fee rate * p * (1-p) per share, the source desk's model), exit at the best bid, or settle at resolution (payout 1 or 0 per share, no fee).
  perps       long or short at the fetched mid plus slippage and the venue taker fee, isolated margin, leverage capped, funding accrued each engine tick from the newest funding rate, liquidation price
              from the venue's maintenance rule (maintenance leverage = twice the coin's max leverage); marked at mid.
  stocks      long or short at the fetched last price plus a per-side cost (2 bps, the source desk's), regular session only, nothing new in the last 30 minutes, flat 5 minutes before the close; leverage <= 5.
Unknown price, stale quote or closed market: no fill and no mark. An open position without a fresh price stays open and unpriced; it is never closed at an invented price.
Not modelled (each makes a result optimistic): queue and landing delay, depth beyond the best level (an order larger than the level's size is refused), intrabar stop walks (rails fire on the tick that sees them and exit at that tick's price),
stock borrow cost and halts, funding between ticks at a different rate, the venue's partial liquidation."""
import hashlib
import json
import math
import time
import uuid

from . import common as C

START_CASH = 1000.0
POLICY = dict(
    start_cash=START_CASH, review_gate=True, quote_max_age_s=120.0, max_open=5, max_margin_pct=25.0,
    pm_min_price=0.03, pm_max_price=0.97, pm_max_spread=0.06,
    perp_max_leverage=5.0, perp_slip_bps=2.0, perp_fee_bps=4.5, perp_max_hold_s=4 * 3600.0, perp_sl_pct=(0.1, 10.0), perp_tp_pct=(0.1, 30.0),
    stock_max_leverage=5.0, stock_cost_bps=2.0, stock_quote_max_age_s=180.0, stock_no_new_s=1800.0, stock_flat_before_s=300.0, stock_sl_pct=(0.1, 10.0), stock_tp_pct=(0.1, 30.0),
    max_loss_on_margin_pct=50.0, stock_liq_loss_pct=90.0,
)
LANES = ('polymarket', 'perps', 'stocks')
PLAN_FIELDS = ('thesis', 'disproof', 'exit_plan')


class Refused(Exception):
    def __init__(self, reason, **more):
        super().__init__(reason)
        self.reason, self.more = reason, more


def policy(cfg=None):
    return {**POLICY, **{k: v for k, v in (cfg or {}).items() if k in POLICY}}


# ---- pure math -------------------------------------------------------------------------------------------------------------

def pm_fee(rate, exp, price):
    """Taker fee in dollars per share: rate * (p * (1-p)) ** exp."""
    return rate * (price * (1.0 - price)) ** exp


def perp_liq_price(entry, side, leverage, max_leverage):
    """Isolated-margin liquidation price (the venue's rule): liq = entry - s * (margin - maintenance) / size / (1 - s * l), s=+1 long / -1 short, l = 1 / (2 * max leverage)."""
    s = 1.0 if side == 'long' else -1.0
    l = 1.0 / (2.0 * max_leverage)
    return entry - s * entry * (1.0 / leverage - l) / (1.0 - s * l)


def pnl(side, entry, price, qty):
    return (1.0 if side == 'long' else -1.0) * (price - entry) * qty


def stock_liq_price(entry, side, leverage, loss_pct):
    return entry * (1.0 - (1.0 if side == 'long' else -1.0) * (loss_pct / 100.0) / leverage)


def clamp_leverage(lev, cap, sl_pct, max_loss_pct):
    """Leverage cut so a stop loses at most max_loss_pct of margin (the source desk's rule); at least 1."""
    return max(1.0, min(float(lev), float(cap), float(int(max_loss_pct // sl_pct)) or 1.0))


# ---- ledger ----------------------------------------------------------------------------------------------------------------

def book(led, book_id, lane, actor=None, role=None, now=None, start_cash=START_CASH):
    led.execute('INSERT OR IGNORE INTO bk_books(book_id,lane,actor_id,role,start_cash,cash,created_at) VALUES(?,?,?,?,?,?,?)', (book_id, lane, actor, role, start_cash, start_cash, now or time.time()))
    return led.execute('SELECT * FROM bk_books WHERE book_id=?', (book_id,)).fetchone()


def event(led, book_id, pos_id, now, kind, detail):
    led.execute('INSERT INTO bk_events(book_id,pos_id,at,kind,detail_json) VALUES(?,?,?,?,?)', (book_id, pos_id, now, kind, json.dumps(detail, default=str)))


def _latest(feed, sql, args):
    return feed.execute(sql, args).fetchone()


def quote(feed, lane, instrument, now, pol):
    """The newest usable price row for a position's instrument as a dict, or None. pm: bid/ask/sizes; perps: mid/mark/funding; stocks: price with session times."""
    if lane == 'polymarket':
        r = _latest(feed, 'SELECT * FROM mk_pm_quotes WHERE token_id=? ORDER BY fetched_at DESC LIMIT 1', (instrument,))
        if r and now - r['fetched_at'] <= pol['quote_max_age_s']:
            return dict(bid=r['bid'], ask=r['ask'], bid_size=r['bid_size'], ask_size=r['ask_size'], at=r['fetched_at'], source=r['source'])
    elif lane == 'perps':
        venue, _, coin = instrument.partition(':')
        r = _latest(feed, 'SELECT * FROM mk_perp_ticks WHERE venue=? AND coin=? ORDER BY fetched_at DESC LIMIT 1', (venue, coin))
        px = r and (r['mid'] or r['mark'])
        if r and px and now - r['fetched_at'] <= pol['quote_max_age_s']:
            return dict(price=px, mark=r['mark'], funding=r['funding'], funding_h=r['funding_h'] or 1.0, max_leverage=r['max_leverage'], at=r['fetched_at'], source=r['source'])
    elif lane == 'stocks':
        r = _latest(feed, 'SELECT * FROM mk_stock_quotes WHERE symbol=? ORDER BY fetched_at DESC LIMIT 1', (instrument,))
        if r and r['price'] and r['quote_ts'] and now - r['quote_ts'] <= pol['stock_quote_max_age_s'] and now - r['fetched_at'] <= pol['stock_quote_max_age_s']:
            return dict(price=r['price'], at=r['quote_ts'], fetched_at=r['fetched_at'], start=r['regular_start'], end=r['regular_end'], source=r['source'])
    return None


def session_open(q, now):
    return bool(q and q.get('start') and q.get('end') and q['start'] <= now < q['end'])


def _plan(args):
    plan = {}
    for k in PLAN_FIELDS:
        v = ' '.join(str(args.get(k) or '').split())
        if not 8 <= len(v) <= 400:
            raise Refused('plan_missing', field=k, rule='thesis, disproof and exit_plan are required, 8 to 400 characters each')
        plan[k] = v
    return plan


def _strategy(led, lane, args):
    """Every order names a strategy of this lane that is registered and not retired; "none fits" and no strategy are refused."""
    from . import strategies as S
    sid = args.get('strategy')
    bad = 'strategy_unregistered' if not isinstance(sid, str) or sid in ('', S.NONE_FITS) else S.usable(led, lane, sid)
    if bad:
        raise Refused(bad, strategy=sid, available=sorted(S.options(lane)),
                      rule='name a strategy from this lane\'s strategies tool; to test an idea, register it first (hypothesis, entry_rule, exit_rule, kill_criterion, min_n) with the lane\'s strategy_register tool')
    return sid, None


def unreviewed(led, b, role):
    """Closed losing positions of the book that the book's own seat has not written a lesson (with a cause and a change) for."""
    return [dict(r) for r in led.execute(
        "SELECT pos_id,instrument,side,entry_price,exit_price,realized,exit_reason,thesis,disproof,strategy_id FROM bk_positions p WHERE book_id=? AND status='closed' AND realized<0 AND NOT EXISTS "
        "(SELECT 1 FROM bk_lessons l WHERE l.pos_id=p.pos_id AND l.by_role=? AND IFNULL(l.cause,'')<>'' AND IFNULL(l.change,'')<>'') ORDER BY exit_at LIMIT 10", (b['book_id'], role or b['role']))]


def _gate(led, b, pol, margin, lane):
    if b['halted']:
        raise Refused('book_halted')
    if led.execute("SELECT COUNT(*) FROM bk_positions WHERE book_id=? AND status='open'", (b['book_id'],)).fetchone()[0] >= pol['max_open']:
        raise Refused('max_open', max_open=pol['max_open'])
    if margin > b['cash'] + 1e-9:
        raise Refused('insufficient_cash', cash=round(b['cash'], 2))
    if margin > b['start_cash'] * pol['max_margin_pct'] / 100.0 + 1e-9:
        raise Refused('margin_above_cap', cap_usd=round(b['start_cash'] * pol['max_margin_pct'] / 100.0, 2))


def _insert(led, b, now, **p):
    p.setdefault('pos_id', 'pos_' + uuid.uuid4().hex[:16])
    row = dict(book_id=b['book_id'], lane=b['lane'], entry_at=now, status='open', last_funding_at=now, **p)
    cols = list(row)
    led.execute('INSERT INTO bk_positions(%s) VALUES(%s)' % (','.join(cols), ','.join('?' * len(cols))), [row[c] for c in cols])
    led.execute('UPDATE bk_books SET cash=cash-? WHERE book_id=?', (row['margin'] + row['entry_fee'], b['book_id']))
    event(led, b['book_id'], row['pos_id'], now, 'open', {k: row[k] for k in ('instrument', 'side', 'qty', 'entry_price', 'margin', 'leverage', 'tp_price', 'sl_price')})
    return row['pos_id']


# ---- orders ----------------------------------------------------------------------------------------------------------------

def open_polymarket(led, feed, b, now, args, pol):
    plan, (sid, sreason) = _plan(args), _strategy(led, 'polymarket', args)
    m = feed.execute('SELECT * FROM mk_pm_markets WHERE condition_id=?', (args.get('condition_id'),)).fetchone()
    if not m:
        raise Refused('market_unknown')
    side = str(args.get('side') or '').upper()
    if side not in ('YES', 'NO'):
        raise Refused('side_must_be_YES_or_NO')
    if m['closed'] or not m['accepting'] or (m['end_ts'] and m['end_ts'] <= now):
        raise Refused('market_not_open')
    token = m['yes_token'] if side == 'YES' else m['no_token']
    q = quote(feed, 'polymarket', token, now, pol)
    if not q or q['ask'] is None:
        raise Refused('quote_unknown_or_stale', max_age_s=pol['quote_max_age_s'])
    ask, bid = q['ask'], q['bid']
    if not pol['pm_min_price'] <= ask <= pol['pm_max_price'] or ask - bid > pol['pm_max_spread']:
        raise Refused('price_or_spread_outside_rails', ask=ask, bid=bid, rails=[pol['pm_min_price'], pol['pm_max_price'], pol['pm_max_spread']])
    usd = C.num(args.get('usd'))
    tp, sl = C.num(args.get('tp_price')), C.num(args.get('sl_price'))
    if not usd or usd <= 0 or tp is None or sl is None or not 0 < sl < ask < tp < 1:
        raise Refused('order_fields', rule='usd > 0 and 0 < sl_price < entry ask < tp_price < 1 (token prices)')
    fee1 = pm_fee(m['fee_rate'], m['fee_exp'], ask)
    shares = math.floor(usd / (ask + fee1) * 100) / 100.0
    if shares <= 0:
        raise Refused('too_small')
    if q['ask_size'] is not None and shares > q['ask_size']:
        raise Refused('insufficient_depth_at_best_ask', ask_size=q['ask_size'], shares=shares)
    cost = shares * ask
    _gate(led, b, pol, cost + shares * fee1, 'polymarket')
    meta = dict(condition_id=m['condition_id'], outcome=side, question=m['question'], end_ts=m['end_ts'], fee_rate=m['fee_rate'], fee_exp=m['fee_exp'], fee_source=m['fee_source'])
    proof = dict(model='pm-ask-v1', ask=ask, bid=bid, ask_size=q['ask_size'], quote_at=q['at'], source=q['source'], filled_at=now)
    return _insert(led, b, now, instrument=token, label='%s %s' % (side, m['question'][:80]), side=side, qty=shares, entry_price=ask, leverage=1.0, margin=cost, notional=cost, liq_price=None, tp_price=tp, sl_price=sl,
                   strategy_id=sid, strategy_reason=sreason, news_id=args.get('news_id'), entry_fee=shares * fee1, meta_json=json.dumps(meta), proof_json=json.dumps(proof), **plan)


def open_perps(led, feed, b, now, args, pol):
    plan, (sid, sreason) = _plan(args), _strategy(led, 'perps', args)
    coin, venue = str(args.get('coin') or '').strip(), str(args.get('venue') or 'hyperliquid')
    side = str(args.get('side') or '').lower()
    if side not in ('long', 'short'):
        raise Refused('side_must_be_long_or_short')
    q = quote(feed, 'perps', '%s:%s' % (venue, coin), now, pol)
    if not q:
        raise Refused('quote_unknown_or_stale', max_age_s=pol['quote_max_age_s'])
    sl, tp, margin, lev = C.num(args.get('sl_pct')), C.num(args.get('tp_pct')), C.num(args.get('margin_usd')), C.num(args.get('leverage'))
    if None in (sl, tp, margin, lev) or margin <= 0 or lev < 1 or not (pol['perp_sl_pct'][0] <= sl <= pol['perp_sl_pct'][1] and pol['perp_tp_pct'][0] <= tp <= pol['perp_tp_pct'][1]):
        raise Refused('order_fields', rule='margin_usd > 0, leverage >= 1, sl_pct %s, tp_pct %s (percent of price)' % (pol['perp_sl_pct'], pol['perp_tp_pct']))
    cap = min(pol['perp_max_leverage'], q['max_leverage'] or pol['perp_max_leverage'])
    lev_applied = clamp_leverage(lev, cap, sl, pol['max_loss_on_margin_pct'])
    s = 1.0 if side == 'long' else -1.0
    fill = q['price'] * (1.0 + s * pol['perp_slip_bps'] / 1e4)
    notional = margin * lev_applied
    qty = notional / fill
    fee = notional * pol['perp_fee_bps'] / 1e4
    _gate(led, b, pol, margin + fee, 'perps')
    liq = perp_liq_price(fill, side, lev_applied, q['max_leverage'] or 20.0)
    proof = dict(model='perp-mid-v1', mid=q['price'], mark=q['mark'], slip_bps=pol['perp_slip_bps'], fee_bps=pol['perp_fee_bps'], quote_at=q['at'], source=q['source'], filled_at=now, leverage_asked=lev,
                 funding=q['funding'], funding_h=q['funding_h'])
    return _insert(led, b, now, instrument='%s:%s' % (venue, coin), label='%s %s x%g' % (side, coin, lev_applied), side=side, qty=qty, entry_price=fill, leverage=lev_applied, margin=margin, notional=notional,
                   liq_price=liq, tp_price=fill * (1 + s * tp / 100.0), sl_price=fill * (1 - s * sl / 100.0), strategy_id=sid, strategy_reason=sreason, news_id=args.get('news_id'), entry_fee=fee,
                   meta_json=json.dumps(dict(venue=venue, coin=coin, max_leverage=q['max_leverage'], max_hold_s=pol['perp_max_hold_s'], leverage_asked=lev)), proof_json=json.dumps(proof), **plan)


def open_stocks(led, feed, b, now, args, pol):
    plan, (sid, sreason) = _plan(args), _strategy(led, 'stocks', args)
    sym = str(args.get('symbol') or '').upper().strip()
    side = str(args.get('side') or '').lower()
    if side not in ('long', 'short'):
        raise Refused('side_must_be_long_or_short')
    q = quote(feed, 'stocks', sym, now, pol)
    if not q:
        raise Refused('quote_unknown_or_stale', max_age_s=pol['stock_quote_max_age_s'])
    if not session_open(q, now):
        raise Refused('market_closed', regular_start=q['start'], regular_end=q['end'])
    if q['end'] - now < pol['stock_no_new_s']:
        raise Refused('no_new_positions_in_last_%d_minutes' % (pol['stock_no_new_s'] // 60))
    sl, tp, margin, lev = C.num(args.get('sl_pct')), C.num(args.get('tp_pct')), C.num(args.get('margin_usd')), C.num(args.get('leverage'))
    if None in (sl, tp, margin, lev) or margin <= 0 or lev < 1 or not (pol['stock_sl_pct'][0] <= sl <= pol['stock_sl_pct'][1] and pol['stock_tp_pct'][0] <= tp <= pol['stock_tp_pct'][1]):
        raise Refused('order_fields', rule='margin_usd > 0, leverage >= 1, sl_pct %s, tp_pct %s' % (pol['stock_sl_pct'], pol['stock_tp_pct']))
    lev_applied = clamp_leverage(lev, pol['stock_max_leverage'], sl, pol['max_loss_on_margin_pct'])
    s = 1.0 if side == 'long' else -1.0
    fill = q['price'] * (1.0 + s * pol['stock_cost_bps'] / 1e4)
    notional = margin * lev_applied
    _gate(led, b, pol, margin, 'stocks')
    proof = dict(model='stock-last-v1', last=q['price'], cost_bps=pol['stock_cost_bps'], quote_ts=q['at'], fetched_at=q['fetched_at'], source=q['source'], filled_at=now, leverage_asked=lev)
    return _insert(led, b, now, instrument=sym, label='%s %s x%g' % (side, sym, lev_applied), side=side, qty=notional / fill, entry_price=fill, leverage=lev_applied, margin=margin, notional=notional,
                   liq_price=stock_liq_price(fill, side, lev_applied, pol['stock_liq_loss_pct']), tp_price=fill * (1 + s * tp / 100.0), sl_price=fill * (1 - s * sl / 100.0), strategy_id=sid,
                   strategy_reason=sreason, news_id=args.get('news_id'), entry_fee=0.0, meta_json=json.dumps(dict(regular_end=q['end'])), proof_json=json.dumps(proof), **plan)


OPEN = dict(polymarket=open_polymarket, perps=open_perps, stocks=open_stocks)


def place(led, feed, book_id, lane, args, now, pol, actor=None, role=None):
    """Open a position. Returns dict(state='filled', pos_id=...) or dict(state='refused', reason=...). One ledger transaction."""
    try:
        with C.txn(led):
            b = book(led, book_id, lane, actor, role, now, pol['start_cash'])
            if pol['review_gate'] and not b['halted'] and (owed := unreviewed(led, b, role)):
                raise Refused('review_pending', unreviewed=owed, next='call lesson_add for each pos_id: pos_id, cause (what went wrong), change (what you will do differently); then order again')
            return dict(state='filled', pos_id=OPEN[lane](led, feed, b, now, args, pol), book_id=book_id)
    except Refused as r:
        return dict(state='refused', reason=r.reason, **r.more)


# ---- marks, exits ----------------------------------------------------------------------------------------------------------

def exit_price(pos, q, pol=POLICY):
    """The price a position would close at on the newest quote."""
    if pos['lane'] == 'polymarket':
        return q['bid']
    s = 1.0 if pos['side'] == 'long' else -1.0
    slip = pol['perp_slip_bps'] if pos['lane'] == 'perps' else pol['stock_cost_bps']
    return q['price'] * (1.0 - s * slip / 1e4)


def _exit_fee(pos, price, pol):
    if pos['lane'] == 'polymarket':
        m = json.loads(pos['meta_json'])
        return pos['qty'] * pm_fee(m['fee_rate'], m['fee_exp'], price)
    return pos['qty'] * price * pol['perp_fee_bps'] / 1e4 if pos['lane'] == 'perps' else 0.0


def live(pos, q, pol):
    """The position's live numbers on quote q (None: unpriced): price, gross, net unrealized (after both fees and funding), and what closing now would return as cash."""
    if q is None:
        return None
    px = exit_price(pos, q, pol)
    if px is None:
        return None
    fee = _exit_fee(pos, px, pol)
    gross = pnl('long' if pos['lane'] == 'polymarket' else pos['side'], pos['entry_price'], px, pos['qty'])
    value = max(0.0, pos['margin'] + gross - fee) if pos['lane'] != 'polymarket' else pos['qty'] * px - fee
    return dict(price=px, mark_at=q['at'], source=q['source'], gross_pnl=gross, unrealized=value - pos['margin'] - pos['entry_fee'] - pos['funding_paid'] if pos['lane'] != 'polymarket' else value - pos['margin'] - pos['entry_fee'],
                value=value, pnl_pct_of_margin=100.0 * (value - pos['margin'] - pos['entry_fee'] - (pos['funding_paid'] if pos['lane'] != 'polymarket' else 0.0)) / pos['margin'])


def close(led, pos, price, now, reason, pol, *, proof=None, fee=None, payout_per_share=None):
    """Close at `price` (or settle at payout_per_share). Cash back = margin (or proceeds) + pnl - exit fee; realized adds the entry fee and the funding already paid. Idempotent on status."""
    if pos['status'] != 'open':
        return None
    if pos['lane'] == 'polymarket':
        fee = 0.0 if payout_per_share is not None else _exit_fee(pos, price, pol)
        proceeds = pos['qty'] * (payout_per_share if payout_per_share is not None else price) - fee
        back, realized = proceeds, proceeds - pos['margin'] - pos['entry_fee']
        price = payout_per_share if payout_per_share is not None else price
    else:
        fee = _exit_fee(pos, price, pol) if fee is None else fee
        gross = pnl(pos['side'], pos['entry_price'], price, pos['qty'])
        back = 0.0 if reason == 'liquidated' and pos['lane'] == 'perps' else max(0.0, pos['margin'] + gross - fee)
        realized = back - pos['margin'] - pos['entry_fee'] - pos['funding_paid']
    led.execute("UPDATE bk_positions SET status='closed',exit_price=?,exit_at=?,exit_reason=?,exit_fee=?,realized=?,mark=?,mark_at=? WHERE pos_id=? AND status='open'", (price, now, reason, fee, realized, price, now, pos['pos_id']))
    led.execute('UPDATE bk_books SET cash=cash+? WHERE book_id=?', (back, pos['book_id']))
    event(led, pos['book_id'], pos['pos_id'], now, 'close', dict(reason=reason, price=price, fee=fee, realized=realized, proof=proof))
    if pos['strategy_id']:
        from . import strategies as S
        if stats := S.retire_check(led, pos['strategy_id'], now):
            event(led, pos['book_id'], pos['pos_id'], now, 'strategy_retired', dict(strategy_id=pos['strategy_id'], **stats))
    return realized


def manual_exit(led, feed, pos_id, book_id, reason, now, pol):
    with C.txn(led):
        pos = led.execute('SELECT * FROM bk_positions WHERE pos_id=? AND book_id=?', (pos_id, book_id)).fetchone()
        if not pos:
            return dict(state='refused', reason='position_unknown')
        if pos['status'] != 'open':
            return dict(state='refused', reason='already_closed', exit_reason=pos['exit_reason'])
        q = quote(feed, pos['lane'], pos['instrument'], now, pol)
        if q is None:
            return dict(state='refused', reason='quote_unknown_or_stale', note='stays open, unpriced; retry when the feed is fresh')
        if pos['lane'] == 'stocks' and not session_open(q, now):
            return dict(state='refused', reason='market_closed')
        px = exit_price(pos, q, pol)
        r = close(led, pos, px, now, 'seat_exit: ' + str(reason)[:200], pol, proof=dict(quote_at=q['at'], source=q['source']))
        return dict(state='closed', pos_id=pos_id, exit_price=px, realized=r)


# ---- engine ----------------------------------------------------------------------------------------------------------------

def resolved_payout(feed, pos):
    """Payout per share of a polymarket position once its market is closed with a 0 or 1 resolution, else None."""
    m = json.loads(pos['meta_json'])
    row = feed.execute('SELECT closed,resolved_yes,fetched_at FROM mk_pm_markets WHERE condition_id=?', (m['condition_id'],)).fetchone()
    if not row or not row['closed'] or row['resolved_yes'] not in (0.0, 1.0):
        return None
    return row['resolved_yes'] if m['outcome'] == 'YES' else 1.0 - row['resolved_yes']


def _rail(pos, px):
    long_like = pos['lane'] == 'polymarket' or pos['side'] == 'long'
    if pos['sl_price'] is not None and (px <= pos['sl_price'] if long_like else px >= pos['sl_price']):
        return 'stop_loss'
    if pos['tp_price'] is not None and (px >= pos['tp_price'] if long_like else px <= pos['tp_price']):
        return 'take_profit'
    return None


def step(led, feed, pos, now, pol):
    """One engine pass over one open position: settle, accrue funding, liquidate, rails, time and session exits. Returns what happened (a string) or None."""
    lane = pos['lane']
    if lane == 'polymarket':
        pay = resolved_payout(feed, pos)
        if pay is not None:
            close(led, pos, None, now, 'resolved', pol, payout_per_share=pay)
            return 'resolved'
    q = quote(feed, lane, pos['instrument'], now, pol)
    if q is None:
        return None            # unpriced: stays open
    px = exit_price(pos, q, pol)
    led.execute('UPDATE bk_positions SET mark=?,mark_at=? WHERE pos_id=?', (px, q['at'], pos['pos_id']))
    pos = dict(pos, mark=px)
    if lane == 'perps':
        dt = max(0.0, now - (pos['last_funding_at'] or now))
        if q['funding'] is not None and dt > 0:
            pay = (1.0 if pos['side'] == 'long' else -1.0) * (q['funding'] / q['funding_h']) * (dt / 3600.0) * pos['qty'] * (q['mark'] or px)
            led.execute('UPDATE bk_positions SET funding_paid=funding_paid+?,last_funding_at=?,liq_price=liq_price+? WHERE pos_id=?',
                        (pay, now, (1.0 if pos['side'] == 'long' else -1.0) * pay / pos['qty'], pos['pos_id']))
            led.execute('UPDATE bk_books SET cash=cash-? WHERE book_id=?', (pay, pos['book_id']))
            event(led, pos['book_id'], pos['pos_id'], now, 'funding', dict(paid=pay, rate=q['funding'], per_h=q['funding_h'], dt_s=dt, source=q['source']))
            pos = dict(pos, funding_paid=pos['funding_paid'] + pay, liq_price=pos['liq_price'] + (1.0 if pos['side'] == 'long' else -1.0) * pay / pos['qty'], last_funding_at=now)
        else:
            led.execute('UPDATE bk_positions SET last_funding_at=? WHERE pos_id=?', (now, pos['pos_id']))
        if (px <= pos['liq_price'] if pos['side'] == 'long' else px >= pos['liq_price']):
            close(led, pos, pos['liq_price'], now, 'liquidated', pol, proof=dict(mark=px, quote_at=q['at']))
            return 'liquidated'
    if lane == 'stocks' and pos['liq_price'] and (px <= pos['liq_price'] if pos['side'] == 'long' else px >= pos['liq_price']):
        close(led, pos, px, now, 'liquidated', pol, proof=dict(quote_at=q['at']))
        return 'liquidated'
    why = _rail(pos, px)
    if not why and lane == 'perps' and now - pos['entry_at'] >= json.loads(pos['meta_json']).get('max_hold_s', pol['perp_max_hold_s']):
        why = 'max_hold'
    if not why and lane == 'stocks':
        end = q['end'] or json.loads(pos['meta_json']).get('regular_end')
        if end and now >= end - pol['stock_flat_before_s']:
            why = 'session_flat'      # priced only while the quote is fresh; after the close the position waits, unpriced, for the next session's first quote
    if why:
        close(led, pos, px, now, why, pol, proof=dict(quote_at=q['at'], source=q['source']))
        return why
    return None


def engine_tick(led, feed, now, pol):
    done = {}
    for pos in led.execute("SELECT * FROM bk_positions WHERE status='open'").fetchall():
        with C.txn(led):
            fresh = led.execute('SELECT * FROM bk_positions WHERE pos_id=?', (pos['pos_id'],)).fetchone()
            r = step(led, feed, fresh, now, pol) if fresh['status'] == 'open' else None
        if r:
            done[r] = done.get(r, 0) + 1
    return done


# ---- views -----------------------------------------------------------------------------------------------------------------

def position_view(pos, q, pol):
    d = {k: pos[k] for k in ('pos_id', 'lane', 'instrument', 'label', 'side', 'qty', 'entry_price', 'entry_at', 'leverage', 'margin', 'liq_price', 'tp_price', 'sl_price', 'thesis', 'disproof', 'exit_plan',
                              'strategy_id', 'news_id', 'entry_fee', 'funding_paid', 'status', 'exit_price', 'exit_at', 'exit_reason', 'realized')}
    d['meta'] = json.loads(pos['meta_json'] or '{}')
    if pos['status'] == 'open':
        lv = live(pos, q, pol)
        d['live'] = lv if lv else dict(state='unknown', reason='no_fresh_price', last_mark=pos['mark'], last_mark_at=pos['mark_at'])
    return d


def book_view(led, feed, book_id, now, pol):
    b = led.execute('SELECT * FROM bk_books WHERE book_id=?', (book_id,)).fetchone()
    if not b:
        return dict(state='known', book_id=book_id, cash=pol['start_cash'], equity=pol['start_cash'], open=[], closed_n=0, realized_total=0.0, unrealized_total=0.0, note='no orders yet; the book starts on the first order')
    pos = led.execute('SELECT * FROM bk_positions WHERE book_id=? ORDER BY entry_at', (book_id,)).fetchall()
    open_, closed = [p for p in pos if p['status'] == 'open'], [p for p in pos if p['status'] == 'closed']
    views = [position_view(p, quote(feed, p['lane'], p['instrument'], now, pol), pol) for p in open_]
    priced = all('value' in v['live'] for v in views)
    realized = sum(p['realized'] or 0.0 for p in closed)
    today = now - now % 86400
    return dict(state='known' if priced else 'partial', book_id=book_id, lane=b['lane'], halted=bool(b['halted']), start_cash=b['start_cash'], cash=b['cash'],
                equity=b['cash'] + sum(v['live']['value'] for v in views) if priced else None, equity_note=None if priced else 'position_unpriced: equity unknown, cash shown',
                realized_total=realized, unrealized_total=sum(v['live']['unrealized'] for v in views) if priced else None,
                realized_today=sum(p['realized'] or 0.0 for p in closed if (p['exit_at'] or 0) >= today), closed_n=len(closed), wins=sum(1 for p in closed if (p['realized'] or 0) > 0),
                open=views, closed_recent=[position_view(p, None, pol) for p in closed[-10:]], max_open=pol['max_open'])


# ---- host process ----------------------------------------------------------------------------------------------------------

def tick(cfg, now):
    pol = policy(cfg)
    led, feed = C.connect(cfg['ledger_db'], C.LEDGER_SCHEMA), C.connect(cfg['markets_db'], C.FEED_SCHEMA)
    done = engine_tick(led, feed, now, pol)
    n = led.execute("SELECT COUNT(*) FROM bk_positions WHERE status='open'").fetchone()[0]
    detail = 'open=%d actions=%s' % (n, json.dumps(done, sort_keys=True))
    C.status(feed, 'book_engine', True, detail, now)
    return detail


def main(argv=None):
    return C.main_loop('markets-books', tick, argv, 15.0)


if __name__ == '__main__':
    raise SystemExit(main())
