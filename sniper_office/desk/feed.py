"""Live coin feed for the desk plug-in. A host process, never a seat: it finds new Solana pools from public sources, collects the evidence a hard filter needs, and writes one ticket per
pool into the Office's own desk db, in the schema desk/paper.py reads (table desk_tickets), so feed_list / sheet_read / paper_order work with no other project running.

    python3 -m sniper_office.desk.feed --config feed.json [--once]

Read-only and keyless by default: https GET/POST to a fixed host allowlist, no redirects, bounded bodies, 429 backoff per host. The optional research RPC (url in an env file named by
config research_env_file, key MEMECOIN_RESEARCH_HELIUS_RPC_URL) is used for four read-only methods only; its url is never logged or stored.

Every check row carries value, source, provider, fetched_at and the provider's claim. A check whose data is unknown is recorded status 'unknown' with a reason, never assumed to pass.
Verdict, ported from the source project's hard filter (hf2 + intake bounds): a kill on any check => SKIP; an unknown MANDATORY check (market_data, token_safety, sell_probe, liquidity,
mcap) or an unknown required-collected check (chart, holders) => SKIP; otherwise PASS. Unknown soft checks (cluster, dump_group) never refuse. Dropped, because they need the source
project's private tape or funding graph: dump_group, cluster, mayhem flag, 60 s chart prints, pumpswap pool-math sell probe (replaced by a public route quote round trip)."""
import argparse
from contextlib import closing
import fcntl
import hashlib
import json
import logging
import math
from pathlib import Path
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from . import chains as CH

log = logging.getLogger('desk.feed')
WSOL = 'So11111111111111111111111111111111111111112'
TOKEN_2022 = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'
B58 = re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}')
HOSTS = {'api.dexscreener.com': 'dexscreener', 'api.geckoterminal.com': 'geckoterminal', 'lite-api.jup.ag': 'jupiter'}
RPC_METHODS = ('getAccountInfo', 'getMultipleAccounts', 'getTokenLargestAccounts', 'getTokenSupply')
MAX_BODY = 2_000_000
DEFAULTS = dict(desk_db='desk.db', interval_s=60., max_new_per_cycle=15, retry_unknown_s=300., retention_days=7., max_rows=20000, probe_sol=0.1,
                mcap_safety=[5000., 50_000_000.], liq_min_quote_sol=10., round_trip_loss_pct_max=8., max_pair_age_s=86400., min_prints_m5=2, min_interval_s=dict(dexscreener=1., geckoterminal=2.5, jupiter=1.2, rpc=0.2))
SCHEMA = '''
CREATE TABLE IF NOT EXISTS desk_tickets(id INTEGER PRIMARY KEY, ticket_id TEXT NOT NULL, network_id TEXT NOT NULL, mint TEXT NOT NULL, pool TEXT, stage TEXT NOT NULL, verdict TEXT NOT NULL,
  reason TEXT NOT NULL, ts TEXT NOT NULL, inputs_json TEXT NOT NULL, UNIQUE(ticket_id, stage));
CREATE INDEX IF NOT EXISTS desk_tickets_ts ON desk_tickets(ts);
CREATE TABLE IF NOT EXISTS feed_seen(mint TEXT PRIMARY KEY, first_seen REAL NOT NULL, source TEXT NOT NULL, claim_json TEXT NOT NULL, ticketed INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS feed_seen_first ON feed_seen(first_seen);
'''


def finite(v):
    return type(v) in (int, float) and math.isfinite(v)


