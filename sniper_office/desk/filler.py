"""Paper fill engine for the desk plug-in. A host process, never a seat: it reads desk_paper_requests from the office db, fills them in the Office's own ledger, marks open positions,
applies sl / tp / trail / catastrophe / max-hold rails and executes exits.

    python3 -m sniper_office.desk.filler --config filler.json

Paper only: no key, no signing, no chain write, no import from the source project. The one network read is a public price lookup (api.dexscreener.com, https, no redirects, no key).
That price is a PROVIDER CLAIM, not a proven fill: it carries no timestamp of its own, so every fill, mark and exit records the price, the source, the host and our fetch time.
Unknown price (or liquidity, or clock) => no fill and no mark value; the reason is written down. A price is never invented, defaulted or carried over from an earlier fetch.

At-most-once: an order's id IS its request id (primary key), and a request's final outcome (filled, refused or accepted) is written in the same ledger transaction as the rows it causes,
so a restart re-reads the requests and skips every one that already has an order or an outcome. The supervisor's apply_action receipts are turn/lease based and do not fit a poller;
the same idea (one durable key, one transaction) is used directly."""
import argparse
from contextlib import closing
import fcntl
import json
import logging
import math
from pathlib import Path
import sqlite3
import time
import urllib.error
import urllib.request

from .. import clock as K
from . import chains as CH, ticks as TK

log = logging.getLogger('desk.filler')
HOST = 'api.dexscreener.com'
SOURCE = 'dexscreener:provider_claim'
# Used only when book_policy does not carry the key; every fill records the values it used and where they came from.
DEFAULTS = dict(fill_model_version='desk-paper-v1', slippage_bps=50., fee_bps=100., max_impact_bps=1000., mark_max_age_s=120., entry_timeout_s=60., max_hold_s=3600.,
                catastrophe_pct=60., sl_pct=30., tp_pct=100., trail_pct=20.)
MAX_SLIP_BPS = 9500.
SCHEMA = '''
CREATE TABLE IF NOT EXISTS orders(order_id TEXT PRIMARY KEY, book_id TEXT NOT NULL, epoch_id TEXT NOT NULL, network_id TEXT, mint TEXT NOT NULL, pool TEXT, state TEXT NOT NULL,
  reason TEXT, requested_at REAL NOT NULL, filled_at REAL, closed_at REAL, spend_usd REAL, exit_usd REAL, costs_usd REAL NOT NULL DEFAULT 0, exit_reason TEXT, exit_fill_kind TEXT,
  exit_requested_at REAL, qty REAL, entry_price_usd REAL, peak_value_usd REAL, policy_json TEXT, fill_proof TEXT, exit_proof TEXT);
CREATE TABLE IF NOT EXISTS marks(id INTEGER PRIMARY KEY, order_id TEXT NOT NULL, at REAL NOT NULL, lamports TEXT, price_usd REAL, value_usd REAL, source TEXT, reason TEXT);
CREATE INDEX IF NOT EXISTS marks_order_at ON marks(order_id, at);
CREATE TABLE IF NOT EXISTS cashflows(id INTEGER PRIMARY KEY, order_id TEXT NOT NULL, kind TEXT NOT NULL, at REAL NOT NULL, usd REAL NOT NULL, UNIQUE(order_id, kind));
CREATE TABLE IF NOT EXISTS capital_flows(id INTEGER PRIMARY KEY, book_id TEXT NOT NULL, at REAL NOT NULL, kind TEXT NOT NULL, usd REAL NOT NULL, note TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS capital_initial ON capital_flows(book_id) WHERE kind='initial';
CREATE TABLE IF NOT EXISTS request_outcomes(request_id TEXT PRIMARY KEY, kind TEXT NOT NULL, outcome TEXT NOT NULL, reason TEXT, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS fill_attempts(id INTEGER PRIMARY KEY, request_id TEXT NOT NULL, at REAL NOT NULL, outcome TEXT NOT NULL, reason TEXT);
CREATE INDEX IF NOT EXISTS fill_attempts_request ON fill_attempts(request_id, id);
'''


def finite(v):
    return type(v) in (int, float) and math.isfinite(v)


def open_ledger(path):
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript(SCHEMA)
    return conn


# ---- price source -------------------------------------------------------------------------------------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def http_fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'sniper-office-desk/1'})
    try:
        with _OPENER.open(req, timeout=5) as r:
            return r.status, json.loads(r.read(2_000_000))
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (OSError, ValueError):
        return None, None


