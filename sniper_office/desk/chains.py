"""Chain names for the multi-chain desk: J7's networkId / network name -> DexScreener chainId (+ GeckoTerminal network id), address checks, per-chain size floors.
Ported from the source project's chain table (Solana 1399811149, BSC 56, Base 8453, Ethereum 1, the 4663 chain) with the same ids J7 relays. A chain not listed falls back to J7's own lowercase
network name as the DexScreener chainId; a name DexScreener does not know simply prices as unknown (no fill)."""
import re

# (J7 networkId, J7 network names) -> (DexScreener chainId, GeckoTerminal network or None)
TABLE = (
    (('1399811149', 'solana'), 'solana', 'solana'),
    (('56', 'bnb', 'bsc'), 'bsc', 'bsc'),
    (('8453', 'base'), 'base', 'base'),
    (('1', 'ethereum', 'eth'), 'ethereum', 'eth'),
    (('4663', 'robinhood'), 'robinhood', None),
    (('42161', 'arbitrum'), 'arbitrum', 'arbitrum'),
    (('137', 'polygon'), 'polygon', 'polygon_pos'),
    (('10', 'optimism'), 'optimism', 'optimism'),
    (('43114', 'avalanche', 'avax'), 'avalanche', 'avax'),
    (('146', 'sonic'), 'sonic', 'sonic'),
)
BY_KEY = {k: (d, g) for keys, d, g in TABLE for k in keys}
GECKO = {d: g for _, d, g in TABLE}
CHAIN = re.compile(r'[a-z0-9][a-z0-9_-]{1,31}\Z')
ADDRESS = re.compile(r'(?:0x[0-9a-fA-F]{40}|[A-Za-z0-9]{20,70})\Z')
DEFAULT_MIN_USD = 200.   # the source project's absolute source-buy floor (MEMECOIN_REAL_MIN_SOURCE_BUY_USD, $200), the same on every chain


def chain_of(network_id=None, network=None):
    """The DexScreener chainId for a J7 token, or None when J7 gave neither field."""
    for v in (network_id, network):
        hit = BY_KEY.get(str(v).strip().lower()) if v not in (None, '') else None
        if hit:
            return hit[0]
    name = str(network or '').strip().lower()
    return name if CHAIN.match(name) else None


def valid_chain(chain):
    return isinstance(chain, str) and bool(CHAIN.match(chain))


def valid_address(addr):
    return isinstance(addr, str) and bool(ADDRESS.match(addr))


def same_address(a, b):
    """EVM addresses compare case-insensitively (checksummed vs lowercase); others exactly."""
    return isinstance(a, str) and isinstance(b, str) and (a == b or (a[:2].lower() == '0x' and a.lower() == b.lower()))


def gecko_network(chain):
    return GECKO.get(chain)


def min_usd(cfg, chain, trader=None):
    """Size floor in USD: the trader's own override, else the chain's, else cfg default, else the source project's $200."""
    if trader and trader.get('min_usd') is not None:
        return float(trader['min_usd'])
    per = cfg.get('min_usd') if isinstance(cfg.get('min_usd'), dict) else {}
    return float(per.get(chain, per.get('default', DEFAULT_MIN_USD)))