def num(v):
    """A provider number (they send strings too) as a finite float, or None."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if type(v) is not bool and math.isfinite(f) else None


# ---- http ---------------------------------------------------------------------------------------------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def http_fetch(url, body=None):
    """(status, parsed json or None, retry_after_s). https only; a redirect is a failure; at most MAX_BODY bytes."""
    if urllib.parse.urlsplit(url).scheme != 'https':
        return None, None, None
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), headers={'User-Agent': 'sniper-office-desk/1', 'Content-Type': 'application/json', 'Accept': 'application/json'})
    try:
        with _OPENER.open(req, timeout=10) as r:
            raw = r.read(MAX_BODY + 1)
            return (r.status, json.loads(raw), None) if len(raw) <= MAX_BODY else (r.status, None, None)
    except urllib.error.HTTPError as exc:
        retry = exc.headers.get('Retry-After') if exc.headers else None
        return exc.code, None, num(retry)
    except (OSError, ValueError):
        return None, None, None


class Net:
    """Fetch with a per-host pause between calls and a 429 backoff. Answers (doc or None, why or None); never raises."""
    def __init__(self, fetch, clock, sleep, intervals, rpc_url=None):
        self.fetch, self.clock, self.sleep, self.intervals, self.rpc_url = fetch, clock, sleep, intervals, rpc_url
        self.next_ok, self.stop_until = {}, {}
        self.rpc_host = urllib.parse.urlsplit(rpc_url).hostname if rpc_url else None

    def _key(self, url):
        host = urllib.parse.urlsplit(url).hostname
        return HOSTS.get(host) or ('rpc' if host and host == self.rpc_host else None)

    def get(self, url, body=None):
        key = self._key(url)
        if key is None:
            return None, 'host_not_allowed'
        now = self.clock()
        if now < self.stop_until.get(key, 0.):
            return None, 'rate_limited'
        wait = self.next_ok.get(key, 0.) - now
        if wait > 0:
            self.sleep(wait)
        status, doc, retry = self.fetch(url, body)
        at = self.clock()
        self.next_ok[key] = at + self.intervals.get(key, 1.)
        if status == 429:
            self.stop_until[key] = at + min(max(retry or 60., 5.), 300.)
            return None, 'rate_limited'
        if status != 200 or doc is None:
            return None, 'http_%s' % status if status else 'unreachable'
        return doc, None

    def rpc(self, method, params):
        if self.rpc_url is None:
            return None, 'rpc_unconfigured'
        if method not in RPC_METHODS:
            raise ValueError('rpc_method_not_read_only')
        doc, why = self.get(self.rpc_url, dict(jsonrpc='2.0', id=1, method=method, params=params))
        if why:
            return None, why
        if not isinstance(doc, dict) or 'result' not in doc or doc.get('error'):
            return None, 'rpc_error'
        return doc['result'], None


def rpc_url_from(env_file):
    """The research RPC url from an env file (KEY=value lines), https only; None when unset or unreadable."""
    try:
        for line in Path(env_file).read_text().splitlines():
            key, sep, value = line.strip().partition('=')
            if sep and key.strip() == 'MEMECOIN_RESEARCH_HELIUS_RPC_URL':
                value = value.strip().strip('\'"')
                return value if urllib.parse.urlsplit(value).scheme == 'https' else None
    except (OSError, TypeError):
        pass
    return None


# ---- discovery ----------------------------------------------------------------------------------------------------------------------------------------------------------

def discover(net, clock):
    """New-token candidates [(mint, source, claim)] from the public sources. A source that fails contributes nothing; its reason is returned for the log."""
    found, notes = {}, {}
    for name, url in (('dexscreener:token-profiles', 'https://api.dexscreener.com/token-profiles/latest/v1'), ('dexscreener:token-boosts', 'https://api.dexscreener.com/token-boosts/latest/v1')):
        doc, why = net.get(url)
        at = clock()
        notes[name] = why or 'ok'
        for row in doc if isinstance(doc, list) else []:
            if isinstance(row, dict) and row.get('chainId') == 'solana' and isinstance(row.get('tokenAddress'), str) and B58.fullmatch(row['tokenAddress']):
                found.setdefault(row['tokenAddress'], (name, dict(fetched_at=at, url=url, claim=dict(chainId='solana', tokenAddress=row['tokenAddress']))))
    url = 'https://api.geckoterminal.com/api/v2/networks/solana/new_pools'
    doc, why = net.get(url)
    at = clock()
    notes['geckoterminal:new_pools'] = why or 'ok'
    for row in (doc or {}).get('data', []) if isinstance(doc, dict) else []:
        try:
            base = row['relationships']['base_token']['data']['id']
            attrs = row['attributes']
            mint = base.split('_', 1)[1]
            if base.startswith('solana_') and B58.fullmatch(mint):
                found.setdefault(mint, ('geckoterminal:new_pools', dict(fetched_at=at, url=url, claim=dict(pool=attrs.get('address'), pool_created_at=attrs.get('pool_created_at')))))
        except (KeyError, TypeError, IndexError, AttributeError):
            continue
    return [(m, s, c) for m, (s, c) in found.items()], notes


# ---- evidence -----------------------------------------------------------------------------------------------------------------------------------------------------------

def check(status, reason, source, fetched_at, **values):
    return dict(status=status, reason=reason, source=source, fetched_at=fetched_at, **values)


def best_pair(doc, mint, chain='solana'):
    """The deepest pair of `mint` on `chain` (DexScreener chainId). Solana pair addresses must be base58; other chains' are any plain address, EVM compared case-insensitively."""
    ok = (lambda a: isinstance(a, str) and B58.fullmatch(a)) if chain == 'solana' else CH.valid_address
    pairs = [p for p in (doc.get('pairs') or []) if isinstance(p, dict) and p.get('chainId') == chain and CH.same_address((p.get('baseToken') or {}).get('address'), mint) and ok(p.get('pairAddress'))] if isinstance(doc, dict) else []
    return max(pairs, key=lambda p: num((p.get('liquidity') or {}).get('usd')) or 0., default=None)


