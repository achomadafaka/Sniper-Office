"""On-chain price source: decoders against recorded live account bytes (tests/desk/fixtures/onchain_sol.json, recorded 2026-11-25 beside DexScreener's price for the same pool),
pool math against hand-computed reserves, Multicall3 framing, the websocket/poll plumbing against fakes, batching, refusal and fallback. No test reaches the network."""
import base64
import json
import sqlite3
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_j7_copy import EVM, EVM2, NOW, Dex, TRADERS, listener, recorder, trade  # noqa: E402, F401

from sniper_office.desk import onchain as O, ticks as TK  # noqa: E402

FIX = json.loads((Path(__file__).parent / 'fixtures' / 'onchain_sol.json').read_text())
RAW = {k: {a: base64.b64decode(b) for a, b in v['accts'].items()} for k, v in FIX.items()}


def pool_of(key):
    v = FIX[key]
    return dict(kind=v['kind'], pk=v['pk'], base=v['base'], quote=v['quote'], dec=tuple(v['dec']), vaults=tuple(v['vaults']) if v['vaults'] else None)


# ---- Solana decoders: recorded accounts -> price ratio ----------------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('key', sorted(FIX))
def test_each_pool_type_decodes_from_recorded_account_bytes_to_the_providers_price(key):
    v = FIX[key]
    got = O.decode_pool(v['kind'], RAW[key][v['pk']])
    assert got is not None and (got['base'] in (v['base'], None)) and got['quote'] == v['quote']
    r = O.ratio(pool_of(key), RAW[key])
    # the provider's own price for the same pool, taken seconds apart; the curve moves fastest so it is also pinned by hand below
    assert r == pytest.approx(v['expect_ratio'], rel=.12 if v['kind'] == 'pumpfun' else .03)


def test_the_bonding_curve_price_is_virtual_sol_over_virtual_tokens_with_decimals():
    key = [k for k in FIX if k.startswith('pumpfun')][0]
    d = RAW[key][FIX[key]['pk']]
    vt, vs = struct.unpack_from('<QQ', d, 8)
    assert O.ratio(pool_of(key), RAW[key]) == (vs / 1e9) / (vt / 1e6)


def test_the_cpmm_price_excludes_the_fees_held_in_its_vaults():
    key = [k for k in FIX if k.startswith('raydium_cpmm')][0]
    p = pool_of(key)
    f0, f1, g0, g1 = struct.unpack_from('<4Q', RAW[key][p['pk']], 341)
    b, q = (O.token_amount(RAW[key][v]) for v in p['vaults'])
    assert f1 + g1 > 0 and O.ratio(p, RAW[key]) == ((q - f1 - g1) / 10 ** p['dec'][1]) / ((b - f0 - g0) / 10 ** p['dec'][0])


def test_a_vault_pool_price_is_quote_balance_over_base_balance():
    key = [k for k in FIX if k.startswith('pumpswap')][0]
    p = pool_of(key)
    b, q = (O.token_amount(RAW[key][v]) for v in p['vaults'])
    assert O.ratio(p, RAW[key]) == (q / 10 ** p['dec'][1]) / (b / 10 ** p['dec'][0])


def test_a_decoder_refuses_bytes_that_are_not_its_account():
    key = [k for k in FIX if k.startswith('pumpfun')][0]
    d = bytearray(RAW[key][FIX[key]['pk']])
    assert O.decode_pool('pumpfun', bytes(d)) is not None
    done = bytearray(d)
    done[48] = 1   # graduated curve: its reserves are zero, the pool lives elsewhere
    assert O.decode_pool('pumpfun', bytes(done)) is None and O.decode_pool('pumpfun', bytes(d[:20])) is None
    assert O.decode_pool('pumpfun', b'\0' * 8 + bytes(d[8:])) is None   # wrong discriminator
    swap = [k for k in FIX if k.startswith('pumpswap')][0]
    assert O.decode_pool('pumpswap', RAW[swap][FIX[swap]['pk']][:100]) is None and O.decode_pool('nope', b'x' * 2000) is None
    assert O.ratio(dict(kind='clmm', pk='p', dec=(9, 6), vaults=None), {}) is None   # missing account


def test_base58_matches_known_pubkeys():
    key = [k for k in FIX if k.startswith('pumpswap')][0]
    got = O.decode_pool('pumpswap', RAW[key][FIX[key]['pk']])
    assert got['quote'] == FIX[key]['quote'] and O.b58(b'\0\0\1') == '112'


# ---- EVM: Multicall3 framing and pair math ----------------------------------------------------------------------------------------------------------------------------------------------

