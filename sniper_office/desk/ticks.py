"""The price recorder and live PnL for the desk plug-in. A host process, never a seat:

    python3 -m sniper_office.desk.ticks --config ticks.json [--once]

Every `tick_s` (1 s) it polls DexScreener's batched pair lookup (one request carries up to 30 pairs of one chain) for every open paper position and every J7 signal still inside its watch
window, and stores one `ticks` row per pair per poll, stamped with OUR fetch-completion time. DexScreener carries no timestamp of its own and its pair lookup serves a price that changes only about every 30 s
(measured live 2026-11-25 on Solana, BSC and Ethereum pairs: poll gap median 0.99 s, price-change gap median 30.3 s; GeckoTerminal's trade list lagged ~100 s and rate-limits at 30/min, so it is no faster), so the poll rate is NOT the price resolution:
`resolution()` measures both (poll cadence, and the gap between price changes) and chart_read reports them. No poll => no tick (a gap), never a
carried-over price. Public limits used: DexScreener 300 requests/min per route; this process spends at most `max_req_per_cycle` (default 4) a second, positions first.

It also writes the live PnL of every open position and each book to the Office db (table desk_live_pnl) once per cycle, and closes each signal window with its outcome (return over the window),
which is what a trader's hit rate is computed from. Paper only: public https reads, no key, no signing."""
import argparse
from contextlib import closing
import fcntl
import json
import logging
import math
from pathlib import Path
import sqlite3
import time
from datetime import datetime, timezone

from . import chains as CH, feed as F, onchain as OC

log = logging.getLogger('desk.ticks')
HOST = 'api.dexscreener.com'
SOURCE = 'dexscreener:pairs_batch'
PROVIDER_CLAIM = 'dexscreener:'   # a source with this prefix is a provider claim; 'onchain:<pool kind>' is read from the pool's own state
SCHEMA = '''
CREATE TABLE IF NOT EXISTS ticks(id INTEGER PRIMARY KEY, at REAL NOT NULL, chain TEXT NOT NULL, token TEXT NOT NULL, pair TEXT, price_usd REAL NOT NULL, liquidity_usd REAL, source TEXT NOT NULL, changed INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ticks_key ON ticks(chain, token, at);
CREATE TABLE IF NOT EXISTS onchain_ref(at REAL NOT NULL, chain TEXT NOT NULL, token TEXT NOT NULL, onchain_usd REAL, dex_usd REAL, quote_usd REAL, slot INTEGER);
CREATE TABLE IF NOT EXISTS exit_path(order_id TEXT NOT NULL, at REAL NOT NULL, chain TEXT, mint TEXT, price_usd REAL, source TEXT, slot INTEGER, net_usd REAL, peak_usd REAL, drawdown_pct REAL, covered_from_fill INTEGER);
CREATE INDEX IF NOT EXISTS exit_path_order ON exit_path(order_id, at);
CREATE TABLE IF NOT EXISTS tick_pairs(chain TEXT NOT NULL, token TEXT NOT NULL, pair TEXT, liquidity_usd REAL, tried_at REAL NOT NULL, PRIMARY KEY(chain, token));
'''
LIVE = '''CREATE TABLE IF NOT EXISTS desk_live_pnl(book_id TEXT NOT NULL, order_id TEXT NOT NULL, at REAL NOT NULL, chain TEXT, mint TEXT, state TEXT, price_usd REAL, tick_age_s REAL, qty REAL, cost_usd REAL,
  value_usd REAL, unrealized_usd REAL, unrealized_pct REAL, realized_usd REAL, equity_usd REAL, initial_usd REAL, total_usd REAL, open_n INTEGER, unpriced_n INTEGER, PRIMARY KEY(book_id, order_id))'''
DEFAULTS = dict(tick_s=1., max_req_per_cycle=4, resolve_per_cycle=2, batch=30, pair_retry_s=60., miss_reset=10, retention_days=7., min_interval_s=0.22, books=[],
            onchain=True, flush_s=0.25, heartbeat_s=1., ref_s=30., commitment='confirmed', evm_poll_s=1., evm_rpc={}, max_dev=.6, research_env_file=None)


def norm(addr):
    return addr.lower() if isinstance(addr, str) and addr[:2].lower() == '0x' else addr