def snapshot(pair, now=None):
    """The provider's numbers for one pair, as received: run-up (priceChange), volume, txns, reserves, liquidity, age, fdv/mcap. A missing window stays None, never zero."""
    g = lambda d, k: d.get(k) if isinstance(d, dict) else None
    windows = ('m5', 'h1', 'h6', 'h24')
    txns = pair.get('txns') if isinstance(pair.get('txns'), dict) else {}
    liq = pair.get('liquidity') if isinstance(pair.get('liquidity'), dict) else {}
    created = num(pair.get('pairCreatedAt'))
    return dict(dex=pair.get('dexId'), pair=pair.get('pairAddress'), price_usd=num(pair.get('priceUsd')), price_native=num(pair.get('priceNative')),
                run_up_pct={w: num(g(pair.get('priceChange'), w)) for w in windows}, volume_usd={w: num(g(pair.get('volume'), w)) for w in windows},
                txns={w: dict(buys=num(g(txns.get(w), 'buys')), sells=num(g(txns.get(w), 'sells'))) for w in windows},
                reserves=dict(base=num(liq.get('base')), quote=num(liq.get('quote')), base_token=(pair.get('baseToken') or {}).get('address'), quote_token=(pair.get('quoteToken') or {}).get('address'),
                              quote_symbol=(pair.get('quoteToken') or {}).get('symbol')),
                liquidity_usd=num(liq.get('usd')), pair_created_at_ms=created, pair_age_s=None if created is None or now is None else round(now - created / 1000.),
                fdv_usd=num(pair.get('fdv')), market_cap_usd=num(pair.get('marketCap')))


def market_checks(pair, now, cfg, at):
    """market_data, liquidity, mcap, chart (age + activity), flow: all from one DexScreener pair, which is a provider claim."""
    src = 'dexscreener:pairs'
    claim = {k: pair.get(k) for k in ('dexId', 'pairAddress', 'priceUsd', 'marketCap', 'fdv', 'pairCreatedAt', 'liquidity', 'txns', 'quoteToken')}
    price = num(pair.get('priceUsd'))
    checks = dict(market_data=check('pass' if price and price > 0 else 'unknown', 'price_present' if price and price > 0 else 'price_missing', src, at, price_usd=price, provider_claim=claim))
    liq, quote = pair.get('liquidity') or {}, pair.get('quoteToken') or {}
    sol = num(liq.get('quote')) if quote.get('address') == WSOL else None
    if sol is None:
        checks['liquidity'] = check('unknown', 'quote_sol_unknown' if quote.get('address') != WSOL else 'liquidity_missing', src, at, quote_token=quote.get('address'), liquidity_usd=num(liq.get('usd')))
    else:
        low = sol < cfg['liq_min_quote_sol']
        checks['liquidity'] = check('kill' if low else 'pass', 'liq_min' if low else 'liq_ok', src, at, quote_sol=sol, min_quote_sol=cfg['liq_min_quote_sol'], liquidity_usd=num(liq.get('usd')))
    cap = num(pair.get('marketCap')) or num(pair.get('fdv'))
    lo, hi = cfg['mcap_safety']
    if cap is None:
        checks['mcap'] = check('unknown', 'mcap_missing', src, at)
    else:
        out = not lo <= cap <= hi
        checks['mcap'] = check('kill' if out else 'pass', 'mcap_safety' if out else 'mcap_ok', src, at, mcap_usd=cap, band=[lo, hi], basis='marketCap' if num(pair.get('marketCap')) else 'fdv')
    created = num(pair.get('pairCreatedAt'))
    txns = (pair.get('txns') or {}).get('m5')
    if created is None or not isinstance(txns, dict):
        checks['chart'] = check('unknown', 'age_or_activity_unknown', src, at, pair_created_at_ms=created)
    else:
        age = now - created / 1000.
        old = age > cfg['max_pair_age_s']
        b5, s5 = num(txns.get('buys')), num(txns.get('sells'))
        prints = None if b5 is None or s5 is None else b5 + s5   # a missing count is unknown, never 0 (a provider 0 stays 0)
        # Sniper's chart rule: fewer than 2 recent prints is an unknown chart (chart_60s_prints_lt_2), never a pass; here the window is the provider's m5 count
        quiet = prints is not None and prints < cfg['min_prints_m5']
        checks['chart'] = check('unknown' if prints is None else 'kill' if old else 'unknown' if quiet else 'pass', 'm5_txns_missing' if prints is None else 'pair_too_old' if old else 'chart_prints_lt_2' if quiet else 'age_and_activity_present', src, at, age_s=round(age), max_age_s=cfg['max_pair_age_s'],
                                txns_m5=txns, prints_m5=prints, min_prints_m5=cfg['min_prints_m5'])
    buys, sells = (num(txns.get(k)) if isinstance(txns, dict) else None for k in ('buys', 'sells'))
    if buys is not None and sells is not None and buys >= 20 and sells == 0:
        checks['flow'] = check('kill', 'buys_without_sells', src, at, buys_m5=buys, sells_m5=sells)
    else:
        checks['flow'] = check('pass' if buys is not None and sells is not None else 'unknown', 'flow_m5_present' if buys is not None and sells is not None else 'flow_unknown', src, at, buys_m5=buys, sells_m5=sells)
    return checks


