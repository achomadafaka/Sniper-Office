"""Public, keyless market reads for the desk plug-in, host side: one DexScreener pair on any chain (the provider's claim, never a fill price) and GeckoTerminal's recent trades for a pool.
Transport is feed.py's: https, fixed host allowlist, no redirects, 2 MB cap, per-host pause and 429 backoff, shared by every seat in the process."""
from datetime import datetime
import time

from . import chains as CH, feed as F

FETCH = F.http_fetch          # tests replace this
INTERVALS = dict(F.DEFAULTS['min_interval_s'])
TRADE_CAP = 300               # GeckoTerminal returns at most this many recent trades
_NET = None


def net():
    global _NET
    if _NET is None:
        _NET = F.Net(lambda url, body=None: FETCH(url, body), time.time, time.sleep, INTERVALS)
    return _NET


def _addr_ok(chain, v):
    return bool(F.B58.fullmatch(v)) if chain == 'solana' else CH.valid_address(v)


def read_pair(mint=None, pool=None, chain='solana'):
    """(snapshot-or-None, fetched_at, why). By pool when given, else the deepest pool of the mint, on `chain` (a DexScreener chainId). The provider's claim at fetched_at."""
    if not CH.valid_chain(chain):
        return None, None, 'bad_chain'
    for v in (mint, pool):
        if v is not None and not (isinstance(v, str) and _addr_ok(chain, v)):
            return None, None, 'bad_address'
    if mint is None and pool is None:
        return None, None, 'mint_or_pool_required'
    n = net()
    if pool:
        doc, why = n.get('https://api.dexscreener.com/latest/dex/pairs/%s/%s' % (chain, pool))
        at = time.time()
        pairs = [p for p in (doc or {}).get('pairs') or [] if isinstance(p, dict) and p.get('chainId') == chain and CH.same_address(p.get('pairAddress'), pool)] if isinstance(doc, dict) else []
        pair = pairs[0] if pairs else None
        if pair is not None and mint and not CH.same_address((pair.get('baseToken') or {}).get('address'), mint):
            return None, at, 'mint_pool_mismatch'
    else:
        doc, why = n.get('https://api.dexscreener.com/latest/dex/tokens/' + mint)
        at = time.time()
        pair = F.best_pair(doc, mint, chain) if doc else None
    if pair is None:
        return None, at, why or 'pair_not_found'
    return F.snapshot(pair, at), at, None


def _ts(text):
    try:
        return datetime.fromisoformat(str(text).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def trades_window(pool, now, window_s=60, chain='solana'):
    """Trades of the last window_s seconds for a pool, from GeckoTerminal's recent-trades list. state known with counts and coverage, or unknown with a reason.
    covered is true only when the list reaches back past the window (or is not at its cap): a list full of newer trades proves nothing about the start of the window.
    The provider's trade time lags the chain by an unknown amount, so newest_trade_age_s is returned for the reader to judge."""
    if not (isinstance(pool, str) and _addr_ok(chain, pool)):
        return dict(state='unknown', reason='bad_address')
    gecko = CH.gecko_network(chain)
    if gecko is None:
        return dict(state='unknown', reason='trades_not_available_on_chain', source='geckoterminal:pool_trades')
    doc, why = net().get('https://api.geckoterminal.com/api/v2/networks/%s/pools/%s/trades' % (gecko, pool))
    at = time.time()
    rows = doc.get('data') if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        return dict(state='unknown', reason=why or 'trades_unreadable', source='geckoterminal:pool_trades', fetched_at=at)
    stamps = []
    for r in rows:
        a = r.get('attributes') if isinstance(r, dict) else None
        t = _ts(a.get('block_timestamp')) if isinstance(a, dict) else None
        if t is not None and a.get('kind') in ('buy', 'sell'):
            stamps.append((t, a['kind'], F.num(a.get('volume_in_usd')) or 0.))
    inside = [s for s in stamps if now - window_s <= s[0] <= now + 5]
    covered = (len(rows) < TRADE_CAP and (bool(stamps) or not rows)) or bool(stamps) and min(s[0] for s in stamps) <= now - window_s
    return dict(state='known' if covered else 'unknown', reason=None if covered else 'list_does_not_reach_window_start',
                source='geckoterminal:pool_trades', fetched_at=at, window_s=window_s, trades_n=len(inside), buys_n=sum(s[1] == 'buy' for s in inside), sells_n=sum(s[1] == 'sell' for s in inside),
                volume_usd=round(sum(s[2] for s in inside), 2), newest_trade_age_s=round(now - max(s[0] for s in stamps)) if stamps else None, listed_n=len(rows))
