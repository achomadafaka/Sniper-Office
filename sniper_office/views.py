"""Read models for the operator UI. Every read is a bounded tail on a read-only connection under a deadline.

Rules: a list says when it was cut (`limited`); a value that could not be read is None with a reason, never zero; every number carries the
time it describes. Seat text is returned as plain strings and must be rendered as text. Nothing here writes, except the two operator actions
at the bottom, which only call functions the seat runtime already has."""
from contextlib import contextmanager
import json
import math
import sqlite3
import time

from . import seat_events as E, seat_host as H

DEADLINE_S = 1.
MAX_SEATS = 64
EVENTS = 100
MAIL = 100
DAY = 86400.


def _finite(v):
    return type(v) in (int, float) and math.isfinite(v)


@contextmanager
def _ro(path):
    """Read-only, short busy wait, and a deadline: a slow read must never hold the store against the seats."""
    conn = sqlite3.connect('file:%s?mode=ro' % path, uri=True, timeout=.5)
    try:
        stop = time.monotonic() + DEADLINE_S
        conn.set_progress_handler(lambda: int(time.monotonic() > stop), 2000)
        yield conn
    finally:
        conn.close()


def _refs(raw):
    try:
        value = json.loads(raw) if raw else {}
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _roster(conn):
    rows = conn.execute('SELECT actor_id,epoch_id,role,generation,incarnation,sealed,updated_at FROM office_v2_roster ORDER BY role,actor_id LIMIT ?', (MAX_SEATS + 1,)).fetchall()
    keys = ('actor_id', 'epoch_id', 'role', 'generation', 'incarnation', 'sealed', 'updated_at')
    return [dict(zip(keys, r)) for r in rows[:MAX_SEATS] if not r[5]], len(rows) > MAX_SEATS


def _status(conn, seat, now):
    """From the seat's own lifecycle events, newest first: what the host last said, not a process probe."""
    rows = conn.execute("SELECT text,refs_json,corrected_at FROM office_v2_events WHERE actor_id=? AND kind='lifecycle' AND epoch_id=? AND generation=? ORDER BY seq DESC LIMIT 40",
                        (seat['actor_id'], seat['epoch_id'], seat['generation'])).fetchall()
    out = dict(status='unknown', reason='no_lifecycle_event', since=None, sleep_until=None, last_wake=None)
    decided = False
    for text, raw, at in rows:
        refs = _refs(raw)
        if text == 'wake_end' and out['last_wake'] is None:
            out['last_wake'] = dict(ended_at=at, outcome=refs.get('outcome') if isinstance(refs.get('outcome'), str) else None)
        if text == 'sleep' and out['sleep_until'] is None and _finite(at) and _finite(refs.get('until_s')):
            out['sleep_until'] = at + refs['until_s']
        if decided:
            continue
        if text == 'parked':
            out.update(status='parked', reason=refs.get('reason') if isinstance(refs.get('reason'), str) else None, since=at);decided = True
        elif text == 'wake_start':
            out.update(status='awake', reason=refs.get('type') if isinstance(refs.get('type'), str) else None, since=at);decided = True
        elif text in ('wake_end', 'unparked'):
            out.update(status='asleep', reason=None, since=at);decided = True
    return out


def _usage(quota, actor_id, now):
    """Wakes and tokens since UTC midnight. Tokens are None when any of those wakes has unknown usage: a partial sum would read as a total."""
    if quota is None:
        return dict(wakes_today=None, tokens_today=None, usage_reason='quota_unavailable')
    start = now - now % DAY
    rows = quota.execute('SELECT used,state FROM quota_wakes WHERE actor_id=? AND ended_at>=? ORDER BY ended_at DESC LIMIT 500', (actor_id, start)).fetchall()
    known = all(state == 'known' and type(used) is int for used, state in rows)
    return dict(wakes_today=len(rows), tokens_today=sum(used for used, _ in rows) if known else None, usage_reason=None if known else 'some_wakes_unmeasured')


@contextmanager
def _quota(path):
    if not path:
        yield None;return
    try:
        with _ro(path) as conn:
            conn.execute('SELECT 1 FROM quota_wakes LIMIT 1')
            yield conn
    except sqlite3.Error:
        yield None