def token_checks(net, clock, mint, pool):
    """token_safety (mint + freeze authority, transfer hook) and holders (top 10 share), from the research RPC. No RPC => both unknown."""
    src = 'rpc:getAccountInfo'
    res, why = net.rpc('getAccountInfo', [mint, dict(encoding='jsonParsed')])
    at = clock()
    value = (res or {}).get('value') if isinstance(res, dict) else None
    info = (((value or {}).get('data') or {}).get('parsed') or {}).get('info') if isinstance(value, dict) and isinstance((value.get('data')), dict) else None
    if not isinstance(info, dict):
        reason = why or 'mint_unreadable'
        return dict(token_safety=check('unknown', reason, src, at), holders=check('unknown', reason, 'rpc', at))
    open_ = lambda key: 'closed' if info.get(key) is None else 'open'
    hooks = [e for e in info.get('extensions') or [] if isinstance(e, dict) and e.get('extension') == 'transferHook' and ((e.get('state') or {}).get('programId'))]
    safety = dict(authority_mint=open_('mintAuthority'), authority_freeze=open_('freezeAuthority'), transfer_hook=hooks[0]['state']['programId'] if hooks else None, supply_atomic=str(info.get('supply')), decimals=info.get('decimals'),
                  owner_program=value.get('owner'), mint_context_slot=(res.get('context') or {}).get('slot'))
    bad = safety['authority_mint'] == 'open' or safety['authority_freeze'] == 'open' or hooks
    reason = 'mint_authority_open' if safety['authority_mint'] == 'open' else 'freeze_authority_open' if safety['authority_freeze'] == 'open' else 'transfer_hook' if hooks else 'mint_read'
    token = check('kill' if bad else 'pass', reason, src, at, **safety)
    return dict(token_safety=token, holders=holders_check(net, clock, mint, pool, info))


def holders_check(net, clock, mint, pool, info):
    src = 'rpc:getTokenLargestAccounts'
    res, why = net.rpc('getTokenLargestAccounts', [mint])
    at = clock()
    rows = (res or {}).get('value') if isinstance(res, dict) else None
    supply = num(info.get('supply'))
    if not isinstance(rows, list) or not rows or not supply or supply <= 0:
        return check('unknown', why or 'largest_accounts_unreadable', src, at)
    accounts = [r.get('address') for r in rows[:20] if isinstance(r, dict)]
    amounts = [num(r.get('amount')) for r in rows[:20] if isinstance(r, dict)]
    owners, why = net.rpc('getMultipleAccounts', [accounts, dict(encoding='jsonParsed')])
    vals = (owners or {}).get('value') if isinstance(owners, dict) else None
    if not isinstance(vals, list) or len(vals) != len(accounts) or None in amounts:
        return check('unknown', why or 'owners_unreadable', src, at)
    held = []
    for amount, v in zip(amounts, vals):
        owner = ((((v or {}).get('data') or {}).get('parsed') or {}).get('info') or {}).get('owner') if isinstance(v, dict) and isinstance(v.get('data'), dict) else None
        if owner is None:
            return check('unknown', 'owner_unresolved', src, at)
        if owner != pool:    # the pool's own vault is not a holder
            held.append(amount)
    top10 = sum(sorted(held, reverse=True)[:10]) / supply * 100.
    return check('pass', 'creator_unresolved', src, at, top10_pct=round(top10, 4), creator_pct=None, exclusions_verified=len(held) < len(accounts), excluded_owner=pool, accounts_read=len(accounts))


