"""Stocks feed host process: no-key public quotes, what the source project's stock desk read. `python3 -m sniper_office.markets.stocks --config stocks.json [--once]`.
Yahoo chart endpoint (one call per symbol): last price, its trade time, previous close, day range, volume and the regular session's start and end (so the books know market hours).
TradingView's unauthenticated screener (the source project's) adds context columns and the highest relative-volume large caps; its data can lag, so it is context and never a fill price.
A quote is only as fresh as its own trade time (`quote_ts`); a closed market shows an old `quote_ts` and the book refuses it."""
import json
import urllib.parse

from . import common as C

YAHOO = 'https://query1.finance.yahoo.com/v8/finance/chart/'
TV = 'https://scanner.tradingview.com/america/scan'
CORE = ('SPY', 'QQQ', 'NVDA', 'TSLA', 'AAPL', 'AMD', 'META', 'MSFT', 'AMZN', 'COIN', 'MSTR', 'PLTR')
COLUMNS = ('name', 'close', 'change', 'gap', 'premarket_change', 'RSI', 'Recommend.All', 'relative_volume_10d_calc', 'VWAP', 'ATR', 'EMA20', 'EMA50', 'high', 'low', 'sector')
DEFAULTS = dict(symbols=list(CORE), screen=True, screen_extra=10, retention_days=7, gap_s=0.2, interval_s=60)


def parse_quote(doc, symbol, now):
    """A mk_stock_quotes row (dict) from a Yahoo chart answer, or None."""
    try:
        meta = doc['chart']['result'][0]['meta']
    except (TypeError, KeyError, IndexError):
        return None
    if meta.get('symbol') not in (None, symbol):
        return None
    reg = (meta.get('currentTradingPeriod') or {}).get('regular') or {}
    price = C.num(meta.get('regularMarketPrice'))
    if price is None or price <= 0:
        return None
    return dict(symbol=symbol, price=price, quote_ts=C.num(meta.get('regularMarketTime')), prev_close=C.num(meta.get('chartPreviousClose')), day_high=C.num(meta.get('regularMarketDayHigh')),
                day_low=C.num(meta.get('regularMarketDayLow')), volume=C.num(meta.get('regularMarketVolume')), regular_start=C.num(reg.get('start')), regular_end=C.num(reg.get('end')),
                source='yahoo:chart', fetched_at=now)


def screen(extra):
    """Screener rows [{name, ...columns}] for the core list and the top relative-volume large caps."""
    out = {}
    bodies = [{'symbols': {'tickers': ['AMEX:SPY', 'NASDAQ:QQQ'] + ['NASDAQ:' + s for s in CORE[2:]]}, 'columns': list(COLUMNS)},
              {'filter': [{'left': 'market_cap_basic', 'operation': 'greater', 'right': 2e10}, {'left': 'volume', 'operation': 'greater', 'right': 3e6}, {'left': 'type', 'operation': 'equal', 'right': 'stock'}],
               'sort': {'sortBy': 'relative_volume_10d_calc', 'sortOrder': 'desc'}, 'range': [0, max(extra, 1)], 'columns': list(COLUMNS)}]
    for body in bodies:
        for row in (C.get_json(TV, body).get('data') or ()):
            d = dict(zip(COLUMNS, row.get('d') or ()))
            if d.get('name') and d.get('close'):
                out[d['name']] = d
    return list(out.values())


def tick(cfg, now, sleep=None):
    import time
    sleep = sleep or time.sleep
    cfg = {**DEFAULTS, **cfg}
    feed = C.connect(cfg['markets_db'], C.FEED_SCHEMA)
    symbols, rows_s, err = list(cfg['symbols']), [], None
    if cfg['screen']:
        try:
            rows_s = screen(cfg['screen_extra'])
            feed.executemany('INSERT INTO mk_stock_screen(symbol,data_json,source,fetched_at) VALUES(?,?,?,?)', [(d['name'], json.dumps(d), 'tradingview:scanner', now) for d in rows_s])
            symbols += [d['name'] for d in rows_s[-cfg['screen_extra']:] if d['name'] not in symbols and d['name'].isalpha()]
        except Exception as exc:
            err = 'screener %s: %s' % (type(exc).__name__, str(exc)[:100])
    ok = 0
    for sym in dict.fromkeys(symbols):
        try:
            q = parse_quote(C.get_json(YAHOO + urllib.parse.quote(sym) + '?interval=1m&range=1d&includePrePost=false'), sym, now)
        except Exception:
            q = None
        sleep(cfg['gap_s'])
        if q:
            cols = list(q)
            feed.execute('INSERT INTO mk_stock_quotes(%s) VALUES(%s)' % (','.join(cols), ','.join('?' * len(cols))), [q[c] for c in cols])
            ok += 1
    feed.execute('DELETE FROM mk_stock_quotes WHERE fetched_at<?', (now - 86400 * cfg['retention_days'],))
    feed.execute('DELETE FROM mk_stock_screen WHERE fetched_at<?', (now - 86400 * 2,))
    detail = 'quotes=%d of %d screen_rows=%d%s' % (ok, len(set(symbols)), len(rows_s), ' ' + err if err else '')
    C.status(feed, 'stocks', ok > 0, detail, now)
    return detail


def main(argv=None):
    return C.main_loop('markets-stocks', tick, argv, 60.0)


if __name__ == '__main__':
    raise SystemExit(main())
