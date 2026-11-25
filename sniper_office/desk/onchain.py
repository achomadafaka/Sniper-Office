"""On-chain pool prices for the desk recorder (ticks.py): the primary price source, DexScreener the fallback. Stdlib only, read-only, paper only.

Solana: the pool (or its two token vaults) is read once with getMultipleAccounts, then followed by websocket accountSubscribe (a push on every change, same host as the research RPC).
  Decoded: PumpSwap AMM, Raydium AMM v4 / CPMM / CLMM (and the PancakeSwap CLMM fork), Orca Whirlpool, Meteora DLMM, pump.fun bonding curve. Anything else (DAMM, DBC, ...) is unsupported -> DexScreener.
EVM: Uniswap v2-style pairs (getReserves) and v3-style pools (slot0 sqrtPriceX96), one Multicall3 aggregate3 per chain per poll (blockNumber + block timestamp in the same call), over the public JSON-RPC
  endpoints in `evm_rpc`. ponytail: polling at `evm_poll_s` (1 s) not newHeads/logs wss; upgrade to a subscription if a chain's block time drops under the poll.
USD: the pool gives (other token per watched token). The other token's USD price is a cached reference from DexScreener's own pair (priceUsd / priceNative), refreshed every `ref_s` (30 s) while the pair
  is watched. Quote tokens move slowly against a memecoin; the ratio, which moves with every swap, is on chain. A decoded price more than `max_dev` off the provider's at resolve time is refused (a layout we
  misread never becomes a price). Liquidity in a tick is the provider's, refreshed with the reference.

Pure decoders and math sit at the top (tested with recorded account bytes); OnChain at the bottom owns the threads."""
import base64
import json
import logging
import math
import queue
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import ws as WS

log = logging.getLogger('desk.onchain')
WSOL = 'So11111111111111111111111111111111111111112'
STABLES = {'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v', 'Es9vMFrzaCERmJfrF4H2FYD4KCoNyFDQpFbRC3KCQLr'}   # USDC, USDT on Solana (only a fallback when the provider gives no quote price)
PROG = {'pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA': 'pumpswap', '675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8': 'raydium_v4', 'CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C': 'raydium_cpmm',
        'CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK': 'clmm', 'HpNfyc2Saw7RKkQd8nEL4khUcuPhQ7WwY1B2qjx8jxFq': 'clmm', 'whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc': 'whirlpool',
        'LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo': 'dlmm', '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P': 'pumpfun'}
CURVE_DISC = bytes.fromhex('17b7f83760d8ac60')   # anchor discriminator of pump.fun's BondingCurve account
CURVE_SUPPLY = 1_000_000_000_000_000
B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
Q64 = 2. ** 64


def b58(raw):
    n, out = int.from_bytes(raw, 'big'), ''
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return B58[0] * (len(raw) - len(raw.lstrip(b'\0'))) + out


def _u64(b, o):
    return struct.unpack_from('<Q', b, o)[0]


def _u128(b, o):
    return int.from_bytes(b[o:o + 16], 'little')


def _pk(b, o):
    return b58(bytes(b[o:o + 32]))


# ---- Solana account layouts (offsets read from the programs' IDLs / source; each is checked against recorded live accounts in tests) ------------------------------------------------------------

def decode_pool(kind, d):
    """The static part of a pool account: base/quote mint and the accounts whose changes move the price. None when the bytes are not that account."""
    if kind == 'pumpswap' and len(d) >= 211 and d[:8] == bytes.fromhex('f19a6d0411b16dbc'):
        return dict(kind=kind, base=_pk(d, 43), quote=_pk(d, 75), vaults=(_pk(d, 139), _pk(d, 171)))
    if kind == 'raydium_v4' and len(d) >= 464:
        return dict(kind=kind, base=_pk(d, 400), quote=_pk(d, 432), vaults=(_pk(d, 336), _pk(d, 368)))
    if kind == 'raydium_cpmm' and len(d) >= 333:
        return dict(kind=kind, base=_pk(d, 168), quote=_pk(d, 200), vaults=(_pk(d, 72), _pk(d, 104)))
    if kind == 'clmm' and len(d) >= 273:
        return dict(kind=kind, base=_pk(d, 73), quote=_pk(d, 105), dec=(d[233], d[234]))
    if kind == 'whirlpool' and len(d) >= 213:
        return dict(kind=kind, base=_pk(d, 101), quote=_pk(d, 181))
    if kind == 'dlmm' and len(d) >= 216:
        return dict(kind=kind, base=_pk(d, 88), quote=_pk(d, 120))
    if kind == 'pumpfun' and len(d) >= 49 and d[:8] == CURVE_DISC and _u64(d, 40) == CURVE_SUPPLY and d[48] == 0:
        return dict(kind=kind, base=None, quote=WSOL, dec=(6, 9))   # the curve account does not name its mint: the watched token is the base
    return None