def overview(office_db, quota_db, *, now):
    out = dict(state='unknown', as_of=now if _finite(now) else None, seats=[], alerts=[], limited=False, reason=None)
    if not _finite(now):
        return dict(out, reason='clock_unknown')
    try:
        with _ro(office_db) as conn, _quota(quota_db) as quota:
            seats, out['limited'] = _roster(conn)
            for seat in seats:
                row = dict(seat, **_status(conn, seat, now), **_usage(quota, seat['actor_id'], now))
                row['unread_mail'] = conn.execute('SELECT count(*) FROM office_dm_pending WHERE to_actor_id=? AND to_generation=?', (seat['actor_id'], seat['generation'])).fetchone()[0]
                row['requests'] = conn.execute("SELECT count(*) FROM (SELECT 1 FROM office_v2_events WHERE actor_id=? AND kind='request' AND epoch_id=? ORDER BY seq DESC LIMIT 200)",
                                               (seat['actor_id'], seat['epoch_id'])).fetchone()[0]
                out['seats'].append(row)
                if row['status'] == 'parked':
                    out['alerts'].append(dict(kind='parked', actor_id=seat['actor_id'], role=seat['role'], text=row['reason'] or 'reason unknown', at=row['since']))
                if row['status'] == 'unknown':
                    out['alerts'].append(dict(kind='no_signal', actor_id=seat['actor_id'], role=seat['role'], text='the host has recorded nothing for this seat', at=None))
            waiting = conn.execute("SELECT count(*) FROM (SELECT 1 FROM office_dm_events WHERE to_actor_id='operator' ORDER BY seq DESC LIMIT 200)").fetchone()[0]
            out.update(state='partial' if out['limited'] else 'known', operator_inbox=waiting, quota='known' if quota is not None else 'unavailable')
    except sqlite3.Error as exc:
        return dict(out, seats=[], alerts=[], reason='store_unavailable:' + type(exc).__name__)
    return out


def seat(office_db, quota_db, actor_id, *, now, before=None, limit=EVENTS):
    """One seat: its roster row, its newest events (page back with `before`), its mail and its last wakes."""
    out = dict(state='unknown', as_of=now if _finite(now) else None, seat=None, events=[], mail=[], wakes=[], limited=False, cursor=None, reason=None)
    if not isinstance(actor_id, str) or not 1 <= len(actor_id) <= 256 or type(limit) is not int or not 1 <= limit <= 200 or (before is not None and (type(before) is not int or before < 1)):
        return dict(out, reason='bad_arguments')
    if not _finite(now):
        return dict(out, reason='clock_unknown')
    try:
        with _ro(office_db) as conn, _quota(quota_db) as quota:
            found = [s for s in _roster(conn)[0] if s['actor_id'] == actor_id]
            if not found:
                return dict(out, reason='unknown_seat')
            me = found[0]
            rows = conn.execute('SELECT seq,epoch_id,generation,incarnation,corrected_at,kind,state,text,truncated,refs_json,web_tainted FROM office_v2_events WHERE actor_id=?'
                                + (' AND seq<?' if before else '') + ' ORDER BY seq DESC LIMIT ?', (actor_id, *([before] if before else []), limit + 1)).fetchall()
            out['limited'] = len(rows) > limit
            for seq, epoch, gen, inc, at, kind, state, text, cut, raw, tainted in rows[:limit]:
                out['events'].append(dict(seq=seq, at=at, kind=kind, state=state, text=text, truncated=bool(cut), refs=_refs(raw), web_tainted=bool(tainted),
                                          earlier_identity=(epoch, gen, inc) != (me['epoch_id'], me['generation'], me['incarnation'])))
            out['cursor'] = out['events'][-1]['seq'] if out['events'] and out['limited'] else None
            mail = conn.execute('SELECT e.seq,e.message_id,e.from_actor_id,e.to_actor_id,e.created_at,e.text,p.message_id IS NOT NULL FROM office_dm_events e '
                                'LEFT JOIN office_dm_pending p ON p.message_id=e.message_id WHERE e.to_actor_id=? OR e.from_actor_id=? ORDER BY e.seq DESC LIMIT 40', (actor_id, actor_id)).fetchall()
            out['mail'] = [dict(seq=r[0], message_id=r[1], sender=r[2], to=r[3], at=r[4], text=r[5], unread=bool(r[6])) for r in mail]
            if quota is not None:
                for wake_id, ended, used, state, payload in quota.execute('SELECT wake_id,ended_at,used,state,payload_json FROM quota_wakes WHERE actor_id=? ORDER BY ended_at DESC LIMIT 20', (actor_id,)):
                    doc = _refs(payload)
                    out['wakes'].append(dict(wake_id=wake_id, ended_at=ended, used=used if state == 'known' else None, state=state, outcome=doc.get('outcome'), started_at=doc.get('started_at'), model=doc.get('model')))
            out.update(state='partial' if out['limited'] else 'known', seat=dict(me, **_status(conn, me, now), **_usage(quota, actor_id, now)))
    except sqlite3.Error as exc:
        return dict(out, events=[], mail=[], wakes=[], reason='store_unavailable:' + type(exc).__name__)
    return out


