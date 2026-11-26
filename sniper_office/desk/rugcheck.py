"""Rug detectors for the hard filter, adapted from the source project's proven ones (nothing imported from it): the check row, PumpSwap LP custody, the on-curve test and the program-owner lookup
for wallet-only holder concentration, and the funder cluster. Every function answers a check row (status pass/kill/unknown + reason); a read that fails is unknown, never pass.

RPC cost per mint (read-only methods through feed.Net.rpc): lp 2 calls (pool, lp supply; +3 when the LP is not burned); holders +1 (owner programs); cluster 0 to 2 per traced owner
(a getSignaturesForAddress page, capped at 2 pages, and one getTransaction) over the top-20 wallets, run last and only when every other check would pass."""
import base64
import json
from pathlib import Path
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

from .onchain import b58

WSOL = 'So11111111111111111111111111111111111111112'
PUMP_AMM = 'pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA'
SYSTEM_PROGRAM = '11111111111111111111111111111111'
INCINERATOR = '1nc1nerator11111111111111111111111111111111'
POOL_PROGRAMS = frozenset({
    'pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA',   # PumpSwap AMM
    '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P',   # pump.fun bonding curve
    'LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo',   # Meteora DLMM
    'cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG',   # Meteora DAMM v2
    'Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB',  # Meteora dynamic AMM
    'dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN',   # Meteora dynamic bonding curve
    '675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8',  # Raydium AMM v4
    'CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C',  # Raydium CPMM
    'CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK',  # Raydium CLMM
    'LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj',   # Raydium LaunchLab (Bonk)
    'whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc',   # Orca Whirlpool
})
POOL_KEYS_AT = 11      # discriminator 8, bump 1, index 2; then creator, base mint, quote mint, lp mint, two vaults, lp_supply u64
MAX_PAGES = 2          # signature pages per funder trace: an owner with more than 1000 * MAX_PAGES signatures is 'deep', unresolved
KNOWN_FUNDERS_PATH = Path(__file__).with_name('cluster_known_funders.json')
B58_ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
P25519 = 2 ** 255 - 19
D25519 = -121665 * pow(121666, -1, P25519) % P25519


def check(status, reason, source, fetched_at, **values):
    return dict(status=status, reason=reason, source=source, fetched_at=fetched_at, **values)


