"""Hyperliquid hourly candle recorder, a port of the source project's `intel-hl` service (a forward-only BTC 1h candle recorder with a 15 s delay after the hour and a pinned receipt per candle).
`python3 -m sniper_office.markets.intel_hl --config intel_hl.json [--once]` records each just-closed 1h candle of `coins` (default BTC) into mk_perp_candles, never backfilled: a candle the venue did not answer
by `give_up_s` after the hour is stored as status 'missed' with no prices. The bar must match the requested coin, interval and exact open/close times, as in the source. Public API, no key."""
from . import common as C

HL = 'https://api.hyperliquid.xyz/info'
HOUR = 3600
DEFAULTS = dict(coins=['BTC'], delay_s=15, give_up_s=900, interval_s=15)


def valid_bar(rows, coin, start):
    """The bar as floats, or None unless exactly one bar for this coin, 1h, opening at `start` and closing at start+1h-1ms."""
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return None
    b = rows[0]
    if (b.get('s'), b.get('i'), b.get('t'), b.get('T')) != (coin, '1h', start * 1000, (start + HOUR) * 1000 - 1):
        return None
    vals = [C.num(b.get(k)) for k in ('o', 'h', 'l', 'c', 'v')]
    return None if None in vals or vals[3] <= 0 else vals


def tick(cfg, now):
    cfg = {**DEFAULTS, **cfg}
    feed = C.connect(cfg['markets_db'], C.FEED_SCHEMA)
    start = (int(now) // HOUR - 1) * HOUR            # the candle that has just closed
    phase = now - (start + HOUR)
    out = []
    for coin in cfg['coins']:
        if feed.execute("SELECT 1 FROM mk_perp_candles WHERE coin=? AND interval='1h' AND start_ts=?", (coin, start)).fetchone() or phase < cfg['delay_s']:
            continue
        bar = None
        try:
            bar = valid_bar(C.get_json(HL, {'type': 'candleSnapshot', 'req': dict(coin=coin, interval='1h', startTime=start * 1000, endTime=(start + HOUR) * 1000 - 1)}), coin, start)
        except Exception:
            bar = None
        if bar:
            feed.execute("INSERT INTO mk_perp_candles VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (coin, '1h', start, 'ok', *bar, now, 'hyperliquid:candleSnapshot', now))
            out.append('%s ok' % coin)
        elif phase > cfg['give_up_s']:
            feed.execute("INSERT INTO mk_perp_candles VALUES(?,?,?,?,NULL,NULL,NULL,NULL,NULL,?,?,NULL)", (coin, '1h', start, 'missed', now, 'hyperliquid:candleSnapshot'))
            out.append('%s missed' % coin)
        else:
            out.append('%s pending' % coin)
    detail = 'candle_start=%d %s' % (start, ' '.join(out) or 'nothing_due')
    C.status(feed, 'intel_hl', 'missed' not in detail, detail, now)
    return detail


def main(argv=None):
    return C.main_loop('markets-intel-hl', tick, argv, 15.0)


if __name__ == '__main__':
    raise SystemExit(main())