def sell_probe(net, clock, mint, probe_sol):
    """Round trip through a public route quote: sell exactly what the buy quote returns. The loss is what the route says it costs, a provider claim, not a landed trade."""
    src = 'jupiter:quote'
    lamports = int(probe_sol * 1e9)
    base = 'https://lite-api.jup.ag/swap/v1/quote?slippageBps=50&swapMode=ExactIn&'
    buy, why = net.get(base + urllib.parse.urlencode(dict(inputMint=WSOL, outputMint=mint, amount=lamports)))
    at = clock()
    out = int(buy['outAmount']) if isinstance(buy, dict) and str(buy.get('outAmount', '')).isdigit() else 0
    if out <= 0:
        return check('unknown', why or 'no_route_buy', src, at)
    sell, why = net.get(base + urllib.parse.urlencode(dict(inputMint=mint, outputMint=WSOL, amount=out)))
    at = clock()
    back = int(sell['outAmount']) if isinstance(sell, dict) and str(sell.get('outAmount', '')).isdigit() else 0
    if back <= 0:
        return check('unknown', why or 'no_route_sell', src, at, buy_out_atomic=out)
    return check('pass', 'route_round_trip', src, at, round_trip_loss_pct=round((1 - back / lamports) * 100., 4), exact_quantity=True, in_lamports=lamports, buy_out_atomic=out, sell_back_lamports=back,
                 price_impact_pct=dict(buy=buy.get('priceImpactPct'), sell=sell.get('priceImpactPct')))


def verdict(checks, cfg):
    """(verdict, reason). Order as in the source: kill first, then unknown mandatory, then unknown required-collected."""
    for name, row in checks.items():
        if row['status'] == 'kill':
            return 'SKIP', 'kill:%s:%s' % (name, row['reason'])
    for name in ('market_data', 'token_safety', 'sell_probe', 'liquidity', 'mcap', 'chart', 'holders'):
        if checks[name]['status'] != 'pass':
            return 'SKIP', 'unknown:%s:%s' % (name, checks[name]['reason'])
    loss = checks['sell_probe'].get('round_trip_loss_pct')
    if not finite(loss):
        return 'SKIP', 'unknown:sell_probe:round_trip_loss_pct'
    if loss > cfg['round_trip_loss_pct_max']:
        return 'SKIP', 'kill:sell_probe:round_trip_loss_pct'
    return 'PASS', 'pass'


# ---- collector ----------------------------------------------------------------------------------------------------------------------------------------------------------

def open_db(path):
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript(SCHEMA)
    return conn