def _decode(address):
    """The 32 bytes of a base58 address, or None."""
    if not isinstance(address, str) or not 32 <= len(address) <= 44 or any(c not in B58_ALPHABET for c in address):
        return None
    n = 0
    for c in address:
        n = n * 58 + B58_ALPHABET.index(c)
    raw = (b'\0' * (len(address) - len(address.lstrip('1')))) + n.to_bytes((n.bit_length() + 7) // 8, 'big')
    return raw if len(raw) == 32 else None


def on_curve(address):
    """True for an ed25519 key (a wallet), False for an off-curve address (a PDA), None when unparseable. Pure python point decompression, so no solders dependency."""
    raw = _decode(address)
    if raw is None:
        return None
    y = int.from_bytes(raw, 'little') & (2 ** 255 - 1)
    if y >= P25519:
        return False
    x2 = (y * y - 1) * pow(D25519 * y * y + 1, -1, P25519) % P25519
    return x2 == 0 or pow(x2, (P25519 - 1) // 2, P25519) == 1


def known_funders():
    """Shared infrastructure funders (exchange hot wallets, a co-signer): an owner they funded is resolved but joins no cluster. The committed file, never a live label."""
    return frozenset(json.loads(KNOWN_FUNDERS_PATH.read_bytes())['addresses'])


# ---- PumpSwap LP custody -------------------------------------------------------------------------------------------------------------------------------------------------

def _account(net, address, encoding):
    """(value, why): the account, or None with why None when the address has no account."""
    res, why = net.rpc('getAccountInfo', [address, dict(encoding=encoding, commitment='confirmed')])
    if why or not isinstance(res, dict) or 'value' not in res:
        return None, why or 'rpc_error'
    return res['value'], None


def lp_check(net, clock, pair, mint, pool):
    """Who can pull a PumpSwap pool's liquidity. Pass `lp_burned` when the LP mint supply is 0 at a dated slot. Kill `lp_held_by_creator` when the LP is outstanding and its largest holder
    is the pool creator, or the mint did not come from a launchpad migration (suffix 'pump': a heuristic, as in feed.pool_origin). Anything unread is unknown."""
    src = 'rpc:pumpswap_pool'
    if str(pair.get('dexId') or '') != 'pumpswap':
        return check('unknown', 'lp_reader_not_available', 'none', clock())
    unknown = lambda reason: check('unknown', reason, src, clock())
    acct, why = _account(net, pool, 'base64')
    if why or not isinstance(acct, dict):
        return unknown(why or 'lp_pool_missing')
    if acct.get('owner') != PUMP_AMM:
        return unknown('lp_pool_not_pumpswap')
    try:
        raw = base64.b64decode(acct['data'][0])
    except (KeyError, IndexError, TypeError, ValueError):
        return unknown('lp_pool_unreadable')
    if len(raw) < POOL_KEYS_AT + 32 * 6 + 8:
        return unknown('lp_pool_short')
    key = lambda n: b58(raw[POOL_KEYS_AT + 32 * n:POOL_KEYS_AT + 32 * (n + 1)])
    creator, lp_mint = key(0), key(3)
    if key(1) != mint or key(2) != WSOL:
        return unknown('lp_pool_mismatch')
    res, why = net.rpc('getTokenSupply', [lp_mint, dict(commitment='confirmed')])
    try:
        supply, slot = int(res['value']['amount']), res['context']['slot']
    except (KeyError, TypeError, ValueError):
        return unknown(why or 'lp_supply_unreadable')
    if supply < 0 or type(slot) is not int or slot <= 0:
        return unknown('lp_supply_unusable')      # burned is claimed only at a dated slot
    if supply == 0:
        return check('pass', 'lp_burned', src, clock(), creator=creator, lp_mint=lp_mint, lp_supply_atomic=0, slot=slot)
    owner = None
    res, _ = net.rpc('getTokenLargestAccounts', [lp_mint, dict(commitment='confirmed')])
    rows = (res or {}).get('value') if isinstance(res, dict) else None
    if isinstance(rows, list) and rows and isinstance(rows[0], dict) and rows[0].get('address'):
        value, _ = _account(net, rows[0]['address'], 'jsonParsed')
        owner = ((((value or {}).get('data') or {}).get('parsed') or {}).get('info') or {}).get('owner') if isinstance(value, dict) and isinstance(value.get('data'), dict) else None
    migrated = mint.endswith('pump')
    facts = dict(creator=creator, lp_mint=lp_mint, lp_supply_atomic=supply, slot=slot, largest_holder_owner=owner, launchpad_migrated=migrated)
    if owner == creator or not migrated:
        return check('kill', 'lp_held_by_creator', src, clock(), **facts)
    return check('unknown', 'lp_outstanding_holder_unproven' if owner else 'lp_holder_unreadable', src, clock(), **facts)


# ---- holder book: program-owned owners are not holders -------------------------------------------------------------------------------------------------------------------

def owner_exclusions(net, owners):
    """({owner: exclusion reason or None}, why). A wallet is owned by the System Program; a pool vault authority or a curve is owned by its program; the incinerator holds burned supply. An owner
    with no account is a wallet only when its address is on the ed25519 curve; off-curve with no account, or a row missing from the answer, cannot be classified (why 'owner_program_unknown')."""
    out = {o: 'incinerator' for o in owners if o == INCINERATOR}
    todo = sorted(set(owners) - set(out))
    if todo:
        res, why = net.rpc('getMultipleAccounts', [todo, dict(encoding='jsonParsed')])
        vals = (res or {}).get('value') if isinstance(res, dict) else None
        if not isinstance(vals, list) or len(vals) != len(todo):
            return None, why or 'owner_programs_unreadable'
        for owner, v in zip(todo, vals):
            if v is None:
                if on_curve(owner) is not True:
                    return None, 'owner_program_unknown'
                out[owner] = None
            elif isinstance(v, dict) and isinstance(v.get('owner'), str) and v['owner']:
                # Only a PDA (off-curve) of a vetted pool or launchpad program is a vault or curve. A key that can sign stays a holder whatever program owns its
                # account, and a PDA of any other program (a locker, a creator's own program) is held supply too: excluding it would hide concentration.
                out[owner] = 'program_account:' + v['owner'] if v['owner'] in POOL_PROGRAMS and on_curve(owner) is False else None
            else:
                return None, 'owner_program_unknown'
    return out, None


# ---- funder cluster ------------------------------------------------------------------------------------------------------------------------------------------------------

CLUSTERED_LIMIT = (25, 100)     # clustered supply >= 25% of the attempted book
TOP_FUNDER_LIMIT = (50, 100)    # largest shared funder group >= 50%
MIN_ELIGIBLE_OWNERS = 4         # fewer: the thin-book rule, only a funder shared by two or more owners counts


class ClusterQuota(RuntimeError):
    """The research RPC rate-limited a funder trace."""


def decide_cluster(owners, *, trace_fn, known=frozenset(), workers=8, deadline_s=12., clock=time.monotonic):
    """-> (allowed, reason, metrics). `owners` = [(owner, atomic balance)]. Port of the source's supply-weighted cluster admission with its desk settings (thin-book rule, early allow on the exact
    bound of the unresolved supply, shared-infrastructure funders excluded). Limits are exact integer ratios. Allowed only on a full or provably sufficient resolution; every other end is
    unknown (deadline, quota, trace error, unresolved owner) except the two blocks, cluster_share_high and cluster_top_funder_high."""
    balances = {}
    for owner, bal in owners:
        if bal > 0:
            balances[owner] = balances.get(owner, 0) + bal
    attempted, funders = sum(balances.values()), {}
    thin = len(balances) < MIN_ELIGIBLE_OWNERS

    def metrics():
        found = {}
        for o, f in funders.items():
            if f not in known:
                found.setdefault(f, []).append(o)
        if thin:
            found = {f: g for f, g in found.items() if len(g) >= 2}
        return dict(attempted_supply=attempted, eligible=len(balances), resolved=len(funders), resolved_supply=sum(balances[o] for o in funders),
                    clustered_supply=sum(balances[o] for g in found.values() if len(g) >= 2 for o in g), largest_funder_supply=max((sum(balances[o] for o in g) for g in found.values()), default=0),
                    excluded_known_funders=sum(1 for f in funders.values() if f in known))

    def final(reason, allowed=False):
        return allowed, reason, metrics()

    over = lambda part, limit: part * limit[1] >= attempted * limit[0]

    def block():
        m = metrics()
        if over(m['clustered_supply'], CLUSTERED_LIMIT):
            return final('cluster_share_high')
        if over(m['largest_funder_supply'], TOP_FUNDER_LIMIT):
            return final('cluster_top_funder_high')

    def bound_allows():
        """True when the unresolved supply cannot push either limit whatever its funders are: all of it joins the largest group (top), each open owner turns a different singleton into a cluster."""
        sums, sizes = {}, {}
        for o, f in funders.items():
            if f not in known:
                sums[f], sizes[f] = sums.get(f, 0) + balances[o], sizes.get(f, 0) + 1
        open_owners = [o for o in balances if o not in funders]
        joinable = sum(balances[o] for o in open_owners) if (len(open_owners) >= 2 or sums) else 0
        singles = sorted((v for f, v in sums.items() if sizes[f] == 1), reverse=True)
        shared_top = max((v for f, v in sums.items() if sizes[f] >= 2), default=0)
        top = max(shared_top if thin else max(sums.values(), default=0), singles[0] if singles else 0) + joinable
        clustered = sum(v for f, v in sums.items() if sizes[f] >= 2) + joinable + (sum(singles[:len(open_owners)]) if joinable else 0)
        return clustered * CLUSTERED_LIMIT[1] < attempted * CLUSTERED_LIMIT[0] and top * TOP_FUNDER_LIMIT[1] < attempted * TOP_FUNDER_LIMIT[0]

    def early():
        if len(funders) < len(balances) and bound_allows():
            return final('pass_thin_bound' if thin else 'allowed_bound', allowed=True)

    if not balances:
        return final('cluster_no_holders')
    done = block() or early()
    if done:
        return done
    todo = sorted(balances, key=balances.get, reverse=True)
    stop, started = threading.Event(), clock()
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        while todo:
            remaining = deadline_s - (clock() - started)
            if remaining <= 0:
                return final('cluster_unknown_deadline')
            batch, todo = todo[:max(1, workers)], todo[max(1, workers):]
            futures = {pool.submit(trace_fn, o, stop.is_set): o for o in batch}
            finished, pending = wait(futures, timeout=remaining)
            if pending:
                return final('cluster_unknown_deadline')
            for future in finished:
                try:
                    funder, status = future.result()
                except ClusterQuota:
                    return final('cluster_unknown_quota')
                except Exception:
                    return final('cluster_unknown_trace_error')
                if status == 'resolved' and funder:
                    funders[futures[future]] = funder
            done = block() or early()
            if done:
                return done
    finally:
        stop.set()      # a trace in flight stops at its next page: nothing outside it can end it
        pool.shutdown(wait=False, cancel_futures=True)
    if len(funders) != len(balances):
        return final('cluster_unknown_unresolved')
    return final('pass_thin_owners' if thin else 'allowed', allowed=True)


def wrap_close_funder(instructions, owner):
    """The payer that funded `owner` through a closed wrapped-SOL account in these instructions, or None. Pure."""
    def info(ix):
        parsed = ix.get('parsed')
        return (parsed.get('type'), parsed.get('info') or {}) if isinstance(parsed, dict) else (None, {})
    closed = {i.get('account') for ix in instructions for kind, i in [info(ix)] if kind == 'closeAccount' and i.get('destination') == owner}
    credits = []
    for ix in instructions:
        kind, i = info(ix)
        dest = i.get('newAccount') if kind in ('createAccount', 'createAccountWithSeed') else i.get('destination') if kind == 'transfer' else None
        lamports = i.get('lamports')
        if dest in closed and i.get('source') and type(lamports) is int and lamports > 0:
            credits.append((lamports, i['source']))
    best = max(credits)[1] if credits else None
    return best if best and _decode(best) and best != owner else None


def trace_first_funder(net, owner, should_stop=None, max_pages=MAX_PAGES):
    """(funder or None, status): the source of the owner's FIRST transaction, read from at most `max_pages` signature pages. A failed first transaction or a deep history is unresolved/deep.
    An rpc failure raises (ClusterQuota when rate limited): half a history must never read as the first transaction."""
    def call(method, params):
        res, why = net.rpc(method, params)
        if why == 'rate_limited':
            raise ClusterQuota(why)
        if why:
            raise RuntimeError(why)
        return res
    before, earliest, candidate, err = None, None, None, None
    for _ in range(max_pages):
        if should_stop is not None and should_stop():
            return None, 'aborted'
        rows = call('getSignaturesForAddress', [owner, dict(limit=1000, **(dict(before=before) if before else {}))]) or []
        if not rows:
            earliest = candidate
            break
        candidate, err = rows[-1].get('signature'), rows[-1].get('err')
        if len(rows) < 1000:
            earliest = candidate
            break
        before = candidate
    else:
        return None, 'deep'
    if not earliest or err is not None or (should_stop is not None and should_stop()):
        return None, 'unresolved'
    tx = call('getTransaction', [earliest, dict(encoding='jsonParsed', maxSupportedTransactionVersion=1)])
    meta = tx.get('meta') if isinstance(tx, dict) else None
    if not isinstance(meta, dict) or meta.get('err') is not None:      # cannot verify success, or it failed: fail closed
        return None, 'unresolved'
    instructions = list(tx['transaction']['message'].get('instructions') or [])
    for group in meta.get('innerInstructions') or []:
        instructions.extend(group.get('instructions') or [])
    for ix in instructions:
        parsed = ix.get('parsed')
        info = (parsed.get('info') or {}) if isinstance(parsed, dict) else {}
        if isinstance(parsed, dict) and parsed.get('type') in ('transfer', 'createAccount') and (info.get('destination') if parsed['type'] == 'transfer' else info.get('newAccount')) == owner:
            if _decode(info.get('source')) is None or info['source'] == owner:
                break       # not a funder identity: fall through to the wrap rule
            return info['source'], 'resolved'
    wrapped = wrap_close_funder(instructions, owner)
    return (wrapped, 'resolved') if wrapped else (None, 'unresolved')


def cluster_check(net, clock, book, cfg):
    """The cluster check from `book` = {owner wallet: atomic balance} (program-owned and burned owners already removed). kill on a shared-funder share or a top funder over the limits; pass on a
    full or provably sufficient resolution; unknown otherwise. Hard deadline cfg['cluster_deadline_s'], a pool of cfg['cluster_workers'] threads."""
    allowed, reason, metrics = decide_cluster(list(book.items()), trace_fn=lambda o, stop: trace_first_funder(net, o, stop), known=known_funders(),
                                              workers=cfg['cluster_workers'], deadline_s=cfg['cluster_deadline_s'])
    attempted = metrics['attempted_supply'] or 1
    shares = dict(clustered_pct=round(metrics['clustered_supply'] / attempted * 100., 4), top_funder_pct=round(metrics['largest_funder_supply'] / attempted * 100., 4))
    status = 'pass' if allowed else 'kill' if reason in ('cluster_share_high', 'cluster_top_funder_high') else 'unknown'
    return check(status, reason, 'rpc:funder_trace', clock(), **shares, **metrics)