def open_db(path):
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript(SCHEMA)
    have = {r[1] for r in conn.execute('PRAGMA table_info(ticks)')}
    for col, typ in (('slot', 'INTEGER'), ('seen_at', 'REAL'), ('chain_ts', 'REAL')):   # on-chain ticks: slot / block, our receipt time of that state, the block's own timestamp (EVM)
        if col not in have:
            conn.execute('ALTER TABLE ticks ADD COLUMN %s %s' % (col, typ))
    return conn


def ro(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)


def latest(conn, chain, token, now, max_age):
    """The newest tick of a coin no older than max_age, as the filler's quote shape, or None."""
    try:
        try:
            row = conn.execute('SELECT at,price_usd,liquidity_usd,pair,source,slot FROM ticks WHERE chain=? AND token=? AND at<=? ORDER BY at DESC LIMIT 1', (chain, norm(token), now)).fetchone()
        except sqlite3.OperationalError:   # a db the recorder has not migrated yet
            row = conn.execute('SELECT at,price_usd,liquidity_usd,pair,source,NULL FROM ticks WHERE chain=? AND token=? AND at<=? ORDER BY at DESC LIMIT 1', (chain, norm(token), now)).fetchone()
    except sqlite3.Error:
        return None
    if row is None or now - row[0] > max_age:
        return None
    return dict(state='known', price_usd=row[1], liquidity_usd=row[2], pair=row[3], dex=None, source=row[4] or SOURCE, provider=HOST if (row[4] or SOURCE).startswith(PROVIDER_CLAIM) else 'onchain', fetched_at=row[0], slot=row[5])


def series(conn, chain, token, since, until=None):
    return conn.execute('SELECT at,price_usd,liquidity_usd,changed FROM ticks WHERE chain=? AND token=? AND at>=? AND at<=? ORDER BY at', (chain, norm(token), since, until if until is not None else 1e18)).fetchall()


def _q(v, p):
    return v[min(len(v) - 1, int(len(v) * p))] if v else None


def resolution(rows):
    """What the stored ticks really give: poll cadence (gaps between rows) and price cadence (gaps between rows whose price changed). Seconds."""
    at = [r[0] for r in rows]
    gaps = sorted(b - a for a, b in zip(at, at[1:]))
    moved = [r[0] for r in rows if r[3]]
    mg = sorted(b - a for a, b in zip(moved, moved[1:]))
    r = lambda v: None if v is None else round(v, 3)
    return dict(n=len(rows), span_s=r(at[-1] - at[0]) if at else None, poll_gap_median_s=r(_q(gaps, .5)), poll_gap_p90_s=r(_q(gaps, .9)), poll_gap_max_s=r(gaps[-1] if gaps else None),
                price_changes=max(len(moved) - 1, 0) if rows else 0, price_gap_median_s=r(_q(mg, .5)), price_gap_p90_s=r(_q(mg, .9)))


def outcome(rows, recv_at, watch_until):
    """A signal window closed: the coin's return over it, from ticks only. n_ticks 0 means no price was observed (unknown, not flat)."""
    if not rows:
        return dict(n_ticks=0)
    first, last = rows[0][1], rows[-1][1]
    ps = [r[1] for r in rows]
    return dict(n_ticks=len(rows), first_lag_s=round(rows[0][0] - recv_at, 2), window_s=round(watch_until - recv_at), first_price=first, last_price=last, ret_pct=round((last / first - 1) * 100, 4),
                max_up_pct=round((max(ps) / first - 1) * 100, 4), max_dn_pct=round((min(ps) / first - 1) * 100, 4))


def summary(path, chain, token, since, now, points=0):
    """The ticks of a coin from `since` to `now` as stats (first/last/min/max, change, resolution) and, when points>0, up to that many evenly spaced [seconds_before_now, price] samples. Unknown on any failure."""
    try:
        with closing(ro(path)) as c:
            rows = series(c, chain, token, since, now)
            last_source = (c.execute('SELECT source FROM ticks WHERE chain=? AND token=? AND at<=? ORDER BY at DESC LIMIT 1', (chain, norm(token), now)).fetchone() or (SOURCE,))[0]
    except (OSError, sqlite3.Error):
        return dict(state='unknown', reason='ticks_unreadable')
    if not rows:
        return dict(state='unknown', reason='no_ticks_in_window', source=SOURCE, since=since)
    ps = [r[1] for r in rows]
    doc = dict(state='known', provider_claim=last_source.startswith(PROVIDER_CLAIM), first_at=rows[0][0], last_at=rows[-1][0], last_age_s=round(now - rows[-1][0], 2), first_price=ps[0], last_price=ps[-1], min_price=min(ps), max_price=max(ps),
               change_pct=round((ps[-1] / ps[0] - 1) * 100, 4), max_drawdown_pct=round(min(p / m - 1 for p, m in zip(ps, _runmax(ps))) * 100, 4), liquidity_usd=rows[-1][2], resolution=resolution(rows),
               source=last_source,
               note='a dexscreener source is the provider\'s claim polled about once a second and stamped with our fetch time, but the provider refreshes it only about every 30 s; an onchain source is the pool\'s own state (slot / block in the row) with the quote token\'s USD from the provider every ~30 s; resolution shows the real row gaps and how often the price actually changed')
    if points:
        step = max(1, math.ceil(len(rows) / points))
        doc['samples'] = [[round(r[0] - now, 2), r[1]] for r in rows[::step]] + ([[round(rows[-1][0] - now, 2), rows[-1][1]]] if (len(rows) - 1) % step else [])
    return doc