def mail(office_db, *, now, limit=MAIL):
    """Newest messages across all seats and the operator, with the sender's role as the host knows it."""
    out = dict(state='unknown', as_of=now if _finite(now) else None, items=[], seats=[], limited=False, reason=None)
    if type(limit) is not int or not 1 <= limit <= 200:
        return dict(out, reason='bad_arguments')
    try:
        with _ro(office_db) as conn:
            seats, _ = _roster(conn)
            roles = {s['actor_id']: s['role'] for s in seats}
            rows = conn.execute('SELECT e.seq,e.message_id,e.from_actor_id,e.to_actor_id,e.created_at,e.text,p.message_id IS NOT NULL FROM office_dm_events e '
                                'LEFT JOIN office_dm_pending p ON p.message_id=e.message_id ORDER BY e.seq DESC LIMIT ?', (limit + 1,)).fetchall()
            out['limited'] = len(rows) > limit
            for seq, mid, sender, to, at, text, unread in rows[:limit]:
                out['items'].append(dict(seq=seq, message_id=mid, sender=sender, sender_role='operator' if sender == 'operator' else roles.get(sender), to=to,
                                         to_role='operator' if to == 'operator' else roles.get(to), at=at, text=text, unread=bool(unread)))
            out.update(state='partial' if out['limited'] else 'known', seats=[dict(actor_id=s['actor_id'], role=s['role']) for s in seats])
    except sqlite3.Error as exc:
        return dict(out, items=[], reason='store_unavailable:' + type(exc).__name__)
    return out


