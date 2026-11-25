"""Every desk test runs against a fake market provider: no test reaches the network. Knobs are plain attributes; calls are recorded."""
import time

import pytest

from sniper_office.desk import market as M

MINT, POOL = 'MintAAAA' + '1' * 34, 'PooBBBB' + '2' * 35


class FakeMarket:
    def __init__(self):
        self.down, self.calls, self.status, self.trades_ago, self.trade_cap_full = True, [], {}, [5, 20, 70], False
        self.price = '0.0001'
        self.pair = dict(chainId='solana', dexId='pumpswap', pairAddress=POOL, baseToken=dict(address=MINT), quoteToken=dict(address='So11111111111111111111111111111111111111112', symbol='SOL'), priceUsd='0.0001',
                         priceNative='0.0000007', priceChange=dict(m5=1.5, h1=20.0, h6=30.0, h24=40.0), volume=dict(m5=100., h1=1000., h6=2000., h24=3000.),
                         txns=dict(m5=dict(buys=5, sells=4), h1=dict(buys=50, sells=40), h6=dict(buys=60, sells=50), h24=dict(buys=70, sells=60)), liquidity=dict(usd=9000., base=1e6, quote=40.),
                         fdv=200000, marketCap=200000, pairCreatedAt=(time.time() - 600) * 1000)

    def __call__(self, url, body=None):
        host = url.split('/')[2]
        self.calls.append(url)
        if self.status.get(host):
            return self.status[host], None, 30.
        if self.down:
            return None, None, None
        if '/latest/dex/pairs/solana/' in url or '/latest/dex/tokens/' in url:
            return 200, dict(pairs=[dict(self.pair, priceUsd=self.price)]), None
        if url.endswith('/trades'):
            now = time.time()
            iso = lambda a: time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now - a))
            rows = [dict(attributes=dict(block_timestamp=iso(a), kind='buy' if i % 2 == 0 else 'sell', volume_in_usd='10.5')) for i, a in enumerate(self.trades_ago)]
            return 200, dict(data=rows * (300 // max(len(rows), 1)) if self.trade_cap_full else rows), None
        return 404, None, None


@pytest.fixture(autouse=True)
def market(monkeypatch):
    fake = FakeMarket()
    monkeypatch.setattr(M, 'FETCH', fake)
    monkeypatch.setattr(M, 'INTERVALS', dict(dexscreener=0, geckoterminal=0, jupiter=0, rpc=0))
    monkeypatch.setattr(M, '_NET', None)
    return fake