def _runmax(ps):
    m = 0.
    for p in ps:
        m = max(m, p)
        yield m


# ---- live PnL -----------------------------------------------------------------------------------------------------------------------------------------------------------

def book_live(ledger, tconn, book, now, max_age=10.):
    """Live PnL of one book from the Office ledger and the ticks: per open position and for the book. Unrealized = modelled net exit value at the newest tick (after exit slippage and fee) minus what
    the entry cost; a position with no fresh tick is `unpriced` and never valued (the book total then says how many). Realized = net cashflow of closed orders."""
    from . import filler as FL
    ledger.row_factory = sqlite3.Row
    initial = ledger.execute('SELECT COALESCE(SUM(usd),0) FROM capital_flows WHERE book_id=?', (book,)).fetchone()[0]
    cash = initial + ledger.execute('SELECT COALESCE(SUM(c.usd),0) FROM cashflows c JOIN orders o USING(order_id) WHERE o.book_id=?', (book,)).fetchone()[0]
    realized = ledger.execute("SELECT COALESCE(SUM(c.usd),0) FROM cashflows c JOIN orders o USING(order_id) WHERE o.book_id=? AND o.state='closed'", (book,)).fetchone()[0]
    positions, value_sum, unreal, unpriced = [], 0., 0., 0
    for o in ledger.execute("SELECT order_id,network_id,mint,qty,policy_json,filled_at FROM orders WHERE book_id=? AND state='open' ORDER BY filled_at", (book,)).fetchall():
        chain = o['network_id'] or 'solana'
        cost = -ledger.execute('SELECT COALESCE(SUM(usd),0) FROM cashflows WHERE order_id=?', (o['order_id'],)).fetchone()[0]
        tick = latest(tconn, chain, o['mint'], now, max_age) if tconn is not None else None
        ev = FL.exit_value(json.loads(o['policy_json']), o['qty'], tick) if tick else None
        p = dict(order_id=o['order_id'], chain=chain, mint=o['mint'], qty=o['qty'], cost_usd=cost, filled_at=o['filled_at'])
        if ev is None:
            unpriced += 1
            positions.append(dict(p, state='unpriced', reason='no_fresh_tick' if tick is None else 'liquidity_unknown', price_usd=None, tick_age_s=None, value_usd=None, unrealized_usd=None, unrealized_pct=None))
            continue
        net = ev[2]
        value_sum += net
        unreal += net - cost
        positions.append(dict(p, state='priced', tick_source=tick.get('source'), tick_slot=tick.get('slot'), price_usd=tick['price_usd'], tick_age_s=round(now - tick['fetched_at'], 2), value_usd=net, unrealized_usd=net - cost, unrealized_pct=(net / cost - 1) * 100 if cost > 0 else None))
    total = dict(book_id=book, at=now, initial_usd=initial, realized_usd=realized, unrealized_usd=unreal, open_n=len(positions), unpriced_n=unpriced,
                 equity_usd=cash + value_sum if not unpriced else None, total_usd=realized + unreal if not unpriced else None)
    return dict(book=total, positions=positions)


