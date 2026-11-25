"""Paper-book tools for the desk plug-in. Read-only on the source project's ledger and desk files; every write goes to the seat's own office db (tables desk_*).

Nothing here can sign, fund or reach a live path. Missing config or data is state 'unknown' with a reason, never a default. What the standalone cannot rebuild is said in the result:
the full sheet builder and the fill engine live in the source project, so sheet_read shows the stored hard-filter evidence (state partial) and paper_order/paper_exit record
a request (state queued, filled false) for whatever fills paper orders to pick up. Nothing in this package fills them."""
from contextlib import closing
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import uuid

from .. import clock as K
from ..seat_tools import MAX_OUTPUT, result

MAX_SHEET = 48000
RESULTS_MAX = 60
FEED_MAX = 50
ORDER_ROLES = ('judge', 'judge_gpt')
RESULTS_ROLES = ('head', 'scout') + ORDER_ROLES
BOOK_ROLES = ('head',) + ORDER_ROLES
SEAT_RAILS = ('sl_pct', 'tp_pct', 'trail_pct')
FEED_FACTS = dict(top10_pct=('holders', 'top10_pct'), creator_pct=('holders', 'creator_pct'), cluster_status=('cluster', 'status'), cluster_reason=('cluster', 'reason'),
                  dump_buyer_share=('dump_group', 'buyer_share'), sell_round_trip_loss_pct=('sell_probe', 'round_trip_loss_pct'))
