"""Markets plug-in tests: no test reaches the network (C.FETCH is replaced by a fake that raises on an unknown url) and every clock is explicit."""
import json

import pytest

from sniper_office.markets import common as C

NOW = 1_800_000_000.0
COND, YES, NO = '0xcond1', 'tokyes', 'tokno'


class Fake:
    """url prefix -> JSON-able answer (or bytes, or an Exception to raise); calls recorded."""
    def __init__(self):
        self.routes, self.calls = {}, []

    def __call__(self, url, body=None, *a, **k):
        self.calls.append((url, body))
        for prefix, answer in self.routes.items():
            if url.startswith(prefix) and (not isinstance(answer, tuple) or answer[0] is None or answer[0] == (body or {}).get('type')):
                answer = answer[1] if isinstance(answer, tuple) else answer
                if isinstance(answer, Exception):
                    raise answer
                return answer if isinstance(answer, bytes) else json.dumps(answer).encode()
        raise AssertionError('unexpected fetch ' + url)


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    monkeypatch.setattr(C, 'FETCH', f)
    return f


@pytest.fixture
def dbs(tmp_path):
    return C.connect(tmp_path / 'feed.db', C.FEED_SCHEMA), C.connect(tmp_path / 'ledger.db', C.LEDGER_SCHEMA), tmp_path


def put_market(feed, now=NOW, closed=0, resolved=None, end=NOW + 86400, rate=0.05, accepting=1):
    feed.execute('INSERT OR REPLACE INTO mk_pm_markets(condition_id,question,slug,room,end_ts,yes_token,no_token,fee_rate,fee_exp,fee_source,volume24h,liquidity,closed,accepting,resolved_yes,rules,source,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                 (COND, 'Will the Fed cut rates in March?', 's', 'perps', end, YES, NO, rate, 1.0, 'test', 50000., 1e5, closed, accepting, resolved, 'rules', 'test', now))


def put_quote(feed, token, bid, ask, at=NOW, bid_size=500., ask_size=500.):
    feed.execute('INSERT INTO mk_pm_quotes(token_id,condition_id,bid,bid_size,ask,ask_size,source,fetched_at) VALUES(?,?,?,?,?,?,?,?)', (token, COND, bid, bid_size, ask, ask_size, 'clob:book', at))


def put_perp(feed, coin='BTC', mid=100.0, funding=0.0001, at=NOW, max_lev=20.0, venue='hyperliquid', funding_h=1.0):
    feed.execute('INSERT INTO mk_perp_ticks(venue,coin,mark,mid,oracle,funding,funding_h,oi,day_vol,prev_day,max_leverage,source,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                 (venue, coin, mid, mid, mid, funding, funding_h, 1000., 1e9, mid * 0.99, max_lev, 'hl:test', at))


def put_stock(feed, sym='NVDA', price=100.0, at=NOW, start=NOW - 3600, end=NOW + 4 * 3600):
    feed.execute('INSERT INTO mk_stock_quotes(symbol,price,quote_ts,prev_close,day_high,day_low,volume,regular_start,regular_end,source,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)', (sym, price, at, 99., 101., 98., 1e6, start, end, 'yahoo:test', at))
