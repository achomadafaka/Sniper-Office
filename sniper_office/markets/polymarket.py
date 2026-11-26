"""Polymarket feed host process: public Gamma (markets) and CLOB (order books) read APIs, no keys. `python3 -m sniper_office.markets.polymarket --config pm.json [--once]`.
Each tick: the most traded open binary markets and the YES and NO book of each, plus every market and token an open paper position holds (read from the ledger) so marks and resolution never lapse.
A book is stored with its best bid, best ask and the size at each, our fetch time and source. A crossed, empty or mismatched book is not stored. Universe filters follow the source desk's catalog (volume, spread, days to end).
Fee: the market's own `feeSchedule` when Gamma sends one, else `pm_fee_rate` from the config (default 0.05), with `fee_source` saying which. The fee per share is rate * p * (1 - p)."""
import json
import time
import urllib.parse
from datetime import datetime, timezone

from . import common as C, router as R

GAMMA = 'https://gamma-api.polymarket.com/markets'
EVENTS = 'https://gamma-api.polymarket.com/events'
BOOK = 'https://clob.polymarket.com/book?token_id='
XTRACKER = 'https://xtracker.polymarket.com/api/'
DEFAULTS = dict(top_n=40, min_volume24h=2000.0, max_spread=0.06, min_days_to_end=0.125, pm_fee_rate=0.05, gap_s=0.35, retention_days=7, interval_s=60, pages=1,
                event_tags=(), tag_pages=3, tag_top_n=120, tag_min_volume24h=100.0, trackers=(), tracker_history_days=60)