class Feed:
    def __init__(self, cfg, *, fetch=http_fetch, clock=time.time, sleep=time.sleep):
        self.cfg = {**DEFAULTS, **cfg, 'min_interval_s': {**DEFAULTS['min_interval_s'], **cfg.get('min_interval_s', {})}}
        self.clock = clock
        self.net = Net(fetch, clock, sleep, self.cfg['min_interval_s'], rpc_url_from(cfg['research_env_file']) if cfg.get('research_env_file') else None)
        self.db = open_db(self.cfg['desk_db'])

    def observe(self, mint, source, claim, now):
        """Remember a mint the first time a source names it. True when it is new."""
        cur = self.db.execute('INSERT OR IGNORE INTO feed_seen(mint,first_seen,source,claim_json) VALUES(?,?,?,?)', (mint, now, source, json.dumps(claim, default=str)))
        return cur.rowcount == 1

    def evaluate(self, mint, source, claim, now):
        """The ticket row for one mint, or None while it is too early to decide (a transport failure, retried until retry_unknown_s)."""
        doc, _ = self.net.get('https://api.dexscreener.com/latest/dex/tokens/' + mint)
        at = self.clock()
        pair = best_pair(doc, mint) if doc else None
        if pair is None:
            return None   # not indexed yet, or the provider failed: retried until retry_unknown_s, then written as unknown
        pool = pair['pairAddress']
        checks = market_checks(pair, now, self.cfg, at)
        market = dict(snapshot(pair, now), source='dexscreener:pairs', fetched_at=at, note='provider claim at decision time')
        checks.update(token_checks(self.net, self.clock, mint, pool))
        checks['sell_probe'] = sell_probe(self.net, self.clock, mint, self.cfg['probe_sol'])
        checks['cluster'] = check('unknown', 'not_collected_here', 'none', at)
        checks['dump_group'] = check('unknown', 'not_collected_here', 'none', at)
        v, reason = verdict(checks, self.cfg)
        return self.row(mint, pool, source, claim, now, checks, v, reason, market)

    def row(self, mint, pool, source, claim, now, checks, v, reason, market=None):
        evidence = dict(network_id='solana', mint=mint, pool=pool, known_at=datetime.fromtimestamp(claim['fetched_at'], timezone.utc).isoformat(), checks=checks,
                        market=market,
                        discovery=dict(source=source, **claim), policy=dict(round_trip_loss_pct_max=self.cfg['round_trip_loss_pct_max'], liq_min_quote_sol=self.cfg['liq_min_quote_sol'], mcap_safety=self.cfg['mcap_safety'],
                                                                         max_pair_age_s=self.cfg['max_pair_age_s'], probe_sol=self.cfg['probe_sol']))
        ticket_id = 'office:feed:' + hashlib.sha256(('solana:%s:%s' % (mint, pool)).encode()).hexdigest()[:32]
        return dict(ticket_id=ticket_id, mint=mint, pool=pool, verdict=v, reason=reason, ts=datetime.fromtimestamp(now, timezone.utc).isoformat(), inputs_json=json.dumps(dict(evidence=evidence), default=str, sort_keys=True))

    def write(self, t):
        cur = self.db.execute("INSERT OR IGNORE INTO desk_tickets(ticket_id,network_id,mint,pool,stage,verdict,reason,ts,inputs_json) VALUES(?,'solana',?,?,'hard_filter',?,?,?,?)",
                              (t['ticket_id'], t['mint'], t['pool'], t['verdict'], t['reason'], t['ts'], t['inputs_json']))
        self.db.execute('UPDATE feed_seen SET ticketed=1 WHERE mint=?', (t['mint'],))
        return cur.rowcount == 1

    def retain(self, now):
        cut = datetime.fromtimestamp(now - self.cfg['retention_days'] * 86400., timezone.utc).isoformat()
        n = self.db.execute('DELETE FROM desk_tickets WHERE ts<?', (cut,)).rowcount
        self.db.execute('DELETE FROM feed_seen WHERE first_seen<?', (now - self.cfg['retention_days'] * 86400.,))
        n += self.db.execute('DELETE FROM desk_tickets WHERE id IN (SELECT id FROM desk_tickets ORDER BY id DESC LIMIT -1 OFFSET ?)', (self.cfg['max_rows'],)).rowcount
        return n

    def cycle(self):
        now = self.clock()
        found, notes = discover(self.net, self.clock)
        for mint, source, claim in found:
            self.observe(mint, source, claim, now)
        pending = self.db.execute('SELECT mint,source,claim_json,first_seen FROM feed_seen WHERE ticketed=0 ORDER BY first_seen DESC LIMIT ?', (self.cfg['max_new_per_cycle'],)).fetchall()
        counts = dict(written=0, passed=0, deferred=0)
        for mint, source, claim_json, first_seen in pending:
            t = self.evaluate(mint, source, json.loads(claim_json), self.clock())
            if t is None and self.clock() - first_seen > self.cfg['retry_unknown_s']:
                t = self.row(mint, None, source, json.loads(claim_json), self.clock(), {}, 'SKIP', 'unknown:market_data:no_pair_or_provider_unavailable')
            if t is None:
                counts['deferred'] += 1
                continue
            if self.write(t):
                counts['written'] += 1
                counts['passed'] += t['verdict'] == 'PASS'
        return dict(sources=notes, discovered=len(found), pruned=self.retain(self.clock()), **counts)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--once', action='store_true', help='one cycle, then exit')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    cfg = json.loads(args.config.read_text())
    Path(cfg.get('desk_db', DEFAULTS['desk_db'])).parent.mkdir(parents=True, exist_ok=True)
    with open(str(cfg.get('desk_db', DEFAULTS['desk_db'])) + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)   # one collector per desk db
        feed = Feed(cfg)
        while True:
            try:
                log.info(json.dumps(feed.cycle(), default=str))
            except Exception:   # loud and alive: the next cycle re-reads everything from the durable tables
                log.exception('cycle failed')
            if args.once:
                return
            time.sleep(feed.cfg['interval_s'])


if __name__ == '__main__':
    main()