def word(n):
    return n.to_bytes(32, 'big')


def test_aggregate3_calldata_has_the_abi_layout_and_replies_decode():
    mc = O.MULTICALL3
    data = O.encode_aggregate3([(mc, O.SEL['block'])])
    want = '0x82ad56cb' + (word(32) + word(1) + word(32) + bytes.fromhex(mc[2:]).rjust(32, b'\0') + word(1) + word(96) + word(4) + bytes.fromhex('42cbb15c').ljust(32, b'\0')).hex()
    assert data == want
    # return: Result[] = [(true, 0x2a), (false, empty)]
    res = word(32) + word(2) + word(64) + word(64 + 128) + word(1) + word(64) + word(32) + word(42) + word(0) + word(64) + word(0)
    assert O.decode_aggregate3('0x' + res.hex()) == [(True, word(42)), (False, b'')]
    assert O.decode_aggregate3('0x12') is None


def test_v2_and_v3_pair_math():
    # v2: 2000 WETH-like (18 dec) against 1,000,000 USDC-like (6 dec) -> 500 quote per base
    assert O.evm_ratio('v2', word(2000 * 10 ** 18) + word(1_000_000 * 10 ** 6) + word(0), (18, 6)) == pytest.approx(500.)
    # v3: sqrtPriceX96 = sqrt(price_raw) * 2^96, price_raw = 500 * 10^(6-18)
    sq = int((500 * 10 ** (6 - 18)) ** .5 * 2 ** 96)
    assert O.evm_ratio('v3', word(sq), (18, 6)) == pytest.approx(500., rel=1e-9)
    assert O.evm_ratio('v2', word(0) + word(5) + word(0), (18, 6)) is None and O.evm_ratio('v3', word(0), (18, 6)) is None and O.evm_ratio('x', b'', (1, 1)) is None


# ---- OnChain against fakes ---------------------------------------------------------------------------------------------------------------------------------------------------------

class FakeSolRpc:
    def __init__(self, key, price_scale=1.):
        self.key, self.calls = key, []
        self.raw = {a: v for a, v in RAW[key].items()}

    def __call__(self, url, body):
        self.calls.append(body['method'])
        keys = body['params'][0]
        vals = []
        for k in keys:
            if k in self.raw:
                owner = next((o for o, kind in O.PROG.items() if kind == FIX[self.key]['kind']), 'x') if k == FIX[self.key]['pk'] else 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
                vals.append(dict(owner=owner, data=[base64.b64encode(self.raw[k]).decode(), 'base64']))
            else:
                vals.append(None)
        return dict(result=dict(context=dict(slot=1000), value=vals))


def dex_pair(key, scale=1.):
    v = FIX[key]
    native = v['expect_ratio'] if v['dex_base'] == v['base'] else 1 / v['expect_ratio']
    other_usd = 100.
    return dict(pairAddress=v['pk'], quoteToken=dict(address=v['quote'] if v['dex_base'] == v['base'] else v['base']), priceNative=str(native * scale), priceUsd=str(native * scale * other_usd), liquidity=dict(usd=50000.))


@pytest.mark.parametrize('key', sorted(FIX))
def test_resolve_reads_the_pool_once_and_prices_the_watched_token_in_usd(key):
    rpc = FakeSolRpc(key)
    oc = O.OnChain(dict(sol_rpc_url='https://rpc.invalid/'), rpc=rpc, start=False)
    token = FIX[key]['dex_base']
    assert oc.resolve('solana', token, dex_pair(key)) is True
    e = oc.entries[('solana', token)]
    assert oc.price(e) == pytest.approx(float(dex_pair(key)['priceUsd']), rel=.12 if key.startswith('pumpfun') else .03)
    assert rpc.calls.count('getMultipleAccounts') <= 2          # one for the pool, one for vaults and mints
    assert oc.read('solana', token) is None                      # no websocket yet: not healthy, so the recorder keeps the provider
    oc.ws_ok = True
    r = oc.read('solana', token)
    assert r['source'] == 'onchain:' + FIX[key]['kind'] and r['slot'] == 1000 and r['liquidity_usd'] == 50000.