def publish(office, live):
    office.execute(LIVE)
    b = live['book']
    office.execute('DELETE FROM desk_live_pnl WHERE book_id=?', (b['book_id'],))
    office.execute('INSERT INTO desk_live_pnl(book_id,order_id,at,realized_usd,unrealized_usd,equity_usd,initial_usd,total_usd,open_n,unpriced_n) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (b['book_id'], '', b['at'], b['realized_usd'], b['unrealized_usd'], b['equity_usd'], b['initial_usd'], b['total_usd'], b['open_n'], b['unpriced_n']))
    for p in live['positions']:
        office.execute('INSERT INTO desk_live_pnl(book_id,order_id,at,chain,mint,state,price_usd,tick_age_s,qty,cost_usd,value_usd,unrealized_usd,unrealized_pct) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (b['book_id'], p['order_id'], b['at'], p['chain'], p['mint'], p['state'], p['price_usd'], p['tick_age_s'], p['qty'], p['cost_usd'], p['value_usd'], p['unrealized_usd'], p['unrealized_pct']))


# ---- the recorder -------------------------------------------------------------------------------------------------------------------------------------------------------

class Recorder:
    def __init__(self, cfg, *, fetch=F.http_fetch, clock=time.time, sleep=time.sleep, onchain=None):
        self.cfg = dict(DEFAULTS, **cfg)
        self.clock = clock
        self.net = F.Net(lambda url, body=None: fetch(url, body), clock, sleep, dict(dexscreener=self.cfg['min_interval_s']))
        self.db = open_db(cfg['j7_db'])
        self.office = None
        if cfg.get('office_db'):
            self.office = sqlite3.connect(cfg['office_db'], timeout=10, isolation_level=None)
            self.office.execute('PRAGMA journal_mode=WAL')
        self.last, self.miss, self.rr, self.retained = {}, {}, 0, 0.
        self.ref_at, self.oc_last, self.watched, self.peak, self.first_seen = {}, {}, {}, {}, {}
        self.oc = onchain if onchain is not None else (OC.OnChain(dict(sol_rpc_url=self._sol_url(), commitment=self.cfg['commitment'], evm_poll_s=self.cfg['evm_poll_s'], ref_s=self.cfg['ref_s'], evm_rpc=self.cfg['evm_rpc'],
                                                                    max_dev=self.cfg['max_dev']), clock=clock) if self.cfg['onchain'] else None)

    def _sol_url(self):
        """The research Helius URL from the env file (read-only methods and the same host's wss); never logged."""
        try:
            for line in Path(self.cfg['research_env_file']).read_text().splitlines():
                k, _, v = line.strip().partition('=')
                if k == 'MEMECOIN_RESEARCH_HELIUS_RPC_URL' and v.strip().strip('\'"').startswith('https://'):
                    return v.strip().strip('\'"')
        except (OSError, TypeError):
            pass
        return None

    # what to watch: open positions first, then signals inside their window
    def watch(self, now):
        keys = {}
        if self.cfg.get('ledger_db') and Path(self.cfg['ledger_db']).exists():
            try:
                with closing(ro(self.cfg['ledger_db'])) as c:
                    for chain, mint, pool in c.execute("SELECT network_id,mint,pool FROM orders WHERE state='open'"):
                        keys[(chain or 'solana', norm(mint))] = (0, norm(pool))
            except sqlite3.Error:
                log.warning('ledger_unreadable')
        for chain, token in self.db.execute('SELECT chain,token FROM j7_signals WHERE watch_until>? AND recv_at<=?', (now, now)).fetchall() if self._has_signals() else ():
            keys.setdefault((chain, norm(token)), (1, None))
        return {k: v for k, v in keys.items() if CH.valid_chain(k[0]) and CH.valid_address(k[1])}

    def _has_signals(self):
        return self.db.execute("SELECT 1 FROM sqlite_master WHERE name='j7_signals'").fetchone() is not None

    def pair_of(self, chain, token):
        row = self.db.execute('SELECT pair,tried_at FROM tick_pairs WHERE chain=? AND token=?', (chain, token)).fetchone()
        return row

    def resolve(self, chain, token, hint, now):
        """Find the deepest pair of the token on the chain (one lookup) and remember it; a hint (an order's own pool) is used as is."""
        if hint:
            self.db.execute('INSERT OR REPLACE INTO tick_pairs VALUES(?,?,?,NULL,?)', (chain, token, hint, now))
            return hint
        doc, why = self.net.get('https://%s/latest/dex/tokens/%s' % (HOST, token))
        pair = F.best_pair(doc, token, chain) if doc else None
        self.db.execute('INSERT OR REPLACE INTO tick_pairs VALUES(?,?,?,?,?)', (chain, token, norm(pair['pairAddress']) if pair else None, F.num((pair or {}).get('liquidity', {}).get('usd')) if pair else None, now))
        return norm(pair['pairAddress']) if pair else None

    def cycle(self):
        now = self.clock()
        watch = self.watch(now)
        self.watched = watch
        if self.oc:
            self.oc.sync(set(watch))
        reqs = resolves = polled = 0
        todo = {}
        for (chain, token), (prio, hint) in sorted(watch.items(), key=lambda kv: (kv[1][0], kv[0])):
            row = self.pair_of(chain, token)
            pair = row[0] if row else None
            if (pair is None and (row is None or now - row[1] >= self.cfg['pair_retry_s'])) and resolves < self.cfg['resolve_per_cycle']:
                resolves += 1
                reqs += 0 if hint else 1
                pair = self.resolve(chain, token, hint, now)
            if pair and self.oc and self.oc.healthy(chain, token) and now - self.ref_at.get((chain, token), 0.) < self.cfg['ref_s']:
                continue   # followed on chain: the provider is asked only every ref_s, for the quote token's USD
            if pair:
                todo.setdefault((prio, chain), {})[pair] = token
        batches = []
        for (prio, chain), m in sorted(todo.items()):
            pairs = sorted(m)
            batches += [(prio, chain, {p: m[p] for p in pairs[i:i + self.cfg['batch']]}) for i in range(0, len(pairs), self.cfg['batch'])]
        pos, rest = [b for b in batches if b[0] == 0], [b for b in batches if b[0] == 1]
        if rest:
            self.rr %= len(rest)
            rest = rest[self.rr:] + rest[:self.rr]
        budget = max(self.cfg['max_req_per_cycle'] - reqs, 0)
        chosen = (pos + rest)[:budget]
        self.rr += max(len(chosen) - len(pos), 0)
        for prio, chain, m in chosen:
            doc, why = self.net.get('https://%s/latest/dex/pairs/%s/%s' % (HOST, chain, ','.join(m)))
            at = self.clock()
            seen = set()
            for p in (doc or {}).get('pairs') or [] if isinstance(doc, dict) else []:
                pair = norm(p.get('pairAddress')) if isinstance(p, dict) else None
                token = m.get(pair)
                price, liq = F.num(p.get('priceUsd')), F.num((p.get('liquidity') or {}).get('usd'))
                if token is None or p.get('chainId') != chain or not CH.same_address((p.get('baseToken') or {}).get('address'), token) or not price or price <= 0:
                    continue
                seen.add(token)
                key = (chain, token)
                if self.oc:
                    info = self.oc.refresh_ref(chain, token, p)
                    if info is None:
                        self.oc.add(chain, token, p)   # resolved in the background; this poll's price is recorded below as before
                    else:
                        self.ref_at[key] = at
                        self.db.execute('INSERT INTO onchain_ref(at,chain,token,onchain_usd,dex_usd,quote_usd,slot) VALUES(?,?,?,?,?,?,?)', (at, chain, token, info['onchain'], info['dex'], info['quote_usd'], info['slot']))
                    if self.oc.healthy(chain, token):
                        self.miss[key] = 0
                        continue
                self.db.execute('INSERT INTO ticks(at,chain,token,pair,price_usd,liquidity_usd,source,changed) VALUES(?,?,?,?,?,?,?,?)', (at, chain, token, pair, price, liq, SOURCE, int(self.last.get(key) != price)))
                self.last[key] = price
                self.miss[key] = 0
                polled += 1
            for pair, token in m.items():
                if token not in seen:
                    key = (chain, token)
                    self.miss[key] = self.miss.get(key, 0) + 1
                    if self.miss[key] >= self.cfg['miss_reset']:
                        self.db.execute('DELETE FROM tick_pairs WHERE chain=? AND token=?', (chain, token))   # re-resolve: the pair may have moved or the provider dropped it
                        self.miss[key] = 0
            reqs += 1
        self.close_windows(now)
        live = self.live(now)
        if now - self.retained > 3600:
            self.retained = now
            self.db.execute('DELETE FROM ticks WHERE at<?', (now - self.cfg['retention_days'] * 86400.,))
            self.db.execute('DELETE FROM onchain_ref WHERE at<?', (now - self.cfg['retention_days'] * 86400.,))
            self.db.execute('DELETE FROM exit_path WHERE at<?', (now - self.cfg['retention_days'] * 86400.,))
        return dict(at=now, watched=len(watch), requests=reqs, ticks=polled, positions=len(live['positions']) if live else None)

    def flush(self):
        """Write the on-chain ticks: a row whenever a watched coin's price moved, and a heartbeat row every heartbeat_s (the pool state is still that price: the push feed is alive). A coin whose feed is not
        healthy gets no row here, and cycle() polls the provider for it again. Returns the rows written."""
        if not self.oc:
            return 0
        now, n = self.clock(), 0
        for (chain, token) in list(self.watched):
            r = self.oc.read(chain, token)
            if r is None:
                continue
            key = (chain, token)
            prev = self.oc_last.get(key)
            moved = prev is None or prev[1] != r['price_usd']
            if not moved and now - prev[0] < self.cfg['heartbeat_s']:
                continue
            pair = (self.pair_of(chain, token) or (None,))[0]
            self.db.execute('INSERT INTO ticks(at,chain,token,pair,price_usd,liquidity_usd,source,changed,slot,seen_at,chain_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                            (now, chain, token, pair, r['price_usd'], r['liquidity_usd'], r['source'], int(moved), r['slot'], r['seen_at'], r['chain_ts']))
            self.oc_last[key] = (now, r['price_usd'])
            self.last[key] = r['price_usd']
            n += 1
        return n

    def close_windows(self, now):
        if not self._has_signals():
            return
        for sid, chain, token, recv_at, until in self.db.execute('SELECT signal_id,chain,token,recv_at,watch_until FROM j7_signals WHERE watch_until<=? AND outcome_json IS NULL', (now,)).fetchall():
            self.db.execute('UPDATE j7_signals SET outcome_json=? WHERE signal_id=?', (json.dumps(outcome(series(self.db, chain, token, recv_at, until), recv_at, until)), sid))

    def exit_path(self, positions, now):
        """The observed exit price path of every open position (the Sniper exit observer's price half): per cycle, the price, the source and slot it came from, the modeled net exit value (after slippage and
        fee, as the filler would exit), the running peak of that value since we first saw the position, and the drawdown from it. covered_from_fill says whether the peak includes the fill moment."""
        for p in positions:
            if p['state'] != 'priced':
                continue
            oid = p['order_id']
            if oid not in self.peak:
                row = self.db.execute('SELECT MAX(peak_usd),MIN(at),MIN(covered_from_fill) FROM exit_path WHERE order_id=?', (oid,)).fetchone()
                self.peak[oid] = row[0] or 0.
                self.first_seen[oid] = (row[1] if row[1] is not None else now, row[2] if row[2] is not None else int(now - p['filled_at'] <= 2.5))
            self.peak[oid] = peak = max(self.peak[oid], p['value_usd'])
            self.db.execute('INSERT INTO exit_path(order_id,at,chain,mint,price_usd,source,slot,net_usd,peak_usd,drawdown_pct,covered_from_fill) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                            (oid, now, p['chain'], p['mint'], p['price_usd'], p.get('tick_source'), p.get('tick_slot'), p['value_usd'], peak, (p['value_usd'] / peak - 1) * 100 if peak > 0 else None, self.first_seen[oid][1]))

    def live(self, now):
        if not (self.cfg.get('ledger_db') and self.office and self.cfg['books'] and Path(self.cfg['ledger_db']).exists()):
            return None
        out = dict(positions=[])
        try:
            with closing(ro(self.cfg['ledger_db'])) as lc:
                for book in self.cfg['books']:
                    live = book_live(lc, self.db, book, now, self.cfg.get('tick_max_age_s', 10.))
                    publish(self.office, live)
                    self.exit_path(live['positions'], now)
                    out['positions'] += live['positions']
        except sqlite3.Error:
            log.exception('live pnl failed')
            return None
        return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--once', action='store_true', help='one cycle, then exit')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    cfg = json.loads(args.config.read_text())
    Path(cfg['j7_db']).parent.mkdir(parents=True, exist_ok=True)
    with open(str(cfg['j7_db']) + '.ticks.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)   # one recorder per db
        rec = Recorder(cfg)
        quiet, next_cycle = 0, 0.
        while True:
            t0 = time.time()
            try:
                if t0 >= next_cycle:
                    next_cycle = t0 + rec.cfg['tick_s']
                    out = rec.cycle()
                    quiet += 1
                    if args.once or quiet % 60 == 1:
                        log.info(json.dumps(out))
                rec.flush()
            except Exception:   # loud and alive: the next cycle re-reads the durable tables
                log.exception('cycle failed')
            if args.once:
                return
            time.sleep(max(0., rec.cfg['flush_s'] - (time.time() - t0)))


if __name__ == '__main__':
    main()