FILTER_OPS = {'gt': lambda a, b: a > b, 'lt': lambda a, b: a < b, 'gte': lambda a, b: a >= b, 'lte': lambda a, b: a <= b, 'eq': lambda a, b: a == b, 'ne': lambda a, b: a != b}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS desk_feed_filters(seq INTEGER PRIMARY KEY AUTOINCREMENT,epoch_id TEXT NOT NULL,actor_id TEXT NOT NULL,at REAL NOT NULL,rules_json TEXT NOT NULL,reason TEXT NOT NULL,after_case TEXT);
CREATE TABLE IF NOT EXISTS desk_feed_hidden(epoch_id TEXT NOT NULL,actor_id TEXT NOT NULL,ticket_id TEXT NOT NULL,filter_seq INTEGER NOT NULL,rule_json TEXT NOT NULL,at REAL NOT NULL,PRIMARY KEY(epoch_id,actor_id,ticket_id,filter_seq));
CREATE TABLE IF NOT EXISTS desk_candidates(epoch_id TEXT NOT NULL,network_id TEXT NOT NULL,mint TEXT NOT NULL,pool TEXT,actor_id TEXT NOT NULL,known_at REAL NOT NULL,corrected_known_at REAL,PRIMARY KEY(epoch_id,network_id,mint));
CREATE TABLE IF NOT EXISTS desk_halts(seq INTEGER PRIMARY KEY AUTOINCREMENT,epoch_id TEXT NOT NULL,action TEXT NOT NULL CHECK(action IN ('halt','resume')),actor_id TEXT NOT NULL,reason TEXT NOT NULL,at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS desk_results_reads(epoch_id TEXT NOT NULL,actor_id TEXT NOT NULL,sha256 TEXT NOT NULL,first_at REAL,last_at REAL,items_n INTEGER,reads INTEGER,PRIMARY KEY(epoch_id,actor_id,sha256));
CREATE TABLE IF NOT EXISTS desk_paper_requests(request_id TEXT PRIMARY KEY,kind TEXT NOT NULL CHECK(kind IN ('order','exit')),epoch_id TEXT NOT NULL,actor_id TEXT NOT NULL,incarnation INTEGER NOT NULL,at REAL NOT NULL,
  ticket_id TEXT,network_id TEXT,mint TEXT,pool TEXT,order_id TEXT,reason TEXT NOT NULL,detail_json TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS desk_paper_requests_coin ON desk_paper_requests(epoch_id,network_id,mint) WHERE kind='order';
CREATE UNIQUE INDEX IF NOT EXISTS desk_paper_requests_exit ON desk_paper_requests(epoch_id,order_id) WHERE kind='exit';
'''


def out(doc, error=False):
    return result(json.dumps(doc, default=str), error)


def unknown(reason, **more):
    return out(dict(state='unknown', reason=reason, **more), True)


def refused(reason, **more):
    return out(dict(state='refused', reason=reason, **more), True)


def path_of(tools, key):
    value = tools.cfg.get(key)
    return Path(value) if isinstance(value, str) and value else None


def ro(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=1.)


def own(tools):
    """The seat's office db, with the plug-in's own tables. Writes commit on exit."""
    conn = sqlite3.connect(tools.db_path, timeout=5)
    conn.executescript(SCHEMA)
    return conn


def role_of(tools):
    """The seat's role in this epoch now, or None for a missing, sealed, foreign-epoch or stale-incarnation seat."""
    with closing(sqlite3.connect(tools.db_path, timeout=5)) as conn:
        row = conn.execute('SELECT role,epoch_id,sealed,incarnation FROM office_v2_roster WHERE actor_id=?', (tools.actor,)).fetchone()
    return None if row is None or row[1] != tools.epoch or row[2] or row[3] != tools.incarnation else row[0]


def book_id(tools):
    return tools.cfg.get('ledger_epoch') or tools.epoch


def policy_of(tools):
    """The book policy dict, or None when the file is not configured or not readable."""
    path = path_of(tools, 'book_policy')
    try:
        policy = json.loads(path.read_text())['policy'] if path else None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return policy if isinstance(policy, dict) else None


def ledger_paths(tools):
    listed = tools.cfg.get('results_ledgers')
    paths = [Path(p) for p in listed if isinstance(p, str) and p] if isinstance(listed, list) else []
    own_path = path_of(tools, 'ledger_db')
    return paths or ([own_path] if own_path else [])


def entry_halt(conn, epoch):
    row = conn.execute('SELECT action,actor_id,at,reason FROM desk_halts WHERE epoch_id=? ORDER BY seq DESC LIMIT 1', (epoch,)).fetchone()
    if row is None:
        return dict(state='never_set', entries_halted=False, epoch_id=epoch, receipt=None)
    return dict(state='halted' if row[0] == 'halt' else 'resumed', entries_halted=row[0] == 'halt', epoch_id=epoch, receipt=dict(action=row[0], actor_id=row[1], at=row[2], reason=row[3]))


def valid_rules(rules):
    if not isinstance(rules, list) or len(rules) > 20:
        return False
    return all(isinstance(r, dict) and set(r) == {'path', 'op', 'value'} and isinstance(r['path'], str) and 0 < len(r['path']) <= 80 and r['op'] in FILTER_OPS
               and (type(r['value']) in (str, bool) or type(r['value']) in (int, float) and math.isfinite(r['value'])) for r in rules)


def rule_hides(rule, checks):
    """Missing data never hides a ticket: an absent field, or one of another type, leaves it visible."""
    value = checks
    for part in rule['path'].split('.'):
        value = value.get(part) if isinstance(value, dict) else None
    numeric = lambda v: type(v) in (int, float) and math.isfinite(v)
    if value is None or not (numeric(value) and numeric(rule['value']) or type(value) is type(rule['value']) and type(value) in (str, bool)):
        return False
    return FILTER_OPS[rule['op']](value, rule['value'])


def _stamp(value):
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def _stored_tickets(tools, now, limit):
    """Newest hard-filter PASS tickets in the desk db: (rows, None) or (None, typed refusal). A db whose newest row is older than feed_stale_s is unknown, not an empty feed."""
    path = path_of(tools, 'desk_db')
    if path is None:
        return None, unknown('desk_config_unknown')
    stale = tools.cfg.get('feed_stale_s', 900)
    window = tools.cfg.get('feed_window_s', 3600)
    try:
        with closing(ro(path)) as conn:
            rows = conn.execute("SELECT ticket_id,mint,pool,ts,substr(inputs_json,1,65536) FROM desk_tickets WHERE stage='hard_filter' AND verdict='PASS' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    except (OSError, sqlite3.Error):
        return None, unknown('desk_unreadable')
    newest = _stamp(rows[0][3]) if rows else None
    if newest is None:
        return None, unknown('desk_has_no_dated_tickets')
    if now - newest > stale:
        return None, unknown('desk_stale', newest_age_s=round(now - newest), stale_after_s=stale)
    return [r for r in rows if (_stamp(r[3]) or 0) >= now - window], None


def _checks(blob):
    try:
        return json.loads(blob)['evidence']['checks']
    except (ValueError, TypeError, KeyError):
        return {}


def _now(tools):
    return K.corrected_now(tools.cfg.get('clock_file') or '', host_now=tools.clock())


def feed_list(tools, include_hidden=False):
    role = role_of(tools)
    if role is None:
        return refused('seat_not_in_epoch')
    now = _now(tools)
    if now is None:
        return unknown('clock_unknown')
    rows, bad = _stored_tickets(tools, now, 200)
    if bad:
        return bad
    with closing(own(tools)) as conn, conn:
        row = conn.execute('SELECT seq,rules_json FROM desk_feed_filters WHERE epoch_id=? AND actor_id=? ORDER BY seq DESC LIMIT 1', (tools.epoch, tools.actor)).fetchone()
        rules = json.loads(row[1]) if row else []
        items, hidden = [], 0
        for ticket_id, mint, pool, ts, blob in rows[:FEED_MAX * 2]:
            checks = _checks(blob)
            item = dict(ticket_id=ticket_id, mint=mint, pool=pool, known_at=_stamp(ts), age_s=round(now - (_stamp(ts) or now)),
                        facts={k: checks.get(a, {}).get(b) if isinstance(checks.get(a), dict) else None for k, (a, b) in FEED_FACTS.items()})
            rule = next((r for r in rules if rule_hides(r, checks)), None)
            if rule is not None:
                hidden += 1
                conn.execute('INSERT OR IGNORE INTO desk_feed_hidden VALUES(?,?,?,?,?,?)', (tools.epoch, tools.actor, ticket_id, row[0], json.dumps(rule), now))
            if rule is None:
                items.append(item)
            elif include_hidden is True:
                items.append(dict(item, hidden_by=rule))
    doc = dict(state='partial', feed='stored_hard_filter_pass', items=items[:FEED_MAX], hidden_count=hidden,
               note='stored hard-filter passes from the desk db, newest first; the near-pass feed, run-up, closes and followed-wallet facts are not built here')
    dropped = 0
    while doc['items'] and len(json.dumps(doc, default=str)) > MAX_OUTPUT:   # never a silent prefix
        doc['items'].pop()
        dropped += 1
    if dropped:
        doc['dropped'] = dropped
    return out(doc)


def feed_filter_set(tools, rules, reason, after_case=None):
    if role_of(tools) is None:
        return refused('seat_not_in_epoch')
    if type(reason) is not str or not reason.strip():
        return refused('reason_required')
    if not valid_rules(rules):
        return refused('rules_invalid', hint='up to 20 of {path,op,value}; op gt lt gte lte eq ne; path is dotted under the ticket checks, e.g. holders.top10_pct')
    with closing(own(tools)) as conn, conn:
        conn.execute('INSERT INTO desk_feed_filters(epoch_id,actor_id,at,rules_json,reason,after_case) VALUES(?,?,?,?,?,?)',
                     (tools.epoch, tools.actor, tools.clock(), json.dumps(rules), reason[:500], None if after_case is None else str(after_case)[:128]))
    return out(dict(state='known', rules=rules))


def sheet_read(tools, ticket_id):
    if not isinstance(ticket_id, str) or not ticket_id or len(ticket_id) > 256:
        raise ValueError('bad_arguments')
    if role_of(tools) is None:
        return refused('seat_not_in_epoch')
    path = path_of(tools, 'desk_db')
    if path is None:
        return unknown('desk_config_unknown')
    try:
        with closing(ro(path)) as conn:
            row = conn.execute("SELECT mint,pool,verdict,reason,ts,substr(inputs_json,1,65536) FROM desk_tickets WHERE ticket_id=? AND stage='hard_filter' ORDER BY id DESC LIMIT 1", (ticket_id,)).fetchone()
    except (OSError, sqlite3.Error):
        return unknown('desk_unreadable')
    if row is None:
        return refused('ticket_unknown')
    try:
        evidence = json.loads(row[5]).get('evidence')
    except (ValueError, AttributeError):
        evidence = None
    if not isinstance(evidence, dict):
        return unknown('ticket_evidence_unreadable', ticket_id=ticket_id)
    doc = dict(state='partial', reason='full_sheet_not_built_here', ticket_id=ticket_id, mint=row[0], pool=row[1], hard_filter=dict(verdict=row[2], reason=row[3], at=row[4]),
               evidence=evidence, note='the stored hard-filter evidence for this ticket; the live sheet (pool state, flow, safety evidence) is not rebuilt in this package')
    text = json.dumps(doc, default=str, separators=(',', ':'))
    if len(text) > MAX_SHEET:
        text = json.dumps(dict(state='partial', reason='sheet_too_large', sheet_bytes=len(text)))
    return result(text, False)


def results_view(tools):
    role = role_of(tools)
    if role is None:
        return refused('seat_not_in_epoch')
    if role not in RESULTS_ROLES:
        return refused('role_cannot_view_results')
    paths = ledger_paths(tools)
    if not paths:
        return unknown('ledger_config_unknown')
    rows = []
    try:
        for path in paths:
            with closing(ro(path)) as conn:
                conn.row_factory = sqlite3.Row
                rows += [dict(r) for r in conn.execute("SELECT order_id,epoch_id,mint,state,reason,exit_reason,requested_at,filled_at,closed_at,spend_usd,exit_usd,costs_usd,exit_fill_kind FROM orders "
                                                       "WHERE state IN ('closed','censored') ORDER BY requested_at DESC LIMIT ?", (RESULTS_MAX + 1,))]
    except (OSError, sqlite3.Error):
        return unknown('ledger_unreadable')   # never an empty list: that reads as no results
    rows.sort(key=lambda r: r['requested_at'], reverse=True)
    items = [dict(r, net_usd=r['exit_usd'] - r['spend_usd'] - (r['costs_usd'] or 0.) if r['state'] == 'closed' and r['exit_usd'] is not None and r['spend_usd'] is not None else None) for r in rows[:RESULTS_MAX]]
    at = tools.clock()
    digest = hashlib.sha256(json.dumps(items, sort_keys=True, default=str).encode()).hexdigest()
    with closing(own(tools)) as conn, conn:   # what was shown, counted per distinct result set, before the seat sees it
        conn.execute('INSERT INTO desk_results_reads VALUES(?,?,?,?,?,?,1) ON CONFLICT(epoch_id,actor_id,sha256) DO UPDATE SET last_at=excluded.last_at,reads=reads+1', (tools.epoch, tools.actor, digest, at, at, len(items)))
    return out(dict(state='known', results_asof=at, truncated=len(rows) > RESULTS_MAX, items=items, results_sha256=digest,
                    note='modelled paper fills; no landing proof; small samples do not show skill; exit_fill_kind estimated_terminal is priced on an estimate, upper-biased, not outcome evidence'))


def book_view(tools):
    role = role_of(tools)
    if role is None:
        return refused('seat_not_in_epoch')
    if role not in BOOK_ROLES:
        return refused('role_cannot_view_book')
    ledger = path_of(tools, 'ledger_db')
    if ledger is None:
        return unknown('ledger_config_unknown')
    now = _now(tools)
    if now is None:
        return unknown('clock_unknown')
    policy = policy_of(tools) or {}
    cap, daily_loss, book = policy.get('max_concurrent'), policy.get('daily_loss_usd'), book_id(tools)
    try:
        with closing(ro(ledger)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT order_id,mint,pool,state,filled_at,spend_usd FROM orders WHERE book_id=? AND state IN ('pending','open') ORDER BY requested_at,order_id LIMIT 100", (book,)).fetchall()
            positions = []
            for r in rows:
                mark = conn.execute('SELECT at,lamports FROM marks WHERE order_id=? AND at<=? ORDER BY at DESC LIMIT 1', (r['order_id'], now)).fetchone()
                positions.append(dict(r, mark_at=mark['at'] if mark else None, mark_age_s=now - mark['at'] if mark else None, mark_lamports=mark['lamports'] if mark else None))
            cash = conn.execute('SELECT COALESCE(SUM(usd),0) FROM capital_flows WHERE book_id=? AND at<=?', (book, now)).fetchone()[0] \
                + conn.execute('SELECT COALESCE(SUM(c.usd),0) FROM cashflows c JOIN orders o USING(order_id) WHERE o.book_id=? AND c.at<=?', (book, now)).fetchone()[0]
            realized = conn.execute("SELECT COALESCE(SUM(c.usd),0) FROM cashflows c JOIN orders o USING(order_id) WHERE o.book_id=? AND o.state='closed' AND o.closed_at>=?", (book, now - now % 86400)).fetchone()[0]
    except (OSError, sqlite3.Error):
        return unknown('ledger_unreadable')
    # Open inventory needs the fill model's FX and exit cost to value; those are not rebuilt here, so equity is exact only for a flat book.
    equity = dict(state='known', usd=cash, cash_usd=cash, unpriced_count=0) if not positions else dict(state='unknown', reason='valuation_not_built_here', usd=None, cash_usd=cash, unpriced_count=len(positions))
    with closing(own(tools)) as conn:
        halt = entry_halt(conn, tools.epoch)
    doc = dict(book_id=book, as_of=now, positions=positions, positions_over_cap=cap is not None and len(positions) > cap, equity=equity, daily_realized_usd=realized,
               daily_loss_usd=daily_loss, daily_loss_halted=realized <= -daily_loss if isinstance(daily_loss, (int, float)) else None, entry_halt=halt)
    if daily_loss is None:
        doc['policy_state'] = 'unknown'
    doc['equity_snapshot_id'] = hashlib.sha256(json.dumps(dict(doc, entry_halt=None), sort_keys=True, default=str).encode()).hexdigest()
    text = json.dumps(doc, default=str)
    return result(text if len(text) <= MAX_OUTPUT else json.dumps(dict(state='unknown', reason='view_too_large')), 'equity' not in doc)


def candidate_register(tools, network_id, mint, pool=None):
    if role_of(tools) is None:
        return refused('seat_not_in_epoch')
    if any(type(v) is not str or not v.strip() or len(v) > 256 for v in (network_id, mint)) or (pool is not None and (type(pool) is not str or not pool or len(pool) > 256)):
        return refused('candidate_invalid')
    host = tools.clock()
    with closing(own(tools)) as conn, conn:
        conn.execute('INSERT OR IGNORE INTO desk_candidates VALUES(?,?,?,?,?,?,?)', (tools.epoch, network_id, mint, pool, tools.actor, host, _now(tools)))
        row = conn.execute('SELECT known_at,actor_id,corrected_known_at FROM desk_candidates WHERE epoch_id=? AND network_id=? AND mint=?', (tools.epoch, network_id, mint)).fetchone()
    return out(dict(state='registered', known_at=row[0], first_actor=row[1], corrected_known_at=row[2], clock_state='known' if row[2] is not None else 'unknown'))


def book_halt(tools, action, reason):
    role = role_of(tools)
    if role != 'head' or action not in ('halt', 'resume') or type(reason) is not str or not reason.strip():
        return refused('halt_not_allowed')
    with closing(own(tools)) as conn, conn:
        conn.execute('INSERT INTO desk_halts(epoch_id,action,actor_id,reason,at) VALUES(?,?,?,?,?)', (tools.epoch, action, tools.actor, reason[:500], tools.clock()))
    return out(dict(state=action))


def _rail_refusal(policy, rails):
    for name, value in rails.items():
        bound = (policy.get('rail_bounds') or {}).get(name) if name in SEAT_RAILS else None
        if not isinstance(bound, list) or len(bound) != 2 or type(value) not in (int, float) or not bound[0] <= value <= bound[1]:
            return 'rail_out_of_bounds:' + str(name)[:32]
    return None


def _queued(tools, kind, **fields):
    request_id = uuid.uuid4().hex
    with closing(own(tools)) as conn, conn:
        conn.execute('INSERT INTO desk_paper_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (request_id, kind, tools.epoch, tools.actor, tools.incarnation, tools.clock(), fields.get('ticket_id'), fields.get('network_id'), fields.get('mint'), fields.get('pool'),
                      fields.get('order_id'), fields['reason'], json.dumps(fields.get('detail') or {}, default=str)))
    return request_id


NOT_FILLED = 'recorded as a paper request only: nothing in this package fills paper orders, so there is no fill, no position and no result until a fill engine reads desk_paper_requests'


def paper_order(tools, ticket_id, reason, thesis=None, disproof=None, exit_plan=None, sl_pct=None, tp_pct=None, trail_pct=None):
    if not isinstance(ticket_id, str) or not ticket_id or len(ticket_id) > 256:
        raise ValueError('bad_arguments')
    if type(reason) is not str or not reason.strip() or len(reason) > 160:
        return refused('reason_invalid')
    plan = dict(thesis=thesis, disproof=disproof, exit_plan=exit_plan)
    if not all(isinstance(v, str) and v.strip() and len(v) <= 400 for v in plan.values()):
        return refused('plan_required', hint='thesis, disproof and exit_plan: each one non-empty line, at most 400 characters')
    if role_of(tools) not in ORDER_ROLES:
        return refused('role_cannot_order')
    rails = {k: v for k, v in (('sl_pct', sl_pct), ('tp_pct', tp_pct), ('trail_pct', trail_pct)) if v is not None}
    if rails:
        policy = policy_of(tools)
        if policy is None:
            return unknown('book_policy_unknown')
        bad = _rail_refusal(policy, rails)
        if bad:
            return refused(bad)
    with closing(own(tools)) as conn:
        if entry_halt(conn, tools.epoch)['entries_halted']:
            return refused('book_halted')
    path = path_of(tools, 'desk_db')
    if path is None:
        return unknown('desk_config_unknown')
    try:
        with closing(ro(path)) as conn:
            ticket = conn.execute("SELECT network_id,mint,pool FROM desk_tickets WHERE ticket_id=? AND stage='hard_filter' AND verdict='PASS' ORDER BY id DESC LIMIT 1", (ticket_id,)).fetchone()
    except (OSError, sqlite3.Error):
        return unknown('desk_unreadable')
    if ticket is None:
        return refused('ticket_unknown')
    network_id, mint, pool = ticket
    ledger = path_of(tools, 'ledger_db')
    if ledger is not None:   # one entry per coin per book: an order already in the ledger counts, an unreadable ledger is unknown
        try:
            with closing(ro(ledger)) as conn:
                prior = conn.execute('SELECT order_id,state FROM orders WHERE book_id=? AND mint=? LIMIT 1', (book_id(tools), mint)).fetchone()
        except (OSError, sqlite3.Error):
            return unknown('ledger_unreadable')
        if prior:
            return out(dict(state='duplicate', reason='coin_already_ordered', order_id=prior[0]), False)
    try:
        request_id = _queued(tools, 'order', ticket_id=ticket_id, network_id=network_id, mint=mint, pool=pool, reason=reason, detail=dict(plan=plan, rails=rails))
    except sqlite3.IntegrityError:
        return out(dict(state='duplicate', reason='coin_already_requested'), False)
    return out(dict(state='queued', request_id=request_id, order_id=None, filled=False, note=NOT_FILLED))


def paper_exit(tools, order_id, reason):
    if type(order_id) is not str or not order_id or len(order_id) > 128 or type(reason) is not str or not reason.strip() or len(reason) > 160:
        return refused('exit_request_invalid')
    if role_of(tools) not in ORDER_ROLES:
        return refused('role_cannot_exit')
    ledger = path_of(tools, 'ledger_db')
    known = None
    try:
        if ledger is not None:
            with closing(ro(ledger)) as conn:
                known = conn.execute('SELECT state,exit_requested_at FROM orders WHERE order_id=? AND book_id=?', (order_id, book_id(tools))).fetchone()
        with closing(own(tools)) as conn:
            queued = conn.execute("SELECT 1 FROM desk_paper_requests WHERE kind='order' AND epoch_id=? AND request_id=? AND actor_id=?", (tools.epoch, order_id, tools.actor)).fetchone()
    except (OSError, sqlite3.Error):
        return unknown('ledger_unreadable')
    if known is None and not queued:
        return refused('order_not_in_this_book') if ledger is not None else unknown('ledger_config_unknown')
    if known is not None and (known[0] not in ('pending', 'open')):
        return refused('not_open')
    if known is not None and known[1] is not None:
        return refused('exit_already_requested')
    try:
        request_id = _queued(tools, 'exit', order_id=order_id, reason=reason)
    except sqlite3.IntegrityError:
        return refused('exit_already_requested')
    return out(dict(state='queued', request_id=request_id, order_id=order_id, filled=False, note=NOT_FILLED))