def test_a_decoded_price_far_from_the_providers_is_refused_and_unsupported_owners_are_skipped():
    key = [k for k in FIX if k.startswith('clmm')][0]
    oc = O.OnChain(dict(sol_rpc_url='https://rpc.invalid/'), rpc=FakeSolRpc(key), start=False)
    assert oc.resolve('solana', FIX[key]['dex_base'], dex_pair(key, scale=3.)) is False and not oc.entries     # provider says 3x: a misread layout never becomes a price
    rpc = FakeSolRpc(key)
    rpc.raw = {}
    assert O.OnChain(dict(sol_rpc_url='https://rpc.invalid/'), rpc=rpc, start=False).resolve('solana', FIX[key]['dex_base'], dex_pair(key)) is False   # account missing / unknown program
    assert O.OnChain(dict(sol_rpc_url=None), rpc=rpc, start=False).resolve('solana', FIX[key]['dex_base'], dex_pair(key)) is False                        # no RPC configured


def test_account_notifications_move_the_price_by_slot_and_a_stale_slot_is_ignored():
    key = [k for k in FIX if k.startswith('raydium_v4')][0]
    p = pool_of(key)
    oc = O.OnChain(dict(sol_rpc_url='https://rpc.invalid/'), rpc=FakeSolRpc(key), start=False)
    token = FIX[key]['dex_base']
    assert oc.resolve('solana', token, dex_pair(key))
    oc.ws_ok = True
    e = oc.entries[('solana', token)]
    before = oc.price(e)
    base_vault = p['vaults'][0]
    oc.sub = {7: base_vault}
    subid = {}
    oc.on_message(json.dumps(dict(jsonrpc='2.0', result=55, id=7)), subid)
    assert subid == {55: base_vault}

    def note(slot, base_amount):
        raw = bytearray(RAW[key][base_vault])
        struct.pack_into('<Q', raw, 64, base_amount)
        return json.dumps(dict(method='accountNotification', params=dict(subscription=55, result=dict(context=dict(slot=slot), value=dict(data=[base64.b64encode(bytes(raw)).decode(), 'base64'])))))
    orig = O.token_amount(RAW[key][base_vault])
    oc.on_message(note(2000, orig // 2), subid)                 # half the base in the pool: the base token is worth twice as much in quote
    assert oc.price(e) == pytest.approx(before * 2, rel=1e-6) and e.slot == 2000
    oc.on_message(note(1500, orig * 10), subid)                 # an older slot arriving late
    assert oc.price(e) == pytest.approx(before * 2, rel=1e-6) and e.slot == 2000
    oc.on_message('not json', subid), oc.on_message(json.dumps([1]), subid)   # junk frames are dropped


def evm_reply(results, block=123, ts=1_700_000_000):
    out = [(True, word(block)), (True, word(ts))] + results
    n = len(out)
    heads, bodies, off = [], [], 32 * n
    for ok, data in out:
        body = word(int(ok)) + word(64) + word(len(data)) + data.ljust(-(-len(data) // 32) * 32, b'\0')
        heads.append(word(off))
        bodies.append(body)
        off += len(body)
    return '0x' + (word(32) + word(n) + b''.join(heads) + b''.join(bodies)).hex()


class FakeEvm:
    """A WETH(18)/memecoin(18) v2 pair: token0 = coin, token1 = WETH; reserves are mutable."""
    COIN, WETH = '0x' + 'c0' * 20, '0x' + 'e7' * 20

    def __init__(self):
        self.r0, self.r1, self.calls, self.down = 1_000_000 * 10 ** 18, 10 * 10 ** 18, 0, False

    def __call__(self, url, body):
        self.calls += 1
        if self.down:
            return None
        data = bytes.fromhex(body['params'][0]['data'][10:])
        n = int.from_bytes(data[32:64], 'big')
        arr, sels = 64, []
        for i in range(n):
            o = arr + int.from_bytes(data[arr + 32 * i:arr + 32 * i + 32], 'big')
            sels.append((data[o + 12:o + 32].hex(), data[o + 128:o + 132].hex()))
        res = []
        for tgt, sel in sels[2:]:
            res.append({'0dfe1681': (True, word(int(self.COIN, 16))), 'd21220a7': (True, word(int(self.WETH, 16))), '0902f1ac': (True, word(self.r0) + word(self.r1) + word(0)),
                        '3850c7bd': (False, b''), '313ce567': (True, word(18))}[sel])
        return dict(result=evm_reply(res))


def evm_dex_pair(price_usd=3000., coin_usd=0.03):
    return dict(pairAddress='0x' + '99' * 20, quoteToken=dict(address=FakeEvm.WETH), priceUsd=str(coin_usd), priceNative=str(coin_usd / price_usd), liquidity=dict(usd=60000.))


def test_an_evm_v2_pair_resolves_prices_from_reserves_and_follows_each_poll_with_block_and_timestamp():
    f = FakeEvm()
    oc = O.OnChain(dict(evm_rpc=dict(base='https://rpc.invalid/')), rpc=f, start=False)
    assert oc.resolve('base', FakeEvm.COIN, evm_dex_pair()) is True
    r = oc.read('base', FakeEvm.COIN)
    assert r['price_usd'] == pytest.approx(10 / 1e6 * 3000.) and r['slot'] == 123 and r['chain_ts'] == 1_700_000_000. and r['source'] == 'onchain:v2'
    f.r1 = 20 * 10 ** 18                                        # the pool doubles in WETH: the coin doubles
    res = oc._evm_multicall('u', [(oc.entries[('base', FakeEvm.COIN)].pool['pk'], O.SEL['reserves'])])
    e = oc.entries[('base', FakeEvm.COIN)]
    oc._evm_apply(e, res[0][1], res[1][1], res[2][1])
    assert oc.price(e) == pytest.approx(2 * 10 / 1e6 * 3000.)
    assert f.calls == 3                                          # resolve = probe + decimals; one more for the poll
    oc.evm_ok['base'] -= 100.                                    # the poller has not succeeded for a long time: not healthy
    assert oc.read('base', FakeEvm.COIN) is None


def test_an_evm_token_that_is_not_in_the_pair_or_a_dead_rpc_does_not_resolve():
    f = FakeEvm()
    oc = O.OnChain(dict(evm_rpc=dict(base='https://rpc.invalid/')), rpc=f, start=False)
    assert oc.resolve('base', '0x' + 'aa' * 20, evm_dex_pair()) is False
    f.down = True
    assert oc.resolve('base', FakeEvm.COIN, evm_dex_pair()) is False and oc.resolve('nochain', FakeEvm.COIN, evm_dex_pair()) is False


def test_add_is_queued_once_and_a_failed_pool_is_not_retried_inside_retry_s():
    oc = O.OnChain(dict(evm_rpc=dict(base='https://rpc.invalid/')), rpc=FakeEvm(), start=False, clock=lambda: NOW)
    oc.add('base', FakeEvm.COIN, evm_dex_pair())
    oc.add('base', FakeEvm.COIN, evm_dex_pair())
    assert oc.todo.qsize() == 1


# ---- recorder: on-chain first, provider fallback ----------------------------------------------------------------------------------------------------------------------------------------

class StubOC:
    def __init__(self):
        self.ok, self.price, self.added, self.entries = False, 1.5, [], {}
        self.slot = 10
        self.stats = {}

    def sync(self, keys):
        pass

    def healthy(self, chain, token):
        return self.ok

    def read(self, chain, token):
        return dict(price_usd=self.price, liquidity_usd=50000., source='onchain:v2', slot=self.slot, seen_at=NOW, chain_ts=NOW - 1.) if self.ok else None

    def refresh_ref(self, chain, token, pair):
        return None if not self.entries else dict(onchain=self.price, dex=float(pair['priceUsd']), quote_usd=3000., slot=self.slot)

    def add(self, chain, token, pair):
        self.added.append((chain, token))


def watched_rec(tmp_path, oc, clock, dex):
    lis = listener(tmp_path, now=NOW, watch_after_signal_s=600.)
    lis.handle(0, 'fomo_event', trade())
    return recorder(tmp_path, dex, clock, onchain=False, heartbeat_s=1.) if False else TK.Recorder(dict(j7_db=str(tmp_path / 'j7.db'), min_interval_s=0, onchain=True), fetch=dex, clock=lambda: clock[0], sleep=lambda s: None, onchain=oc)


def test_a_healthy_onchain_coin_is_ticked_from_the_pool_with_slot_and_the_provider_is_only_the_reference(tmp_path):
    clock, dex, oc = [NOW + 1], Dex(), StubOC()
    rec = watched_rec(tmp_path, oc, clock, dex)
    rec.cycle()                                                   # unresolved: provider tick, and the pool is offered for resolution
    assert oc.added == [('base', EVM)] and rec.db.execute('SELECT source FROM ticks').fetchall() == [(TK.SOURCE,)]
    oc.ok, oc.entries = True, {('base', EVM): 1}
    n_calls = len(dex.calls)
    rec.cycle()                                                   # first healthy cycle: one reference poll (no tick row from it), recorded for the cross-check
    assert rec.db.execute('SELECT count(*) FROM ticks').fetchone() == (1,) and rec.db.execute('SELECT count(*) FROM onchain_ref').fetchone() == (1,)
    clock[0] += 1
    n_calls = len(dex.calls)
    rec.cycle()                                                   # inside ref_s: no provider request at all
    assert len(dex.calls) == n_calls
    assert rec.flush() == 1
    row = rec.db.execute('SELECT source,price_usd,slot,seen_at,chain_ts,changed,liquidity_usd FROM ticks ORDER BY id DESC LIMIT 1').fetchone()
    assert row == ('onchain:v2', 1.5, 10, NOW, NOW - 1., 1, 50000.)
    assert rec.flush() == 0                                       # unchanged and inside the heartbeat: nothing
    clock[0] += 1.0
    assert rec.flush() == 1 and rec.db.execute('SELECT changed FROM ticks ORDER BY id DESC LIMIT 1').fetchone() == (0,)   # heartbeat: same price, pool feed alive
    oc.price, oc.slot = 1.6, 11
    clock[0] += .25
    assert rec.flush() == 1 and rec.db.execute('SELECT price_usd,slot,changed FROM ticks ORDER BY id DESC LIMIT 1').fetchone() == (1.6, 11, 1)
    got = TK.latest(rec.db, 'base', EVM, clock[0], 5.)
    assert got['source'] == 'onchain:v2' and got['price_usd'] == 1.6 and got['slot'] == 11 and got['provider'] == 'onchain'


def test_when_the_onchain_feed_goes_unhealthy_the_provider_is_polled_again_every_cycle(tmp_path):
    clock, dex, oc = [NOW + 1], Dex(), StubOC()
    oc.ok, oc.entries = True, {('base', EVM): 1}
    rec = watched_rec(tmp_path, oc, clock, dex)
    rec.cycle()
    rec.flush()
    clock[0] += 1
    oc.ok = False                                                 # websocket / rpc down
    before = len(dex.calls)
    rec.cycle()
    assert rec.flush() == 0 and len(dex.calls) > before
    assert rec.db.execute('SELECT source FROM ticks ORDER BY id DESC LIMIT 1').fetchone() == (TK.SOURCE,)


def test_exit_path_records_price_source_slot_modeled_value_peak_and_drawdown_per_cycle(tmp_path):
    clock, dex = [NOW + 1], Dex()
    rec = recorder(tmp_path, dex, clock)
    pos = lambda v, at: [dict(state='priced', order_id='o1', chain='base', mint=EVM, price_usd=v / 100, tick_source='onchain:v2', tick_slot=77, value_usd=v, filled_at=at)]
    rec.exit_path(pos(100., NOW), NOW + 1)
    rec.exit_path(pos(150., NOW), NOW + 2)
    rec.exit_path(pos(120., NOW), NOW + 3)
    rec.exit_path([dict(state='unpriced', order_id='o1')], NOW + 4)   # an unpriced position writes nothing
    rows = rec.db.execute('SELECT price_usd,source,slot,net_usd,peak_usd,round(drawdown_pct,4),covered_from_fill FROM exit_path ORDER BY at').fetchall()
    assert rows == [(1.0, 'onchain:v2', 77, 100., 100., 0., 1), (1.5, 'onchain:v2', 77, 150., 150., 0., 1), (1.2, 'onchain:v2', 77, 120., 150., -20., 1)]
    late = recorder(tmp_path, dex, clock)
    late.exit_path([dict(pos(100., NOW - 60)[0], order_id='o2')], NOW + 1)   # first seen a minute after the fill: the peak does not cover the fill
    assert late.db.execute("SELECT covered_from_fill FROM exit_path WHERE order_id='o2'").fetchone() == (0,)


def test_an_unmigrated_ticks_db_is_migrated_and_old_rows_still_read(tmp_path):
    path = tmp_path / 'old.db'
    c = sqlite3.connect(path)
    c.execute('CREATE TABLE ticks(id INTEGER PRIMARY KEY, at REAL NOT NULL, chain TEXT NOT NULL, token TEXT NOT NULL, pair TEXT, price_usd REAL NOT NULL, liquidity_usd REAL, source TEXT NOT NULL, changed INTEGER NOT NULL)')
    c.execute("INSERT INTO ticks(at,chain,token,pair,price_usd,liquidity_usd,source,changed) VALUES(?,?,?,?,?,?,?,?)", (NOW, 'base', EVM, 'p', 2., 1e4, TK.SOURCE, 1))
    c.commit()
    assert TK.latest(c, 'base', EVM, NOW + 1, 5.)['slot'] is None      # reader on the old shape (the recorder has not restarted yet)
    c.close()
    c = TK.open_db(str(path))
    assert 'slot' in {r[1] for r in c.execute('PRAGMA table_info(ticks)')} and TK.latest(c, 'base', EVM, NOW + 1, 5.)['price_usd'] == 2.