def _has(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _part(reason=None):
    """One section of a view. A table this deployment does not have is 'unavailable', which is different from an empty list."""
    return dict(state='unavailable' if reason else 'known', items=[], limited=False, reason=reason)


def requests(office_db, *, now, limit=100):
    """What seats asked for, and tickets to humans with where each one stands."""
    out = dict(state='unknown', as_of=now if _finite(now) else None, asks=_part('not_read'), tickets=_part('not_read'), reason=None)
    try:
        with _ro(office_db) as conn:
            roles = {s['actor_id']: s['role'] for s in _roster(conn)[0]}
            asks = _part()
            rows = conn.execute("SELECT seq,actor_id,corrected_at,text,refs_json,epoch_id FROM office_v2_events WHERE kind='request' ORDER BY seq DESC LIMIT ?", (limit + 1,)).fetchall()
            asks['limited'] = len(rows) > limit
            for seq, actor, at, text, raw, epoch in rows[:limit]:
                refs = _refs(raw)
                asks['items'].append(dict(seq=seq, actor_id=actor, role=roles.get(actor), at=at, text=text, epoch_id=epoch,
                                          kind=refs.get('kind') if isinstance(refs.get('kind'), str) else None, why=refs.get('why') if isinstance(refs.get('why'), str) else None))
            tickets = _part() if _has(conn, 'tickets') else _part('no_ticket_table')
            if tickets['state'] == 'known':
                rows = conn.execute('SELECT id,substr(payload_json,1,65537) FROM tickets ORDER BY rowid DESC LIMIT ?', (limit + 1,)).fetchall()
                tickets['limited'] = len(rows) > limit
                for tid, raw in rows[:limit]:
                    doc = _refs(raw) if len(raw) <= 65536 else {}
                    history = doc.get('history') if isinstance(doc.get('history'), list) else []
                    last = history[-1] if history and isinstance(history[-1], dict) else {}
                    tickets['items'].append(dict(ticket_id=tid, state=doc.get('state'), author=doc.get('author'), problem=doc.get('problem'), done_when=doc.get('done_when'),
                                                 known_at=doc.get('known_at'), expected_impact=doc.get('expected_impact'), evidence_refs=(doc.get('evidence_refs') or [])[:8],
                                                 steps=len(history), last_actor=last.get('actor_id'), last_at=last.get('known_at')))
            out.update(state='partial' if asks['limited'] or tickets['limited'] else 'known', asks=asks, tickets=tickets)
    except sqlite3.Error as exc:
        return dict(out, reason='store_unavailable:' + type(exc).__name__)
    return out


def budget(office_db, quota_db, *, now):
    """Per seat: wakes this hour and today, tokens today, the largest wake. Per account: each usage window as last measured, and how old that reading is."""
    out = dict(state='unknown', as_of=now if _finite(now) else None, seats=[], accounts=_part('not_read'), clears=_part('not_read'), reason=None)
    if not _finite(now):
        return dict(out, reason='clock_unknown')
    try:
        with _ro(office_db) as conn, _quota(quota_db) as quota:
            seats, _ = _roster(conn)
            if quota is None:
                return dict(out, seats=[dict(s, **_usage(None, s['actor_id'], now)) for s in seats], accounts=_part('quota_unavailable'), clears=_part('quota_unavailable'), state='partial')
            for s in seats:
                hour = quota.execute('SELECT count(*) FROM quota_wakes WHERE actor_id=? AND ended_at>?', (s['actor_id'], now - 3600)).fetchone()[0]
                top = quota.execute("SELECT max(used) FROM (SELECT used FROM quota_wakes WHERE actor_id=? AND ended_at>=? AND state='known' ORDER BY ended_at DESC LIMIT 500)", (s['actor_id'], now - now % DAY)).fetchone()[0]
                unknown = quota.execute("SELECT count(*) FROM (SELECT state FROM quota_wakes WHERE actor_id=? AND ended_at>=? ORDER BY ended_at DESC LIMIT 500) WHERE state!='known'", (s['actor_id'], now - now % DAY)).fetchone()[0]
                out['seats'].append(dict(actor_id=s['actor_id'], role=s['role'], wakes_last_hour=hour, largest_wake_today=top, unmeasured_wakes_today=unknown, **_usage(quota, s['actor_id'], now)))
            accounts = _part()
            for alias, at, raw in quota.execute('SELECT account_alias,max(observed_at),payload_json FROM quota_account_snapshots GROUP BY account_alias ORDER BY account_alias LIMIT 16'):
                doc = _refs(raw);windows = doc.get('windows') if isinstance(doc.get('windows'), dict) else {}
                accounts['items'].append(dict(account=alias, observed_at=at, age_s=now - at if _finite(at) else None, provider=doc.get('provider'), reading=doc.get('state'), reading_reason=doc.get('reason'),
                                              windows=[dict(name=name, used_percent=w.get('used_percent'), resets_at=w.get('resets_at')) for name, w in sorted(windows.items())[:8] if isinstance(w, dict)]))
            clears = _part() if _has(quota, 'seat_park_clears') else _part('none_recorded')
            if clears['state'] == 'known':
                roles = {s['actor_id']: s['role'] for s in seats}
                clears['items'] = [dict(actor_id=a, role=roles.get(a), at=at, by=by, reason=why) for a, at, by, why in quota.execute('SELECT actor_id,cleared_at,cleared_by,reason FROM seat_park_clears ORDER BY cleared_at DESC LIMIT 20')]
            out.update(state='known', accounts=accounts, clears=clears)
    except sqlite3.Error as exc:
        return dict(out, seats=[], reason='store_unavailable:' + type(exc).__name__)
    return out


def evaluation(office_db, *, now):
    """Epochs, the trial registry in order, and each released verdict. Released aggregates only: this never opens the evaluation file."""
    out = dict(state='unknown', as_of=now if _finite(now) else None, epochs=_part('not_read'), trials=_part('not_read'), verdicts=_part('not_read'), reason=None)
    try:
        with _ro(office_db) as conn:
            epochs = _part() if _has(conn, 'epochs') else _part('no_epoch_table')
            if epochs['state'] == 'known':
                for (raw,) in conn.execute("SELECT payload_json FROM epochs ORDER BY json_extract(payload_json,'$.opened_at') DESC LIMIT 20"):
                    e = _refs(raw)
                    epochs['items'].append({k: e.get(k) for k in ('epoch_id', 'kind', 'opened_at', 'closed_at', 'status', 'verdict', 'k', 'alpha_k', 'hypothesis_id', 'reason', 'config_custody')})
            trials = _part() if _has(conn, 'trial_registry') else _part('no_trial_registry')
            if trials['state'] == 'known':
                rows = conn.execute('SELECT seq,payload_json FROM trial_registry ORDER BY seq DESC LIMIT 101').fetchall()
                trials['limited'] = len(rows) > 100
                trials['items'] = [dict(seq=seq, **{k: _refs(raw).get(k) for k in ('k', 'alpha_k', 'hypothesis_id', 'status', 'epoch_id', 'reason', 'look_id')}) for seq, raw in rows[:100]]
            verdicts = _part() if _has(conn, 'sealed_views') else _part('no_released_views')
            if verdicts['state'] == 'known':
                keep = ('state', 'reasons', 'epoch_id', 'hypothesis_id', 'k', 'alpha_k', 'released_at', 'estimand', 'unit', 'delta0', 'cases', 'graded', 'censored', 'clusters', 'net_mean', 'net_lower',
                        'lift_mean', 'lift_lower', 'positive_clusters', 'top3_share', 'team_total', 'baseline_total', 'fixed_cost', 'custody', 'gaps')
                for digest, raw in conn.execute("SELECT row_hash,payload_json FROM sealed_views WHERE json_extract(payload_json,'$.kind')='evaluation_verdict' ORDER BY seq DESC LIMIT 20"):
                    doc = _refs(raw)
                    verdicts['items'].append(dict(release_hash=digest, **{k: doc.get(k) for k in keep}))
            out.update(state='known', epochs=epochs, trials=trials, verdicts=verdicts)
    except sqlite3.Error as exc:
        return dict(out, reason='store_unavailable:' + type(exc).__name__)
    return out


CHAIN_ROWS = 200_000


def health(office_db, *, now, verify=False):
    """Supervisor lease and heartbeat, turn states, and on request a hash-chain check of the append-only tables. The check re-hashes every row, so it
    is asked for, never polled, and a table over CHAIN_ROWS rows is reported as too large instead of holding the store."""
    out = dict(state='unknown', as_of=now if _finite(now) else None, lease=None, heartbeat=None, turns=_part('not_read'), chains=_part('not_checked'), reason=None)
    if not _finite(now):
        return dict(out, reason='clock_unknown')
    try:
        with _ro(office_db) as conn:
            if _has(conn, 'supervisor_lease'):
                row = conn.execute('SELECT holder,generation,expires_at FROM supervisor_lease WHERE id=1').fetchone()
                out['lease'] = dict(holder=row[0], generation=row[1], expires_at=row[2], held=bool(_finite(row[2]) and row[2] > now)) if row else dict(holder=None, held=False)
            if _has(conn, 'heartbeats'):
                row = conn.execute("SELECT payload_json FROM heartbeats WHERE id='supervisor'").fetchone()
                beat = _refs(row[0]) if row else {}
                frontiers = beat.get('frontiers') if isinstance(beat.get('frontiers'), dict) else {}
                out['heartbeat'] = dict(at=beat.get('at'), age_s=now - beat['at'] if _finite(beat.get('at')) else None, generation=beat.get('generation'), progress=frontiers.get('state'),
                                        oldest_pending_s=frontiers.get('oldest_pending_s')) if row else dict(at=None, age_s=None)
            turns = _part() if _has(conn, 'turn_runs') else _part('no_turn_table')
            if turns['state'] == 'known':
                counts = {}
                rows = conn.execute("SELECT json_extract(payload_json,'$.state') FROM turn_runs ORDER BY json_extract(payload_json,'$.created_at') DESC LIMIT 501").fetchall()
                turns['limited'] = len(rows) > 500
                for (state,) in rows[:500]:
                    counts[state or 'unknown'] = counts.get(state or 'unknown', 0) + 1
                turns['items'] = [dict(state=k, count=v) for k, v in sorted(counts.items())]
            if verify:
                from . import store as S
                chains = _part()
                for table in sorted(S.APPEND_ONLY_TABLES - S.PRIVATE_TABLES):
                    if not _has(conn, table):
                        continue
                    rows = conn.execute('SELECT count(*) FROM (SELECT 1 FROM ' + table + ' LIMIT ?)', (CHAIN_ROWS + 1,)).fetchone()[0]
                    if rows > CHAIN_ROWS:
                        chains['items'].append(dict(table=table, rows=None, result='too_large_to_check_here'));continue
                    try:
                        intact, bad = S.verify_chain(conn, table)
                    except sqlite3.Error:
                        chains['items'].append(dict(table=table, rows=rows, result='deadline'));continue
                    chains['items'].append(dict(table=table, rows=rows, result='intact' if intact else 'broken_at_row_%s' % bad))
                out['chains'] = chains
            out.update(state='known', turns=turns)
    except sqlite3.Error as exc:
        return dict(out, reason='store_unavailable:' + type(exc).__name__)
    return out


# ---- project panels

PANEL_ROWS = 200
PANEL_POINTS = 2000
PANEL_COLUMNS = 16
PANEL_TEXT = 2000


def _cell(v):
    if v is None or type(v) is bool or _finite(v):
        return v
    return str(v)[:PANEL_TEXT]                      # anything else becomes capped text; the page renders text only


def panel(read, *, now):
    """Run one project panel and force its answer into one of three shapes. A panel that raises, returns another shape or is too big is
    'unavailable' with a reason; it never breaks the page and its exception text is not shown."""
    try:
        doc = read(now)
        if not isinstance(doc, dict):
            raise ValueError('shape')
        shape = doc.get('shape')
        base = dict(state='known', shape=shape, as_of=doc.get('as_of') if _finite(doc.get('as_of')) else None, limited=bool(doc.get('limited')),
                    note=str(doc['note'])[:500] if isinstance(doc.get('note'), str) else None)
        if shape == 'table':
            columns = [str(c)[:80] for c in doc['columns']][:PANEL_COLUMNS]
            rows = doc['rows']
            if not columns or not isinstance(rows, list):
                raise ValueError('shape')
            return dict(base, columns=columns, rows=[[_cell(v) for v in list(r)[:len(columns)]] for r in rows[:PANEL_ROWS]], limited=base['limited'] or len(rows) > PANEL_ROWS)
        if shape == 'facts':
            items = doc['items']
            return dict(base, items=[dict(label=str(i['label'])[:120], value=_cell(i.get('value')), unit=str(i['unit'])[:24] if i.get('unit') else None,
                                          as_of=i.get('as_of') if _finite(i.get('as_of')) else None) for i in items[:PANEL_ROWS]], limited=base['limited'] or len(items) > PANEL_ROWS)
        if shape == 'series':
            points = [(p[0], p[1]) for p in doc['points'] if _finite(p[0]) and _finite(p[1])]
            return dict(base, unit=str(doc['unit'])[:24] if doc.get('unit') else None, points=points[-PANEL_POINTS:], limited=base['limited'] or len(points) > PANEL_POINTS,
                        dropped=len(doc['points']) - len(points))
        raise ValueError('shape')
    except Exception as exc:
        return dict(state='unavailable', reason='panel_failed' if not isinstance(exc, (ValueError, KeyError, TypeError, IndexError)) else 'panel_shape_invalid')


# ---- the two operator actions

def send_operator_mail(office_db, *, to_actor_id, client_id, text, now):
    """-> {state: sent|duplicate|refused, ...}. The sender is set to the operator by the seat runtime, never by the caller."""
    conn = sqlite3.connect(office_db, timeout=2, isolation_level=None)
    try:
        return E.operator_dm(conn, to_actor_id=to_actor_id, client_id=client_id, text=text, now=now)
    except E.OperatorDmRefused as exc:
        return dict(state='refused', reason=exc.code)
    finally:
        conn.close()


def clear_park(office_db, quota_db, *, actor_id, reason, now):
    """Logs who cleared it and why. The seat host re-checks on its next tick: a cause that is still true parks the seat again."""
    try:
        with _ro(office_db) as conn:
            if not any(s['actor_id'] == actor_id for s in _roster(conn)[0]):
                return dict(state='refused', reason='unknown_seat')
        conn = sqlite3.connect(quota_db, timeout=2)
        try:
            H.ensure_schema(conn)
            H.clear_park(conn, actor_id=actor_id, cleared_by='operator', reason=reason, now=now)
        finally:
            conn.close()
    except ValueError as exc:
        return dict(state='refused', reason=str(exc))
    except sqlite3.Error as exc:
        return dict(state='refused', reason='store_unavailable:' + type(exc).__name__)
    return dict(state='cleared', actor_id=actor_id)