def _end(text):
    try:
        return datetime.fromisoformat(str(text).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def _list(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, list) else None


def parse_market(m, cfg, now):
    """A mk_pm_markets row (dict) for a binary Yes/No market with an order book, else None. Missing fields stay None."""
    if not isinstance(m, dict) or not m.get('conditionId') or not m.get('question'):
        return None
    tokens, outcomes, prices = _list(m.get('clobTokenIds')), _list(m.get('outcomes')), _list(m.get('outcomePrices'))
    if not tokens or len(tokens) != 2 or not outcomes or [str(o).lower() for o in outcomes] != ['yes', 'no']:
        return None
    sched = m.get('feeSchedule') if isinstance(m.get('feeSchedule'), dict) else {}
    rate, exp = C.num(sched.get('rate')), C.num(sched.get('exponent'))
    from_market = rate is not None
    closed = bool(m.get('closed'))
    p0 = C.num(prices[0]) if prices else None
    return dict(condition_id=m['conditionId'], question=str(m['question'])[:300], slug=m.get('slug'), end_ts=_end(m.get('endDate')), yes_token=str(tokens[0]), no_token=str(tokens[1]),
                fee_rate=rate if from_market else cfg['pm_fee_rate'], fee_exp=(exp or 1.0) if from_market else 1.0, fee_source='gamma_feeSchedule' if from_market else 'config_default',
                volume24h=C.num(m.get('volume24hr')), liquidity=C.num(m.get('liquidityNum')), closed=1 if closed else 0,
                accepting=1 if m.get('acceptingOrders', m.get('active')) and m.get('enableOrderBook', True) and not closed else 0, resolved_yes=p0 if closed and p0 in (0.0, 1.0) else None,
                rules=str(m.get('description') or '')[:1500], bid=C.num(m.get('bestBid')), ask=C.num(m.get('bestAsk')))


def parse_book(doc, token):
    """(bid, bid_size, ask, ask_size) from a CLOB book of `token`, or None when the book is not usable (wrong token, empty side, crossed)."""
    if not isinstance(doc, dict) or str(doc.get('asset_id')) != str(token):
        return None
    def best(side, pick):
        rows = [(C.num(r.get('price')), C.num(r.get('size'))) for r in doc.get(side) or () if isinstance(r, dict)]
        rows = [r for r in rows if r[0] is not None and r[1] is not None and r[1] > 0 and 0 < r[0] < 1]
        return pick(rows) if rows else (None, None)
    bid, ask = best('bids', lambda r: max(r)), best('asks', lambda r: min(r))
    if bid[0] is None or ask[0] is None or bid[0] >= ask[0]:
        return None
    return bid[0], bid[1], ask[0], ask[1]


def _store_market(feed, row, now):
    cols = ('condition_id', 'question', 'slug', 'end_ts', 'yes_token', 'no_token', 'fee_rate', 'fee_exp', 'fee_source', 'volume24h', 'liquidity', 'closed', 'accepting', 'resolved_yes', 'rules')
    room = R.classify(dict(source='pm', title=row['question']))['rooms']
    feed.execute('INSERT OR REPLACE INTO mk_pm_markets(%s,room,source,fetched_at) VALUES(%s)' % (','.join(cols), ','.join('?' * (len(cols) + 3))),
                 [row[c] for c in cols] + [max(room, key=lambda r: room[r]['score']), 'gamma:markets', now])


def _tagged(cfg, now, feed, have):
    """Open markets of events under each `event_tags` slug (rule-driven series: daily temperature, post counts), busiest first, outside the volume-ranked set.
    Event lists are paged small (one daily-temperature page of 20 is ~1.7 MB). A bucket priced outside 3-97 cents is left out: orders refuse it anyway."""
    rows = {}
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    for tag in cfg['event_tags']:
        for i in range(max(1, int(cfg['tag_pages']))):
            try:
                evs = C.get_json('%s?%s' % (EVENTS, urllib.parse.urlencode(dict(tag_slug=tag, closed='false', end_date_min=stamp, order='volume24hr', ascending='false', limit=10, offset=10 * i))))
            except Exception as exc:
                C.status(feed, 'polymarket_tags', False, '%s %s: %s' % (tag, type(exc).__name__, str(exc)[:100]), now)
                break
            if not isinstance(evs, list) or not evs:
                break
            for e in evs:
                for m in (e.get('markets') or ()) if isinstance(e, dict) else ():
                    row = parse_market(m, cfg, now)
                    if row and row['condition_id'] not in have and row['accepting'] and row['end_ts'] and row['end_ts'] > now \
                            and (row['volume24h'] or 0) >= cfg['tag_min_volume24h'] and row['ask'] is not None and 0.03 <= row['ask'] <= 0.97:
                        rows[row['condition_id']] = row
    return sorted(rows.values(), key=lambda r: -(r['volume24h'] or 0))[:int(cfg['tag_top_n'])]


def _trackers(cfg, now, feed):
    """Post-count windows from Polymarket's own tracker (the resolution source of the post-count markets): running total, pace and hourly counts.
    Active windows refresh every tick; a closed window is stored once with its final count, for base rates."""
    for handle in cfg['trackers']:
        try:
            for t in C.get_json(XTRACKER + 'users/%s/trackings' % urllib.parse.quote(handle)).get('data') or ():
                end = _end(t.get('endDate')) or 0
                if not t.get('isActive') and (end < now - 86400 * cfg['tracker_history_days'] or feed.execute('SELECT 1 FROM mk_pm_trackers WHERE tracking_id=? AND active=0', (t['id'],)).fetchone()):
                    continue
                st = (C.get_json(XTRACKER + 'trackings/%s?includeStats=true' % urllib.parse.quote(t['id'])).get('data') or {}).get('stats') or {}
                hourly = [[d.get('date'), d.get('count')] for d in st.get('daily') or () if isinstance(d, dict)]
                feed.execute('INSERT OR REPLACE INTO mk_pm_trackers VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (t['id'], handle, str(t.get('title'))[:200], _end(t.get('startDate')), end, 1 if t.get('isActive') else 0,
                             C.num(st.get('total')), C.num(st.get('pace')), json.dumps(hourly), str(t.get('marketLink') or '')[:300], 'xtracker', now))
        except Exception as exc:
            C.status(feed, 'polymarket_trackers', False, '%s %s: %s' % (handle, type(exc).__name__, str(exc)[:100]), now)