def token_amount(d):
    """SPL token account balance (atomic), or None."""
    return _u64(d, 64) if len(d) >= 72 else None


def mint_decimals(d):
    return d[44] if len(d) >= 45 else None


def keys_of(pool):
    """The accounts whose changes move this pool's price (what is subscribed to)."""
    return list(pool['vaults']) + ([pool['pk']] if pool['kind'] == 'raydium_cpmm' else []) if pool.get('vaults') else [pool['pk']]


def ratio(pool, accts):
    """Quote units per one base unit (decimals applied) from the pool's current account bytes, or None. `accts` maps pubkey -> bytes; pool carries 'pk', 'kind', 'dec', 'vaults'."""
    kind, (db, dq) = pool['kind'], pool['dec']
    try:
        if pool.get('vaults'):
            b, q = token_amount(accts[pool['vaults'][0]]), token_amount(accts[pool['vaults'][1]])
            if kind == 'raydium_cpmm':   # the vaults also hold the protocol and fund fees, which are not pool reserves (PoolState: protocol_fees_0/1, fund_fees_0/1 at 341)
                f0, f1, g0, g1 = struct.unpack_from('<4Q', accts[pool['pk']], 341)
                b, q = b - f0 - g0, q - f1 - g1
            return (q / 10 ** dq) / (b / 10 ** db) if b and q else None
        d = accts[pool['pk']]
        if kind == 'clmm':
            s = _u128(d, 253) / Q64
        elif kind == 'whirlpool':
            s = _u128(d, 65) / Q64
        elif kind == 'dlmm':
            step, active = struct.unpack_from('<H', d, 80)[0], struct.unpack_from('<i', d, 76)[0]
            return (1 + step / 1e4) ** active * 10. ** (db - dq)
        elif kind == 'pumpfun':
            vt, vs = _u64(d, 8), _u64(d, 16)
            return (vs / 1e9) / (vt / 1e6) if vt and vs else None
        else:
            return None
        return s * s * 10. ** (db - dq) if s > 0 else None
    except (KeyError, struct.error, ZeroDivisionError, OverflowError):
        return None


# ---- EVM: ABI, Multicall3, v2 / v3 pair math ------------------------------------------------------------------------------------------------------------------------------------------------------

MULTICALL3 = '0xcA11bde05977b3631167028862bE2a173976CA11'
SEL = dict(token0='0x0dfe1681', token1='0xd21220a7', reserves='0x0902f1ac', slot0='0x3850c7bd', decimals='0x313ce567', block='0x42cbb15c', ts='0x0f28c97d')
RPC_DEFAULT = {'ethereum': 'https://ethereum-rpc.publicnode.com', 'base': 'https://base-rpc.publicnode.com', 'bsc': 'https://bsc-rpc.publicnode.com', 'arbitrum': 'https://arbitrum-one-rpc.publicnode.com',
               'polygon': 'https://polygon-bor-rpc.publicnode.com', 'optimism': 'https://optimism-rpc.publicnode.com', 'avalanche': 'https://avalanche-c-chain-rpc.publicnode.com'}


def _w(n):
    return n.to_bytes(32, 'big')


