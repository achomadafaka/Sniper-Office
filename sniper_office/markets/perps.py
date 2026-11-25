"""Perps feed host process: Hyperliquid public info API (no key): mark, mid, oracle, funding (hourly rate), open interest, 24h volume, max leverage per coin. `python3 -m sniper_office.markets.perps --config perps.json [--once]`.
Optional second venue "binance" (USDT-M futures public data, what the source project's perps desk read): mark, book-ticker mid, 8-hour funding. Each row carries source and our fetch time; a value the venue does not send is NULL.
Also here: the hourly candle recorder (`intel_hl.py` runs it as its own process), the port of the source project's Hyperliquid intel service."""
from . import common as C

HL = 'https://api.hyperliquid.xyz/info'
BN = 'https://fapi.binance.com/fapi/v1/'
DEFAULTS = dict(top_n=60, coins=['BTC', 'ETH', 'SOL'], venues=['hyperliquid'], retention_days=3, interval_s=30)


def parse_hl(doc, now, top_n, coins, held=()):
    """Rows (dicts) from a metaAndAssetCtxs answer: the top_n by day volume plus `coins` and held ones."""
    try:
        universe, ctxs = doc[0]['universe'], doc[1]
    except (TypeError, IndexError, KeyError):
        return []
    rows = []
    for u, c in zip(universe, ctxs):
        if not isinstance(u, dict) or not isinstance(c, dict) or u.get('isDelisted'):
            continue
        rows.append(dict(venue='hyperliquid', coin=u['name'], mark=C.num(c.get('markPx')), mid=C.num(c.get('midPx')), oracle=C.num(c.get('oraclePx')), funding=C.num(c.get('funding')), funding_h=1.0,
                         oi=C.num(c.get('openInterest')), day_vol=C.num(c.get('dayNtlVlm')), prev_day=C.num(c.get('prevDayPx')), max_leverage=C.num(u.get('maxLeverage')),
                         source='hyperliquid:metaAndAssetCtxs', fetched_at=now))
    keep = {r['coin'] for r in sorted(rows, key=lambda r: -(r['day_vol'] or 0))[:top_n]} | set(coins) | set(held)
    return [r for r in rows if r['coin'] in keep]


def parse_binance(day, prem, book, now, top_n, coins, held=()):
    prem = {d['symbol']: d for d in prem if isinstance(d, dict) and 'symbol' in d}
    book = {d['symbol']: d for d in book if isinstance(d, dict) and 'symbol' in d}
    rows = []
    for d in day:
        s = d.get('symbol', '')
        if not s.endswith('USDT') or s not in prem:
            continue
        b = book.get(s) or {}
        bid, ask = C.num(b.get('bidPrice')), C.num(b.get('askPrice'))
        rows.append(dict(venue='binance', coin=s, mark=C.num(prem[s].get('markPrice')), mid=(bid + ask) / 2 if bid and ask else None, oracle=C.num(prem[s].get('indexPrice')),
                         funding=C.num(prem[s].get('lastFundingRate')), funding_h=8.0, oi=None, day_vol=C.num(d.get('quoteVolume')), prev_day=None, max_leverage=None,
                         source='binance:fapi', fetched_at=now))
    keep = {r['coin'] for r in sorted(rows, key=lambda r: -(r['day_vol'] or 0))[:top_n]} | set(coins) | set(held)
    return [r for r in rows if r['coin'] in keep]


def _held(cfg):
    if not cfg.get('ledger_db'):
        return []
    try:
        led = C.ro(cfg['ledger_db'])
        rows = [r[0].split(':', 1)[-1] for r in led.execute("SELECT instrument FROM bk_positions WHERE lane='perps' AND status='open'")]
        led.close()
        return rows
    except Exception:
        return []


def tick(cfg, now):
    cfg = {**DEFAULTS, **cfg}
    feed = C.connect(cfg['markets_db'], C.FEED_SCHEMA)
    held, rows, errors = _held(cfg), [], []
    for venue in cfg['venues']:
        try:
            if venue == 'hyperliquid':
                rows += parse_hl(C.get_json(HL, {'type': 'metaAndAssetCtxs'}), now, cfg['top_n'], cfg['coins'], held)
            elif venue == 'binance':
                rows += parse_binance(C.get_json(BN + 'ticker/24hr'), C.get_json(BN + 'premiumIndex'), C.get_json(BN + 'ticker/bookTicker'), now, cfg['top_n'],
                                      [c + 'USDT' if not c.endswith('USDT') else c for c in cfg['coins']], held)
        except Exception as exc:
            errors.append('%s %s: %s' % (venue, type(exc).__name__, str(exc)[:100]))
    cols = ('venue', 'coin', 'mark', 'mid', 'oracle', 'funding', 'funding_h', 'oi', 'day_vol', 'prev_day', 'max_leverage', 'source', 'fetched_at')
    feed.executemany('INSERT INTO mk_perp_ticks(%s) VALUES(%s)' % (','.join(cols), ','.join('?' * len(cols))), [[r[c] for c in cols] for r in rows])
    feed.execute('DELETE FROM mk_perp_ticks WHERE fetched_at<?', (now - 86400 * cfg['retention_days'],))
    detail = 'ticks=%d%s' % (len(rows), ' errors=' + '; '.join(errors) if errors else '')
    C.status(feed, 'perps', bool(rows), detail, now)
    if not rows and errors:
        raise RuntimeError(detail)
    return detail


def main(argv=None):
    return C.main_loop('markets-perps', tick, argv, 30.0)


if __name__ == '__main__':
    raise SystemExit(main())