def _held(cfg):
    """(condition ids, token ids) of open paper positions, from the ledger when the config names one."""
    if not cfg.get('ledger_db'):
        return [], []
    try:
        led = C.ro(cfg['ledger_db'])
        rows = led.execute("SELECT instrument,meta_json FROM bk_positions WHERE lane='polymarket' AND status='open'").fetchall()
        led.close()
    except Exception:
        return [], []
    return [json.loads(r[1] or '{}').get('condition_id') for r in rows if r[1]], [r[0] for r in rows]


def tick(cfg, now, sleep=time.sleep):
    cfg = {**DEFAULTS, **cfg}
    feed = C.connect(cfg['markets_db'], C.FEED_SCHEMA)
    markets, held_c, held_t = {}, *_held(cfg)
    try:
        pages = [C.get_json('%s?%s' % (GAMMA, urllib.parse.urlencode(dict(active='true', closed='false', order='volume24hr', ascending='false', limit=100, offset=100 * i))))
                 for i in range(max(1, int(cfg['pages'])))]
        for m in (m for page in pages if isinstance(page, list) for m in page):
            row = parse_market(m, cfg, now)
            if row and (row['volume24h'] or 0) >= cfg['min_volume24h'] and row['accepting'] and row['end_ts'] and (row['end_ts'] - now) / 86400 >= cfg['min_days_to_end'] \
                    and (row['ask'] is None or row['bid'] is None or row['ask'] - row['bid'] <= cfg['max_spread']):
                markets[row['condition_id']] = row
    except Exception as exc:
        C.status(feed, 'polymarket', False, 'gamma %s: %s' % (type(exc).__name__, str(exc)[:120]), now)
        raise
    ranked = list(markets.values())[:cfg['top_n']]
    ranked += _tagged(cfg, now, feed, {r['condition_id'] for r in ranked})
    _trackers(cfg, now, feed)
    held_c = [c for c in dict.fromkeys(held_c) if c]
    if held_c:        # held markets by condition id, open and closed (Gamma hides closed ones unless asked): their closed flag and resolution price
        for extra in ('', 'closed=true&'):
            try:
                page = C.get_json('%s?%s%s' % (GAMMA, extra, '&'.join('condition_ids=' + urllib.parse.quote(c) for c in held_c[:50])))
                for m in page if isinstance(page, list) else ():
                    row = parse_market(m, cfg, now)
                    if row:
                        ranked.append(row)
            except Exception as exc:
                C.status(feed, 'polymarket_held', False, '%s: %s' % (type(exc).__name__, str(exc)[:120]), now)
    n_books = n_bad = 0
    tokens = []
    for row in ranked:
        _store_market(feed, row, now)
        tokens += [(row['condition_id'], row['yes_token']), (row['condition_id'], row['no_token'])]
    tokens += [(None, t) for t in held_t if t not in {t2 for _, t2 in tokens}]
    for cid, token in tokens:
        try:
            q = parse_book(C.get_json(BOOK + urllib.parse.quote(token)), token)
        except Exception:
            q = None
        sleep(cfg['gap_s'])
        if q is None:
            n_bad += 1
            continue
        feed.execute('INSERT INTO mk_pm_quotes(token_id,condition_id,bid,bid_size,ask,ask_size,source,fetched_at) VALUES(?,?,?,?,?,?,?,?)', (token, cid, *q, 'clob:book', now))
        n_books += 1
    feed.execute('DELETE FROM mk_pm_quotes WHERE fetched_at<?', (now - 86400 * cfg['retention_days'],))
    detail = 'markets=%d books_ok=%d books_unusable=%d held_markets=%d' % (len(ranked), n_books, n_bad, len(held_c))
    C.status(feed, 'polymarket', n_books > 0, detail, now)
    return detail


def main(argv=None):
    return C.main_loop('markets-polymarket', tick, argv, 60.0)


if __name__ == '__main__':
    raise SystemExit(main())