def encode_aggregate3(calls):
    """Multicall3.aggregate3((address,bool allowFailure,bytes)[]) calldata for [(target_hex, data_hex)], allowFailure always true."""
    n = len(calls)
    heads, bodies, off = [], [], 32 * n
    for target, data in calls:
        raw = bytes.fromhex(data[2:])
        body = bytes.fromhex(target[2:]).rjust(32, b'\0') + _w(1) + _w(96) + _w(len(raw)) + raw.ljust(-(-len(raw) // 32) * 32, b'\0')
        heads.append(_w(off))
        bodies.append(body)
        off += len(body)
    return '0x82ad56cb' + (_w(32) + _w(n) + b''.join(heads) + b''.join(bodies)).hex()


def decode_aggregate3(hexdata):
    """[(success, bytes)] from an aggregate3 return, or None when malformed."""
    try:
        d = bytes.fromhex(hexdata[2:])
        if len(d) < 64:
            return None
        base = int.from_bytes(d[0:32], 'big')
        n = int.from_bytes(d[base:base + 32], 'big')
        arr, out = base + 32, []
        for i in range(n):
            o = arr + int.from_bytes(d[arr + 32 * i:arr + 32 * i + 32], 'big')
            ok, boff = int.from_bytes(d[o:o + 32], 'big'), int.from_bytes(d[o + 32:o + 64], 'big')
            ln = int.from_bytes(d[o + boff:o + boff + 32], 'big')
            out.append((bool(ok), d[o + boff + 32:o + boff + 32 + ln]))
        return out
    except (ValueError, IndexError):
        return None


def evm_ratio(kind, ret, dec):
    """Quote per base for a pool whose token0 is the base (callers invert otherwise): v2 from reserves (r1/r0), v3 from sqrtPriceX96. dec = (dec0, dec1). None on bad data."""
    d0, d1 = dec
    try:
        if kind == 'v2':
            r0, r1 = int.from_bytes(ret[0:32], 'big'), int.from_bytes(ret[32:64], 'big')
            return (r1 / 10 ** d1) / (r0 / 10 ** d0) if r0 and r1 else None
        if kind == 'v3':
            s = int.from_bytes(ret[0:32], 'big') / 2. ** 96
            return s * s * 10. ** (d0 - d1) if s > 0 else None
    except (ZeroDivisionError, OverflowError):
        pass
    return None


# ---- transport ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

def http_post(url, body, timeout=10.):
    """JSON-RPC POST -> parsed json or None. https only, no redirects followed beyond urllib's default for POST (none)."""
    if urllib.parse.urlsplit(url).scheme != 'https':
        return None
    req = urllib.request.Request(url, json.dumps(body).encode(), {'Content-Type': 'application/json', 'User-Agent': 'sniper-office-desk/1'})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
            return json.loads(r.read(4_000_000))
    except (OSError, ValueError, urllib.error.URLError):
        return None


class Entry:
    """One watched coin: its decoded pool and the latest observed state. `price` is the USD price or None when no reference quote is known."""
    def __init__(self, chain, token, pool, other, ref, liq):
        self.chain, self.token, self.pool, self.other, self.ref, self.liq = chain, token, pool, other, ref, liq   # ref: USD price of `other` (the pool's other token)
        self.ratio, self.slot, self.seen_at, self.chain_ts, self.changed_at = None, None, None, None, None
        self.invert = pool['base'] != token if pool['base'] else False   # the watched token is the pool's quote side


class OnChain:
    def __init__(self, cfg, *, rpc=http_post, clock=time.time, ws_factory=None, start=True):
        self.cfg, self.rpc, self.clock = dict(commitment='confirmed', evm_poll_s=1., ref_s=30., max_dev=.6, retry_s=300., idle_s=600., evm_rpc={}), rpc, clock
        self.cfg.update(cfg or {})
        self.url = self.cfg.get('sol_rpc_url')
        self.lock = threading.Lock()
        self.entries, self.skip, self.pools, self.accts = {}, {}, {}, {}   # (chain,token)->Entry / retry-at; pool pk -> pool; sol pubkey -> (bytes, slot, seen_at)
        self.todo, self.stats, self.ws_ok, self.evm_ok, self.stop = queue.Queue(), dict(sol_rpc=0, evm_rpc=0, ws_msgs=0, ws_bytes=0), False, {}, threading.Event()
        self.ws_factory, self.sub, self.ws, self.sendlock = ws_factory or (lambda url: WS.WebSocket(url)), {}, None, threading.Lock()
        if start:
            for fn in (self._resolver, self._sol_ws, self._evm_poll):
                threading.Thread(target=fn, daemon=True).start()

    # -- bookkeeping the recorder uses
    def count(self, key, n=1):
        with self.lock:
            self.stats[key] = self.stats.get(key, 0) + n

    def known(self, chain, token):
        return (chain, token) in self.entries or self.skip.get((chain, token), 0) > self.clock()

    def add(self, chain, token, pair):
        """Offer a DexScreener pair dict (dexId, pairAddress, quoteToken, priceUsd, priceNative, liquidity) for on-chain resolution; resolved in the background."""
        if not self.known(chain, token):
            self.skip[(chain, token)] = self.clock() + 30.   # in flight
            self.todo.put((chain, token, pair))

    def refresh_ref(self, chain, token, pair):
        """The provider's pair row, polled every ref_s: renews the quote USD reference and liquidity. Returns the (onchain, provider) prices for the cross-check log, or None."""
        e = self.entries.get((chain, token))
        usd, nat = _f(pair.get('priceUsd')), _f(pair.get('priceNative'))
        if e is None:
            return None
        if usd and nat:
            e.ref = usd / nat   # the watched token is DexScreener's base: priceUsd / priceNative = USD of one quote token
        e.liq = _f((pair.get('liquidity') or {}).get('usd')) or e.liq
        return dict(onchain=self.price(e), dex=usd, quote_usd=e.ref, slot=e.slot)

    def sync(self, keys):
        """Drop coins no longer watched; a reconnect then resubscribes only what is still entered (ponytail: no accountUnsubscribe, a live socket keeps old subscriptions until it reconnects)."""
        with self.lock:
            for k in [k for k in self.entries if k not in keys]:
                e = self.entries.pop(k)
                for a in keys_of(e.pool) if e.chain == 'solana' else ():
                    self.pools.pop(a, None)
                    self.accts.pop(a, None)

    def healthy(self, chain, token):
        e = self.entries.get((chain, token))
        if e is None or e.ratio is None:
            return False
        if chain == 'solana':
            return self.ws_ok
        return self.clock() - self.evm_ok.get(chain, 0.) < max(5., 4 * self.cfg['evm_poll_s'])

    def price(self, e):
        if e.ratio is None or not e.ref or e.ref <= 0:
            return None
        p = (1. / e.ratio if e.invert else e.ratio) * e.ref
        return p if math.isfinite(p) and p > 0 else None

    def read(self, chain, token):
        """(price_usd, liquidity_usd, kind, slot, seen_at, chain_ts) of a healthy coin, else None."""
        e = self.entries.get((chain, token))
        if e is None or not self.healthy(chain, token):
            return None
        p = self.price(e)
        return None if p is None else dict(price_usd=p, liquidity_usd=e.liq, source='onchain:' + e.pool['kind'], slot=e.slot, seen_at=e.seen_at, chain_ts=e.chain_ts)

    # -- resolver thread: decode a pool once, then it is followed by push / poll
    def _resolver(self):
        while not self.stop.is_set():
            try:
                chain, token, pair = self.todo.get(timeout=.5)
            except queue.Empty:
                continue
            try:
                ok = self.resolve(chain, token, pair)
            except Exception:   # noqa: BLE001 - one bad pool must not end the resolver
                log.exception('resolve failed')
                ok = False
            if not ok:
                self.skip[(chain, token)] = self.clock() + self.cfg['retry_s']

    def resolve(self, chain, token, pair):
        usd, nat = _f(pair.get('priceUsd')), _f(pair.get('priceNative'))
        quote = (pair.get('quoteToken') or {}).get('address')
        if not (usd and nat and quote and pair.get('pairAddress')):
            return False
        if chain == 'solana':
            e = self._resolve_sol(token, pair['pairAddress'], quote, usd / nat, _f((pair.get('liquidity') or {}).get('usd')))
        else:
            e = self._resolve_evm(chain, token, pair['pairAddress'], quote, usd / nat, _f((pair.get('liquidity') or {}).get('usd')))
        if e is None:
            return False
        p = self.price(e)
        if p is None or not (usd / (1 + self.cfg['max_dev']) <= p <= usd * (1 + self.cfg['max_dev'])):
            log.warning('decoded price off the provider on %s %s: refused', chain, e.pool['kind'])
            return False
        with self.lock:
            self.entries[(chain, token)] = e
        self.skip.pop((chain, token), None)
        return True

    # Solana
    def _sol(self, method, params):
        self.count('sol_rpc')
        doc = self.rpc(self.url, dict(jsonrpc='2.0', id=1, method=method, params=params)) if self.url else None
        return doc.get('result') if isinstance(doc, dict) else None

    def _get_accounts(self, keys):
        res = self._sol('getMultipleAccounts', [keys, dict(encoding='base64', commitment=self.cfg['commitment'])])
        if not isinstance(res, dict) or not isinstance(res.get('value'), list) or len(res['value']) != len(keys):
            return None, None
        out = []
        for v in res['value']:
            out.append(None if not isinstance(v, dict) else (v.get('owner'), base64.b64decode(v['data'][0])))
        return out, (res.get('context') or {}).get('slot')

    def _resolve_sol(self, token, pk, quote, quote_usd, liq):
        got, slot = self._get_accounts([pk])
        if not got or got[0] is None or got[0][0] not in PROG:
            return None
        kind = PROG[got[0][0]]
        pool = decode_pool(kind, got[0][1])
        if pool is None:
            return None
        pool.update(pk=pk)
        if pool['base'] is None:
            pool['base'] = token
        if token not in (pool['base'], pool['quote']):
            return None
        need = keys_of(pool)
        mints = [] if pool.get('dec') else [pool['base'], pool['quote']]
        got2, slot2 = self._get_accounts(need + mints)
        if not got2 or None in got2:
            return None
        if mints:
            decs = [mint_decimals(a[1]) for a in got2[len(need):]]
            if None in decs:
                return None
            pool['dec'] = tuple(decs)
        other = pool['quote'] if token == pool['base'] else pool['base']
        if other != quote:
            return None
        e = Entry('solana', token, pool, other, quote_usd, liq)
        now = self.clock()
        with self.lock:
            for k, a in zip(need, got2):
                self.accts[k] = (a[1], slot2, now)
        self._update_sol(e)
        self._subscribe(need)
        return e

    def _update_sol(self, e):
        keys = keys_of(e.pool)
        if not all(k in self.accts for k in keys):
            return
        r = ratio(e.pool, {k: self.accts[k][0] for k in keys})
        if r is not None:
            e.ratio, e.slot, e.seen_at = r, max(self.accts[k][1] or 0 for k in keys), max(self.accts[k][2] for k in keys)

    def _subscribe(self, keys):
        with self.lock:
            for k in keys:
                self.pools.setdefault(k, 0)
        ws = self.ws
        if ws is not None:
            for k in keys:
                self._send_sub(ws, k)

    def _send_sub(self, ws, k):
        rid = len(self.sub) + 1000 + int(self.clock() * 1000) % 1000000
        self.sub[rid] = k
        with self.sendlock:
            ws.send(json.dumps(dict(jsonrpc='2.0', id=rid, method='accountSubscribe', params=[k, dict(encoding='base64', commitment=self.cfg['commitment'])])))

    def _sol_ws(self):
        """Websocket loop: connect, subscribe every watched account, re-seed once by REST (no initial push exists), then apply each notification. Reconnects with backoff; ws_ok is False meanwhile."""
        back = 1.
        while not self.stop.is_set():
            if not self.url:
                self.stop.wait(1.)
                continue
            try:
                ws = self.ws_factory(self.url.replace('https://', 'wss://', 1))
                ws.settimeout(60.)
                self.sub.clear()
                self.ws = ws
                for k in list(self.pools):
                    self._send_sub(ws, k)
                self._reseed()
                self.ws_ok, back = True, 1.
                pinger = threading.Thread(target=self._ping, args=(ws,), daemon=True)
                pinger.start()
                subid = {}
                while not self.stop.is_set():
                    msg = ws.recv()
                    if msg is None:
                        continue
                    self.count('ws_msgs')
                    self.count('ws_bytes', len(msg))
                    self.on_message(msg, subid)
            except (WS.WsError, OSError, ValueError) as exc:
                log.warning('solana ws down: %s', str(exc)[:60])
            self.ws_ok, self.ws = False, None
            self.stop.wait(back)
            back = min(back * 2, 30.)

    def _ping(self, ws):
        while self.ws is ws and not self.stop.is_set():
            self.stop.wait(20.)
            try:
                with self.sendlock:
                    ws.send(b'', 9)
            except WS.WsError:
                return

    def _reseed(self):
        keys = list(self.pools)
        for i in range(0, len(keys), 100):
            got, slot = self._get_accounts(keys[i:i + 100])
            now = self.clock()
            if got:
                with self.lock:
                    for k, a in zip(keys[i:i + 100], got):
                        if a:
                            self.accts[k] = (a[1], slot, now)
        for e in list(self.entries.values()):
            if e.chain == 'solana':
                self._update_sol(e)

    def on_message(self, msg, subid):
        """One websocket text frame: a subscribe ack (id -> subscription id) or an accountNotification. Stale slots never overwrite newer ones."""
        try:
            doc = json.loads(msg)
        except ValueError:
            return
        if not isinstance(doc, dict):
            return
        if 'id' in doc and isinstance(doc.get('result'), int):
            if doc['id'] in self.sub:
                subid[doc['result']] = self.sub[doc['id']]
            return
        p = doc.get('params') if doc.get('method') == 'accountNotification' else None
        if not isinstance(p, dict):
            return
        k = subid.get(p.get('subscription'))
        res = p.get('result') or {}
        val, slot = res.get('value'), (res.get('context') or {}).get('slot')
        if k is None or not isinstance(val, dict) or not isinstance(slot, int):
            return
        try:
            raw = base64.b64decode(val['data'][0])
        except (KeyError, IndexError, ValueError, TypeError):
            return
        now = self.clock()
        with self.lock:
            old = self.accts.get(k)
            if old and old[1] and old[1] > slot:
                return
            self.accts[k] = (raw, slot, now)
            for e in self.entries.values():
                if e.chain == 'solana' and k in keys_of(e.pool):
                    self._update_sol(e)

    # EVM
    def _resolve_evm(self, chain, token, pk, quote, quote_usd, liq):
        url = self.cfg['evm_rpc'].get(chain) or RPC_DEFAULT.get(chain)
        if not url or not (pk.startswith('0x') and len(pk) == 42):
            return None
        return self._evm_pool(chain, token, pk, quote, quote_usd, liq, url)

    def _evm_multicall(self, url, calls):
        """[block, timestamp, *results] from one aggregate3 eth_call (block and timestamp first, same state), or None."""
        self.count('evm_rpc')
        data = encode_aggregate3([(MULTICALL3, SEL['block']), (MULTICALL3, SEL['ts'])] + calls)
        doc = self.rpc(url, dict(jsonrpc='2.0', id=1, method='eth_call', params=[dict(to=MULTICALL3, data=data), 'latest']))
        out = decode_aggregate3(doc['result']) if isinstance(doc, dict) and isinstance(doc.get('result'), str) else None
        if not out or len(out) != len(calls) + 2 or not (out[0][0] and out[1][0]):
            return None
        return out

    def _evm_pool(self, chain, token, pk, quote, quote_usd, liq, url):
        res = self._evm_multicall(url, [(pk, SEL['token0']), (pk, SEL['token1']), (pk, SEL['reserves']), (pk, SEL['slot0'])])
        if not res or not (res[2][0] and res[3][0]):
            return None
        if not (res[2][0] and res[3][0]):
            return None
        t0, t1 = '0x' + res[2][1][12:32].hex(), '0x' + res[3][1][12:32].hex()
        kind = 'v2' if res[4][0] and len(res[4][1]) >= 64 else 'v3' if res[5][0] and len(res[5][1]) >= 32 else None
        if kind is None or {token.lower(), quote.lower()} != {t0, t1}:
            return None
        dec = self._evm_multicall(url, [(t0, SEL['decimals']), (t1, SEL['decimals'])])
        if not dec or not (dec[2][0] and dec[3][0]):
            return None
        pool = dict(kind=kind, pk=pk, base=t0, quote=t1, dec=(int.from_bytes(dec[2][1], 'big'), int.from_bytes(dec[3][1], 'big')), vaults=None, url=url)
        e = Entry(chain, token.lower(), pool, quote.lower(), quote_usd, liq)
        e.invert = token.lower() != t0
        self._evm_apply(e, res[0][1], res[1][1], res[4][1] if kind == 'v2' else res[5][1])
        self.evm_ok[chain] = self.clock()
        return e

    def _evm_apply(self, e, block, ts, ret):
        r = evm_ratio(e.pool['kind'], ret, e.pool['dec'])
        if r is not None:
            e.ratio, e.slot, e.chain_ts, e.seen_at = r, int.from_bytes(block, 'big'), float(int.from_bytes(ts, 'big')), self.clock()

    def _evm_poll(self):
        """One multicall per chain per `evm_poll_s` covering every watched pool of that chain."""
        while not self.stop.is_set():
            t0 = self.clock()
            for chain in {k[0] for k in list(self.entries) if k[0] != 'solana'}:
                es = [e for k, e in list(self.entries.items()) if k[0] == chain]
                if not es:
                    continue
                url = es[0].pool['url']
                calls = [(e.pool['pk'], SEL['reserves'] if e.pool['kind'] == 'v2' else SEL['slot0']) for e in es]
                res = self._evm_multicall(url, calls)
                if res:
                    for e, r in zip(es, res[2:]):
                        if r[0]:
                            self._evm_apply(e, res[0][1], res[1][1], r[1])
                    self.evm_ok[chain] = self.clock()
            self.stop.wait(max(.05, self.cfg['evm_poll_s'] - (self.clock() - t0)))


def _f(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None