class Prices:
    """USD price and liquidity for a mint from the public pair lookup. Every answer is typed: state known with the numbers, or state unknown with a reason. A 429 pauses all lookups 60 s."""
    def __init__(self, clock, fetch=http_fetch):
        self.clock, self.fetch, self.stop_until = clock, fetch, 0.

    def read(self, mint, pool, chain='solana'):
        now = self.clock()
        if now < self.stop_until:
            return dict(state='unknown', reason='provider_rate_limited')
        if not (isinstance(mint, str) and mint.isalnum() and (pool is None or isinstance(pool, str) and pool.isalnum()) and CH.valid_chain(chain)):
            return dict(state='unknown', reason='bad_address')
        url = 'https://%s/latest/dex/pairs/%s/%s' % (HOST, chain, pool) if pool else 'https://%s/latest/dex/tokens/%s' % (HOST, mint)
        status, doc = self.fetch(url)
        at = self.clock()
        if status == 429:
            self.stop_until = at + 60.
            return dict(state='unknown', reason='provider_rate_limited')
        if status != 200 or not isinstance(doc, dict):
            return dict(state='unknown', reason='provider_unavailable')
        pairs = doc.get('pairs') or ([doc['pair']] if isinstance(doc.get('pair'), dict) else [])
        pairs = [p for p in pairs if isinstance(p, dict) and p.get('chainId') == chain and CH.same_address((p.get('baseToken') or {}).get('address'), mint) and (not pool or CH.same_address(p.get('pairAddress'), pool))]
        liq = lambda p: float((p.get('liquidity') or {}).get('usd') or 0) if finite((p.get('liquidity') or {}).get('usd')) else 0.
        pair = max(pairs, key=liq, default=None)
        if pair is None:
            return dict(state='unknown', reason='no_matching_pair')
        try:
            price = float(pair.get('priceUsd'))
        except (TypeError, ValueError):
            return dict(state='unknown', reason='price_missing')
        if not (math.isfinite(price) and price > 0):
            return dict(state='unknown', reason='price_invalid')
        return dict(state='known', price_usd=price, liquidity_usd=liq(pair) or None, pair=pair.get('pairAddress'), dex=pair.get('dexId'), source=SOURCE, provider=HOST, fetched_at=at)


# ---- fill model ---------------------------------------------------------------------------------------------------------------------------------------------------------

def slip_bps(pol, notional_usd, liquidity_usd):
    """Base slippage plus a constant-product impact estimate, 2 * notional / liquidity (liquidity_usd is both sides, so the quote side is half). None when liquidity is unknown."""
    if not finite(liquidity_usd) or liquidity_usd <= 0:
        return None, None
    impact = 2. * notional_usd / liquidity_usd * 1e4
    return min(MAX_SLIP_BPS, pol['slippage_bps'] + impact), impact


def pol_of(policy, rails):
    """The frozen per-order policy: book_policy values where present, else DEFAULTS; seat rails override sl/tp/trail. 'defaulted' names what the book policy did not state."""
    pol, defaulted = {}, []
    for k, d in DEFAULTS.items():
        v = policy.get(k)
        if not (isinstance(v, str) if k == 'fill_model_version' else finite(v)):
            v = d
            defaulted.append(k)
        pol[k] = v
    for k in ('sl_pct', 'tp_pct', 'trail_pct'):
        if finite(rails.get(k)):
            pol[k] = float(rails[k])
            if k in defaulted:
                defaulted.remove(k)
    pol['clip_usd'] = policy.get('clip_usd')
    pol['max_concurrent'] = policy.get('max_concurrent')
    pol['daily_loss_usd'] = policy.get('daily_loss_usd')
    pol['defaulted'] = sorted(defaulted)
    return pol


def rail(pol, filled_at, spend, peak, value, now):
    """The first rail hit, or None. value is the net exit value (after modelled exit slippage and fee)."""
    if now - filled_at >= pol['max_hold_s']:
        return 'max_hold'
    if value is None or spend <= 0:
        return None
    pnl = (value / spend - 1.) * 100.
    if pol['catastrophe_pct'] > 0 and pnl <= -pol['catastrophe_pct']:
        return 'catastrophe'
    if pol['sl_pct'] > 0 and pnl <= -pol['sl_pct']:
        return 'stop'
    if pol['tp_pct'] > 0 and pnl >= pol['tp_pct']:
        return 'tp'
    if pol['trail_pct'] > 0 and peak > spend and value <= peak * (1 - pol['trail_pct'] / 100.):
        return 'trail'
    return None


def exit_value(pol, qty, quote):
    """(gross proceeds, fee, net, slip_bps) for selling qty at the quoted price, or None when liquidity is unknown."""
    notional = qty * quote['price_usd']
    slip, impact = slip_bps(pol, notional, quote['liquidity_usd'])
    if slip is None:
        return None
    gross = notional * (1 - slip / 1e4)
    fee = gross * pol['fee_bps'] / 1e4
    return gross, fee, gross - fee, slip


# ---- the engine ---------------------------------------------------------------------------------------------------------------------------------------------------------

class Filler:
    def __init__(self, cfg, *, clock=time.time, fetch=http_fetch):
        self.cfg, self.clock, self.prices = cfg, clock, Prices(clock, fetch)
        self.books = {a: b['book_id'] for b in cfg['books'] for a in b['actor_ids']}
        self.ledger = open_ledger(cfg['ledger_db'])
        self.policies = {}   # a book may name its own policy file (book_policy); the rest share the top-level one
        for b in cfg['books']:
            try:
                doc = json.loads(Path(b.get('book_policy') or cfg['book_policy']).read_text())['policy']
            except (OSError, ValueError, KeyError, TypeError):
                raise SystemExit('book_policy_unreadable')
            self.policies[b['book_id']] = doc if isinstance(doc, dict) else {}
        self.mark_every = {b['book_id']: b.get('mark_every_s', cfg.get('mark_every_s', 15.)) for b in cfg['books']}
        self.ticks = None   # the 1 s recorder's db (config ticks_db): a tick fresher than tick_max_age_s is the quote, else one public lookup
        self.last_mark = {}
        now = clock()
        for b in cfg['books']:
            if not finite(b.get('capital_usd')) or b['capital_usd'] <= 0:
                raise SystemExit('capital_usd_required:' + b['book_id'])
            self.ledger.execute("INSERT OR IGNORE INTO capital_flows(book_id,at,kind,usd,note) VALUES(?,?,'initial',?,'paper capital, filler config')", (b['book_id'], now, float(b['capital_usd'])))

    def quote(self, mint, pool, chain):
        """The price answer for a coin: the newest recorded tick when fresh, else one public lookup. Unknown stays unknown."""
        chain = chain or 'solana'
        if self.cfg.get('ticks_db'):
            try:
                if self.ticks is None:
                    self.ticks = TK.ro(self.cfg['ticks_db'])
                hit = TK.latest(self.ticks, chain, mint, self.clock(), self.cfg.get('tick_max_age_s', 5.))
            except sqlite3.Error:
                self.ticks, hit = None, None
            if hit:
                return hit
        return self.prices.read(mint, pool, chain)

    def corrected(self):
        return K.corrected_now(self.cfg['clock_file'], host_now=self.clock())

    def requests(self):
        try:
            with closing(sqlite3.connect(Path(self.cfg['office_db']).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as conn:
                rows = conn.execute('SELECT request_id,kind,epoch_id,actor_id,at,ticket_id,network_id,mint,pool,order_id,reason,detail_json FROM desk_paper_requests ORDER BY at,rowid').fetchall()
                halts = {r[0]: r[1] for r in conn.execute('SELECT epoch_id,action FROM desk_halts WHERE seq IN (SELECT MAX(seq) FROM desk_halts GROUP BY epoch_id)')}
        except sqlite3.Error:
            return [], {}
        keys = 'request_id kind epoch_id actor_id at ticket_id network_id mint pool order_id reason detail_json'.split()
        return [dict(zip(keys, r)) for r in rows], halts

    def done(self, request_id):
        return self.ledger.execute('SELECT 1 FROM request_outcomes WHERE request_id=?', (request_id,)).fetchone() is not None

    def outcome(self, req, outcome, reason, now):
        self.ledger.execute('INSERT OR IGNORE INTO request_outcomes VALUES(?,?,?,?,?)', (req['request_id'], req['kind'], outcome, reason, now))

    def attempt(self, req, outcome, reason, now):
        self.ledger.execute('INSERT INTO fill_attempts(request_id,at,outcome,reason) VALUES(?,?,?,?)', (req['request_id'], now, outcome, reason))

    def cash(self, book):
        cap = self.ledger.execute('SELECT COALESCE(SUM(usd),0) FROM capital_flows WHERE book_id=?', (book,)).fetchone()[0]
        return cap + self.ledger.execute('SELECT COALESCE(SUM(c.usd),0) FROM cashflows c JOIN orders o USING(order_id) WHERE o.book_id=?', (book,)).fetchone()[0]

    def realized_today(self, book, now):
        return self.ledger.execute("SELECT COALESCE(SUM(c.usd),0) FROM cashflows c JOIN orders o USING(order_id) WHERE o.book_id=? AND o.state='closed' AND o.closed_at>=?", (book, now - now % 86400)).fetchone()[0]

    # -- entries
    def fill_entry(self, req, now, now_host, halts):
        if self.done(req['request_id']) or self.ledger.execute('SELECT 1 FROM orders WHERE order_id=?', (req['request_id'],)).fetchone():
            return 'skipped'
        book = self.books.get(req['actor_id'])
        if book is None:
            return 'no_book'   # not this filler's seat: left alone, not recorded
        detail = json.loads(req['detail_json'] or '{}')
        pol = pol_of(self.policies[book], detail.get('rails') or {})
        if now_host - req['at'] > pol['entry_timeout_s']:
            last = self.ledger.execute('SELECT reason FROM fill_attempts WHERE request_id=? ORDER BY id DESC LIMIT 1', (req['request_id'],)).fetchone()
            self.outcome(req, 'censored', last[0] if last else 'entry_timeout', now)
            return 'censored'
        why = self.entry_refusal(req, book, pol, now, halts)
        if why:
            return self.defer(req, why, now)
        clip = pol['clip_usd']
        quote = self.quote(req['mint'], req['pool'], req['network_id'])
        if quote['state'] != 'known':
            return self.defer(req, quote['reason'], now)
        if not finite(quote['liquidity_usd']):
            return self.defer(req, 'liquidity_unknown', now)
        slip, impact = slip_bps(pol, clip, quote['liquidity_usd'])
        if impact > pol['max_impact_bps']:
            return self.defer(req, 'impact_too_high', now)
        price = quote['price_usd'] * (1 + slip / 1e4)
        qty, fee = clip / price, clip * pol['fee_bps'] / 1e4
        if self.cash(book) < clip + fee:
            self.outcome(req, 'censored', 'insufficient_capital', now)
            return 'censored'
        proof = dict(quote, request_at=req['at'], filled_host_at=now_host, filled_at=now, slippage_bps=slip, impact_bps=impact, fill_price_usd=price, fee_bps=pol['fee_bps'], fee_usd=fee, note='priced from one provider lookup at fill time; no landing proof')
        try:
            self.ledger.execute('BEGIN IMMEDIATE')
            self.ledger.execute("INSERT INTO orders(order_id,book_id,epoch_id,network_id,mint,pool,state,requested_at,filled_at,spend_usd,costs_usd,qty,entry_price_usd,peak_value_usd,policy_json,fill_proof) VALUES(?,?,?,?,?,?,'open',?,?,?,?,?,?,?,?,?)",
                                (req['request_id'], book, req['epoch_id'], req['network_id'], req['mint'], req['pool'], req['at'], now, clip, fee, qty, price, clip, json.dumps(pol, sort_keys=True), json.dumps(proof, sort_keys=True)))
            self.ledger.execute("INSERT INTO cashflows(order_id,kind,at,usd) VALUES(?,'entry',?,?)", (req['request_id'], now, -(clip + fee)))
            self.outcome(req, 'filled', None, now)
            self.ledger.execute('COMMIT')
        except sqlite3.Error:
            self.ledger.execute('ROLLBACK')
            raise
        return 'filled'

    def entry_refusal(self, req, book, pol, now, halts):
        if halts.get(req['epoch_id']) == 'halt':
            return 'book_halted'
        if not finite(pol['clip_usd']) or pol['clip_usd'] <= 0:
            return 'clip_usd_unknown'
        cap = pol['max_concurrent']
        if finite(cap) and self.ledger.execute("SELECT COUNT(*) FROM orders WHERE book_id=? AND state='open'", (book,)).fetchone()[0] >= cap:
            return 'max_concurrent'
        if finite(pol['daily_loss_usd']) and self.realized_today(book, now) <= -pol['daily_loss_usd']:
            return 'daily_loss_halted'
        return None

    def defer(self, req, why, now):
        self.attempt(req, 'no_fill', why, now)
        return 'deferred:' + why

    # -- exits
    def take_exit_request(self, req, now):
        """Accept an exit request onto its order, once: the request outcome and the order's exit_requested_at are one transaction."""
        if self.done(req['request_id']) or req['actor_id'] not in self.books:
            return
        book = self.books[req['actor_id']]
        order = self.ledger.execute('SELECT state FROM orders WHERE order_id=? AND book_id=?', (req['order_id'], book)).fetchone()
        if order is None:
            if self.done(req['order_id']):   # its entry was censored: there is nothing to exit
                self.outcome(req, 'refused', 'order_not_open', now)
            return   # entry not decided yet
        if order[0] != 'open':
            self.outcome(req, 'refused', 'order_not_open', now)
            return
        self.ledger.execute('BEGIN IMMEDIATE')
        self.ledger.execute('UPDATE orders SET exit_requested_at=COALESCE(exit_requested_at,?),exit_reason=COALESCE(exit_reason,?) WHERE order_id=?', (now, 'requested:' + req['reason'][:100], req['order_id']))
        self.outcome(req, 'accepted', None, now)
        self.ledger.execute('COMMIT')

    def manage(self, row, now, now_host):
        pol = json.loads(row['policy_json'])
        quote = self.quote(row['mint'], row['pool'], row['network_id'])
        ev = quote['state'] == 'known' and exit_value(pol, row['qty'], quote)
        if not ev:
            reason = quote.get('reason') if quote['state'] != 'known' else 'liquidity_unknown'
            self.ledger.execute('INSERT INTO marks(order_id,at,source,reason) VALUES(?,?,?,?)', (row['order_id'], now, SOURCE, reason))
            return 'unpriced'
        gross, fee, net, slip = ev
        peak = max(row['peak_value_usd'] or 0., net)
        self.ledger.execute('INSERT INTO marks(order_id,at,price_usd,value_usd,source) VALUES(?,?,?,?,?)', (row['order_id'], now, quote['price_usd'], net, SOURCE))
        reason = 'requested' if row['exit_requested_at'] is not None else rail(pol, row['filled_at'], row['spend_usd'], peak, net, now)
        if reason is None:
            self.ledger.execute('UPDATE orders SET peak_value_usd=? WHERE order_id=?', (peak, row['order_id']))
            return 'marked'
        proof = dict(quote, exit_host_at=now_host, exit_at=now, slippage_bps=slip, fee_bps=pol['fee_bps'], fee_usd=fee, rail=reason, note='priced from the same provider lookup that triggered the exit; no landing proof')
        kind = 'priced_at_trigger_fetch'
        self.ledger.execute('BEGIN IMMEDIATE')
        cur = self.ledger.execute("UPDATE orders SET state='closed',closed_at=?,exit_usd=?,costs_usd=costs_usd+?,exit_reason=COALESCE(exit_reason,?),exit_fill_kind=?,exit_proof=?,peak_value_usd=? WHERE order_id=? AND state='open'",
                                  (now, gross, fee, reason, kind, json.dumps(proof, sort_keys=True), peak, row['order_id']))
        if cur.rowcount == 1:
            self.ledger.execute("INSERT INTO cashflows(order_id,kind,at,usd) VALUES(?,'exit',?,?)", (row['order_id'], now, net))
        self.ledger.execute('COMMIT')
        return 'closed:' + reason

    def tick(self):
        now_host = self.clock()
        now = self.corrected()
        if now is None:
            return dict(state='clock_unknown')
        reqs, halts = self.requests()
        results = []
        for req in reqs:
            if req['kind'] == 'order':
                results.append(self.fill_entry(req, now, now_host, halts))
        for req in reqs:
            if req['kind'] == 'exit':
                self.take_exit_request(req, now)
        self.ledger.row_factory = sqlite3.Row
        try:
            for row in self.ledger.execute("SELECT * FROM orders WHERE state='open' ORDER BY filled_at").fetchall():
                if row['exit_requested_at'] is None and now_host - self.last_mark.get(row['order_id'], -1e18) < self.mark_every.get(row['book_id'], 15.):
                    continue
                self.last_mark[row['order_id']] = now_host
                results.append(self.manage(row, now, now_host))
        finally:
            self.ledger.row_factory = None
        return dict(state='ok', at=now, results=[r for r in results if r not in ('skipped', 'no_book')])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--once', action='store_true', help='one tick, then exit')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    cfg = json.loads(args.config.read_text())
    Path(cfg['ledger_db']).parent.mkdir(parents=True, exist_ok=True)
    with open(str(cfg['ledger_db']) + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)   # one filler per ledger
        filler = Filler(cfg)
        while True:
            try:
                out = filler.tick()
                if out['state'] != 'ok' or out['results']:
                    log.info(json.dumps(out, default=str))
            except Exception:   # loud and alive: the next tick re-reads everything from the durable tables
                log.exception('tick failed')
            if args.once:
                return
            time.sleep(cfg.get('tick_s', 5.))


if __name__ == '__main__':
    main()
