"""Read models for the operator UI. Every read is a bounded tail on a read-only connection under a deadline.

Rules: a list says when it was cut (`limited`); a value that could not be read is None with a reason, never zero; every number carries the
time it describes. Seat text is returned as plain strings and must be rendered as text. Nothing here writes, except the two operator actions
at the bottom, which only call functions the seat runtime already has."""
from contextlib import contextmanager
import json
import math
import os
import re
from pathlib import Path
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


# ---- the seat floor: what /api/office/newsroom is built from

FLOOR_PARTS = ('roster', 'events', 'mail', 'tasks', 'seats', 'questions')


def _text(v, maximum=4096, *, empty=False):
    return isinstance(v, str) and (empty or bool(v)) and len(v.encode('utf-8')) <= maximum


def _cursor(v):
    if v is None:
        return None
    if not isinstance(v, str) or not v.isascii() or not v.isdecimal() or len(v) > 19 or int(v) > 9223372036854775807:
        raise ValueError('cursor_invalid')
    return int(v)


def _floor_part(reason=None):
    return dict(state='unknown' if reason else 'known', items=[], limited=False, omitted=0, cursor=None, reason=reason)


def _host_state(office_db, seat):
    """The seat host's own state file next to the store (seat-<role>.json): sleep_until and parked_reason. Read only if small, regular and the same incarnation."""
    path = Path(office_db).resolve().parent / ('seat-%s.json' % seat['role'].lower())
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if os.fstat(fd).st_size > 16384:
                return {}
            doc = json.loads(os.read(fd, 16385))
        finally:
            os.close(fd)
        return doc if isinstance(doc, dict) and doc.get('incarnation') == seat['incarnation'] else {}
    except (OSError, ValueError, TypeError):
        return {}


def _floor_metrics(quota, seat, now):
    if quota is None:
        return dict(wakes_today=None, tokens_today=None, tokens_known=None, tokens_unknown_wakes=None)
    row = quota.execute("SELECT count(*),sum(CASE WHEN state='known' AND typeof(used)='integer' AND used>=0 THEN used ELSE 0 END),"
                        "sum(CASE WHEN state!='known' OR used IS NULL OR typeof(used)!='integer' OR used<0 THEN 1 ELSE 0 END) FROM quota_wakes WHERE actor_id=? AND ended_at>=? AND ended_at<=?",
                        (seat['actor_id'], now // DAY * DAY, now)).fetchone()
    return dict(wakes_today=row[0], tokens_today=(row[1] or 0) if not row[2] else None, tokens_known=row[1] or 0, tokens_unknown_wakes=row[2] or 0)


def _floor_seats(conn, quota, office_db, roster, now):
    out = []
    for seat in roster:
        rows = conn.execute('SELECT event_id,kind,text,state,corrected_at,refs_json,truncated FROM office_v2_events WHERE actor_id=? AND epoch_id=? AND generation=? AND incarnation=? ORDER BY seq DESC LIMIT 64',
                            tuple(seat[k] for k in ('actor_id', 'epoch_id', 'generation', 'incarnation'))).fetchall()
        life = next((r for r in rows if r['kind'] == 'lifecycle'), None)
        last = rows[0] if rows else None
        last_at = last['corrected_at'] if last and _finite(last['corrected_at']) and 0 <= last['corrected_at'] <= now else None
        rows = [r for r in rows if _finite(r['corrected_at']) and 0 <= r['corrected_at'] <= now]
        thoughts = [r for r in rows if r['kind'] == 'thought' and _text(r['text'])]
        host = _host_state(office_db, seat)
        activity = dict(kind='unknown', until=None, reason=None, at=None)
        if life and life['state'] == 'known' and _finite(life['corrected_at']) and 0 <= life['corrected_at'] <= now:
            at = life['corrected_at'];activity['at'] = at
            refs = _refs(life['refs_json'])
            reason = refs.get('reason') if isinstance(refs.get('reason'), str) else host.get('parked_reason')
            if life['text'] == 'parked' and isinstance(reason, str):
                activity.update(kind='parked', reason=reason[:256])
            elif life['text'] == 'wake_start' and now - at <= 660:
                activity['kind'] = 'working'
            elif life['text'] in ('wake_end', 'sleep', 'unparked'):
                until = refs.get('sleep_until') if life['text'] == 'unparked' and _finite(refs.get('sleep_until')) else host.get('sleep_until')
                if _finite(until) and now < until <= 253402300799:
                    activity.update(kind='sleeping', until=until)
                elif life['text'] == 'unparked':
                    activity['kind'] = 'idle'
        tool = next((r['text'] for r in rows if r['kind'] == 'tool_call' and isinstance(r['text'], str) and _finite(activity['at']) and r['corrected_at'] >= activity['at']), None)
        tool_event = next((r for r in rows if r['kind'] in ('tool_call', 'tool_result')), None)
        last_tool = (_refs(tool_event['refs_json']).get('tool') if tool_event else None) or (tool_event['text'] if tool_event and tool_event['kind'] == 'tool_call' else None)
        out.append(dict(actor_id=seat['actor_id'], role=seat['role'], name=seat['role'].replace('_', ' ').title(), activity=activity,
                        current_tool=tool[:128] if tool and activity['kind'] == 'working' else None, last_thought=thoughts[0]['text'] if thoughts else None,
                        last_event_id=last['event_id'] if last else None, last_event_at=last_at, last_event_kind=last['kind'] if last else None,
                        last_event_age_s=now - last_at if last_at is not None else None, last_tool=last_tool[:128] if isinstance(last_tool, str) else None,
                        tool_event_at=tool_event['corrected_at'] if tool_event else None, tool_event_state=('called' if tool_event['kind'] == 'tool_call' else 'returned') if tool_event else None,
                        messages=[dict(event_id=r['event_id'], actor_id=seat['actor_id'], kind='thought', text=r['text'], corrected_at=r['corrected_at'], truncated=r['truncated']) for r in thoughts[:16]],
                        **_floor_metrics(quota, seat, now)))
    return dict(state='known', items=out, coverage='Current identity; last 64 events per seat. State is last host observation, not a process probe. Wakes are completed today UTC; counted tokens exclude cache reads.')


def _floor_questions(conn, roster, now):
    items = [];limited = False;omitted = 0
    for seat in roster:
        values = conn.execute("SELECT event_id,actor_id,state,text,corrected_at FROM office_v2_events WHERE actor_id=? AND epoch_id=? AND generation=? AND incarnation=? AND kind='request' ORDER BY seq DESC LIMIT 11",
                              tuple(seat[k] for k in ('actor_id', 'epoch_id', 'generation', 'incarnation'))).fetchall()
        limited = limited or len(values) > 10
        for row in values[:10]:
            if not _finite(row['corrected_at']) or not 0 <= row['corrected_at'] <= now or not _text(row['text']) or not row['text'].strip():
                omitted += 1;continue
            items.append(dict(row))
    return dict(state='partial' if limited or omitted else 'known', items=sorted(items, key=lambda r: r['corrected_at'], reverse=True), limited=limited, omitted=omitted,
                coverage='Latest 10 requests per current seat; replies do not automatically resolve a request.')


def _floor_models(quota, result, now):
    """Requested model of the one recorded wake that covers an item, never the current configuration. Unknown when none or several match."""
    windows = []
    current = {r['actor_id']: r for r in result['roster']['items']}
    if quota is not None:
        for actor, seat in current.items():
            for (raw,) in quota.execute('SELECT payload_json FROM quota_wakes WHERE actor_id=? AND ended_at<=? ORDER BY ended_at DESC LIMIT 64', (actor, now)):
                d = _refs(raw if _text(raw, 16384) else None)
                if (d.get('actor_id') == actor and d.get('epoch_id') == seat['epoch_id'] and d.get('generation') == seat['generation'] and str(d.get('incarnation')) == str(seat['incarnation'])
                        and _text(d.get('model'), 128) and _finite(d.get('started_at')) and _finite(d.get('ended_at')) and 0 <= d['started_at'] <= d['ended_at'] <= now):
                    windows.append(d)
    for part in ('events', 'mail', 'questions', 'seat_messages'):
        values = [m for s in result['seats']['items'] for m in s.get('messages', [])] if part == 'seat_messages' else result[part]['items']
        for item in values:
            actor, at = (item.get('from_actor_id'), item.get('created_at')) if part == 'mail' else (item.get('actor_id'), item.get('corrected_at'))
            matches = {d['model'] for d in windows if d['actor_id'] == actor and _finite(at) and d['started_at'] <= at <= d['ended_at']}
            item['model'] = next(iter(matches)) if len(matches) == 1 else None
            item['model_basis'] = 'recorded_wake_requested' if item['model'] else 'unknown'


def _floor_event(r, seat, now):
    """One event row as the floor may show it, or None when it fails the identity or shape checks. Refs are cut down per kind."""
    if (seat is None or any(r[k] != seat[k] for k in ('epoch_id', 'generation', 'incarnation')) or r['kind'] not in E.KINDS or r['state'] not in E.STATES
            or r['corrected_at'] is not None and (not _finite(r['corrected_at']) or not 0 <= r['corrected_at'] <= now)
            or not _text(r['event_id'], 256) or r['text'] is not None and not _text(r['text'], empty=True)):
        return None
    raw = r['refs_json']
    if raw is not None and not _text(raw, empty=True):
        return None
    try:
        refs = json.loads(raw or '{}')
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(refs, dict):
        return None
    kind = r['kind']
    if kind == 'browser_visit':
        refs = {k: refs.get(k) for k in ('host', 'path')}
    elif kind == 'workspace':
        files = refs.get('files', [])
        if not isinstance(files, list):
            return None
        refs = dict({k: refs.get(k) for k in ('notebook_text', 'state', 'truncated', 'tasks')}, files=[dict(path=f.get('path'), size_bytes=f.get('size_bytes')) for f in files[:64] if isinstance(f, dict)])
    elif kind == 'lifecycle':
        refs = {k: refs.get(k) for k in ('reason', 'until_s', 'type', 'outcome') if isinstance(refs.get(k), (str, int, float))}
    elif kind == 'tool_result':
        refs = dict(tool=refs.get('tool') if _text(refs.get('tool'), 256) else None, is_error=refs.get('is_error') if type(refs.get('is_error')) is bool else None)
    else:
        refs = {}
    r = {k: r[k] for k in r.keys() if k != 'refs_json'}
    r.update(refs=refs, seq=str(r['seq']))
    if r['corrected_at'] is None or r['kind'] == 'thought' and r['text'] is None:
        r['state'] = 'unknown'
    return r


def floor_feed(office_db, quota_db, *, now, after=None, mail_after=None, actor=None, limit=64):
    """Roster, newest events, mail, tasks, per-seat activity, open requests: the validated base of `newsroom`.
    Raises ValueError for a bad cursor, limit or actor. Sealed seats are withheld, not unknown."""
    after, mail_after = _cursor(after), _cursor(mail_after)
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('limit_invalid')
    if actor is not None and not _text(actor, 256):
        raise ValueError('actor_invalid')
    result = dict(state='unknown', as_of=now if _finite(now) else None, **{k: _floor_part('source_unavailable') for k in FLOOR_PARTS})
    if not _finite(now):
        return result
    try:
        with _ro(office_db) as conn, _quota(quota_db) as quota:
            conn.row_factory = sqlite3.Row
            roster = _floor_part()
            raw = conn.execute('SELECT * FROM office_v2_roster ORDER BY actor_id LIMIT 65').fetchall()
            roster['limited'] = len(raw) > 64
            for row in raw[:64]:
                r = dict(row)
                if r['sealed'] == 1:
                    continue
                if (r['sealed'] != 0 or not _finite(r['updated_at']) or not 0 <= r['updated_at'] <= now or not all(_text(r[k], 256) for k in ('actor_id', 'epoch_id', 'role'))
                        or any(type(r[k]) is not int or r[k] < 0 for k in ('generation', 'incarnation'))):
                    roster['omitted'] += 1;continue
                roster['items'].append(r)
            if roster['limited'] or roster['omitted']:
                roster['state'] = 'partial'
            result['roster'] = roster
            current = {r['actor_id']: r for r in roster['items']}

            def tail(table, cursor, *, by_actor=False):
                part = _floor_part();where = [];args = []
                if cursor is not None:
                    where.append('seq>?');args.append(cursor)
                if by_actor and actor is not None:
                    where.append('actor_id=?');args.append(actor)
                rows = conn.execute('SELECT * FROM ' + table + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY seq ' + ('ASC' if cursor is not None else 'DESC') + ' LIMIT ?', (*args, limit + 1)).fetchall()
                part['limited'] = len(rows) > limit
                rows = rows[:limit]
                part['cursor'] = str(max(r['seq'] for r in rows)) if rows else str(cursor) if cursor is not None else None
                return rows, part

            rows, part = tail('office_v2_events', after, by_actor=True)
            heads = {}
            for row in rows:
                if row['kind'] == 'workspace' and row['actor_id'] in current and all(row[k] == current[row['actor_id']][k] for k in ('epoch_id', 'generation', 'incarnation')):
                    heads[row['actor_id']] = max(heads.get(row['actor_id'], 0), row['seq'])
                event = _floor_event(row, current.get(row['actor_id']), now)
                if event is None:
                    part['omitted'] += 1
                else:
                    part['items'].append(event)
            kept = [e for e in part['items'] if e['kind'] != 'workspace' or int(e['seq']) == heads.get(e['actor_id'])]
            part['omitted'] += len(part['items']) - len(kept);part['items'] = kept
            if part['limited'] or part['omitted']:
                part['state'] = 'partial'
            result['events'] = part

            rows, part = tail('office_dm_events', mail_after)
            for row in rows:
                r = dict(row);sender = current.get(r['from_actor_id']);target = sender if r['to_actor_id'] == 'operator' else current.get(r['to_actor_id'])   # a reply to the operator binds to the replying seat
                host_sender = E.is_host_sender(r['from_actor_id'])
                if (target is None or r['to_generation'] != target['generation'] or not host_sender and (sender is None or r['from_generation'] != sender['generation'])
                        or not _finite(r['created_at']) or not 0 <= r['created_at'] <= now or not _text(r['text']) or not _text(r['message_id'], 256)):
                    part['omitted'] += 1;continue
                bind = conn.execute('SELECT epoch_id,from_incarnation,to_incarnation FROM office_v2_mail_bind WHERE message_id=?', (r['message_id'],)).fetchone()
                if not (bind and bind['epoch_id'] == target['epoch_id'] and bind['to_incarnation'] == target['incarnation']
                        and (host_sender or sender['epoch_id'] == target['epoch_id'] and bind['from_incarnation'] == sender['incarnation'])):
                    part['omitted'] += 1;continue
                if actor is not None and actor not in (r['from_actor_id'], r['to_actor_id']):
                    continue
                try:
                    listed = json.loads(r.pop('refs_json', None) or 'null')
                except (ValueError, TypeError, RecursionError):
                    listed = None
                r['notify_operator'] = 'notify_operator' in listed if isinstance(listed, list) and len(listed) <= 64 else None
                r['seq'] = str(r['seq']);part['items'].append(r)
            if part['limited'] or part['omitted']:
                part['state'] = 'partial'
            part['coverage'] = 'last bounded mail events, all seats; epoch/generation/incarnation-bound'
            result['mail'] = part

            tasks = _floor_part();seen = set()
            for event in sorted(result['events']['items'], key=lambda e: int(e['seq']), reverse=True):
                if event['kind'] != 'workspace' or event['actor_id'] in seen:
                    continue
                seen.add(event['actor_id']);manifest = event['refs'];values = manifest.get('tasks')
                if event['state'] == 'unknown' or not isinstance(values, list) or manifest.get('state') not in ('known', 'partial'):
                    tasks['state'] = 'unknown';continue
                if event['state'] == 'partial' or manifest.get('truncated') or manifest.get('state') == 'partial' or len(values) > 64:
                    tasks['limited'] = True
                for t in values[:64]:
                    if (not isinstance(t, dict) or t.get('status') not in ('open', 'blocked', 'done') or not _text(t.get('task_id'), 256) or not _text(t.get('title'), 256)
                            or t.get('note') is not None and not _text(t['note'], 2048, empty=True)):
                        tasks['omitted'] += 1;continue
                    tasks['items'].append(dict(actor_id=event['actor_id'], **{k: t.get(k) for k in ('task_id', 'status', 'title', 'note')}))
            if ({actor} if actor in current else set(current) if actor is None else set()) - seen:
                tasks['state'] = 'unknown'
            if tasks['limited'] or tasks['omitted']:
                tasks['state'] = 'partial'
            result['tasks'] = tasks
            if roster['state'] == 'known':
                result['questions'] = _floor_questions(conn, roster['items'], now)
                result['seats'] = _floor_seats(conn, quota, office_db, roster['items'], now)
                _floor_models(quota, result, now)
    except sqlite3.Error:
        pass
    states = [result[k]['state'] for k in ('roster', 'events', 'mail', 'tasks')]
    result['state'] = 'known' if all(s == 'known' for s in states) else 'unknown' if all(s == 'unknown' for s in states) else 'partial'
    return result


# ---- the seat floor page

NO_ROOMS = dict(rooms={}, fallback=None)      # the floor project's own seat-to-room table (rooms.json in its folder): {rooms: {room: [role, ...]}, fallback: room for a role not listed}


def _title(text, n=140):
    return ' '.join(text.split())[:n] if isinstance(text, str) else ''


def book(ledgers, *, now):
    """Paper ledgers, read-only: orders open and closed, wins and net of costs for closed orders that have an exit price. Unknown, with a reason, when none is given or one cannot be read."""
    if not ledgers:
        return dict(state='unknown', reason='no_ledger_given', books=[], last_closed=[])
    books, last = [], []
    try:
        for path in ledgers:
            name = Path(path).resolve().parent.name
            with _ro(path) as conn:
                net = 'CASE WHEN state=\'closed\' AND exit_usd IS NOT NULL AND spend_usd IS NOT NULL THEN exit_usd-spend_usd-COALESCE(costs_usd,0) END'
                rows = {r[0]: r[1:] for r in conn.execute('SELECT state,count(*),sum(%s),sum(%s>0) FROM orders GROUP BY state' % (net, net))}
                books.append(dict(name=name, open=sum(rows.get(k, (0,))[0] for k in ('pending', 'open')), closed=rows.get('closed', (0,))[0], net_usd=rows.get('closed', (0, None))[1],
                                  wins=rows.get('closed', (0, 0, 0))[2] or 0))
                last += [dict(book=name, closed_at=r[0], exit_reason=r[1], net_usd=r[2]) for r in conn.execute(
                    'SELECT closed_at,exit_reason,%s FROM orders WHERE state=\'closed\' AND closed_at<=? ORDER BY closed_at DESC LIMIT 2' % net, (now,))]
    except sqlite3.Error:
        return dict(state='unknown', reason='ledger_unreadable', books=[], last_closed=[])
    return dict(state='known', reason=None, books=books, last_closed=sorted(last, key=lambda o: o['closed_at'] or 0, reverse=True)[:2])


def _tool_calls(office_db, now):
    try:
        with _ro(office_db) as conn:
            return conn.execute("SELECT count(*) FROM office_v2_events WHERE kind='tool_call' AND corrected_at BETWEEN ? AND ?", (now - 3600, now)).fetchone()[0]
    except sqlite3.Error:
        return None


SOURCE_ROWS = 400
SOURCE_DBS = ('office', 'quota', 'ledger', 'sibling')


def source_queries(doc, names=()):
    """The floor project's own read-only queries, `sources.json` in its folder: {"queries": {name: {db, sql, params?, limit?}}}. db is office, quota, ledger (every --ledger-db) or sibling (the file named by `file`, next to the office store: a collector's database that may not exist yet). `sql` may be a list of variants: the first one a database accepts is the one run on it (a ledger written by an older schema).
    `names` are the project databases the floor folder registered (see _paths); any other db name is refused.
    A query with `params` is run by name from the page with those query-string values bound, in order; one without runs with every page read. The SQL is the project's, like a plugin."""
    queries = doc.get('queries') if isinstance(doc, dict) else None
    if not isinstance(queries, dict) or not all(
            isinstance(q, dict) and q.get('db') in (*SOURCE_DBS, *names) and isinstance(q.get('sql'), (str, list)) and all(isinstance(t, str) and t.lstrip().upper().startswith(('SELECT', 'WITH')) for t in ([q['sql']] if isinstance(q.get('sql'), str) else q['sql']))
            and all(t in ('text', 'real', 'int') for t in (q.get('params') or {}).values())
            and (q['db'] != 'sibling' or isinstance(q.get('file'), str) and re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}', q['file'])) for q in queries.values()):
        raise ValueError('sources.json must be {"queries": {name: {"db": office|quota|ledger, "sql": "SELECT ...", "params": {name: text|real|int}}}}')
    return queries


def _read_only(action, *_):
    return sqlite3.SQLITE_OK if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, getattr(sqlite3, 'SQLITE_RECURSIVE', 33)) else sqlite3.SQLITE_DENY


def _value(v):
    return None if isinstance(v, (bytes, memoryview)) or isinstance(v, float) and not math.isfinite(v) else v[:PANEL_TEXT] if isinstance(v, str) else v


def _run(paths, spec, args=()):
    """Rows from every database in `paths` that can answer, each tagged `_source` with its file name. A file lacking the tables is skipped; none answering is None."""
    rows, answered = [], False
    for path in paths:
        for sql in [spec['sql']] if isinstance(spec['sql'], str) else spec['sql']:
            try:
                with _ro(path) as conn:
                    conn.set_authorizer(_read_only)
                    cursor = conn.execute(sql, args)
                    names = [c[0] for c in cursor.description]
                    rows += [dict(zip(names, map(_value, r)), _source=Path(path).stem) for r in cursor.fetchmany(min(int(spec.get('limit') or SOURCE_ROWS), SOURCE_ROWS))]
                    answered = True
                break
            except (sqlite3.Error, ValueError):
                continue
    return rows if answered else None


def _paths(spec, office_db, quota_db, ledgers, extra=None):
    """`extra` maps a project database name to its path (the page server's flags or the floor's routes module); a name with no path has no database."""
    if spec['db'] == 'sibling':
        return [str(Path(office_db).resolve().parent / spec['file'])]
    if spec['db'] not in SOURCE_DBS:
        return [(extra or {})[spec['db']]] if (extra or {}).get(spec['db']) else []
    return {'office': [office_db], 'quota': [quota_db] if quota_db else [], 'ledger': list(ledgers)}[spec['db']]


def sources(queries, office_db, quota_db, ledgers, extra=None):
    """Every parameterless project query: {name: {state, rows}}. Unknown, with the reason, when no database answers."""
    out = {}
    for name, spec in queries.items():
        if spec.get('params'):
            continue
        paths = _paths(spec, office_db, quota_db, ledgers, extra)
        rows = _run(paths, spec) if paths else None
        out[name] = dict(state='known', rows=rows) if rows is not None else dict(state='unknown', rows=[], reason='no_database' if not paths else 'unreadable')
    return out


def query(queries, name, values, office_db, quota_db, ledgers, extra=None):
    """One parameterised project query by name. ValueError for an unknown name or a missing or malformed value."""
    spec = queries.get(name)
    if spec is None or not spec.get('params'):
        raise ValueError('query_unknown')
    args = []
    for key, kind in spec['params'].items():
        raw = values.get(key)
        if not isinstance(raw, str) or not 0 < len(raw) <= 256:
            raise ValueError('param_invalid')
        args.append(raw if kind == 'text' else int(raw) if kind == 'int' else float(raw))
        if kind == 'real' and not math.isfinite(args[-1]):
            raise ValueError('param_invalid')
    rows = _run(_paths(spec, office_db, quota_db, ledgers, extra), spec, args)
    return dict(state='known' if rows is not None else 'unknown', name=name, rows=rows or [])


def operator_chat(office_db, quota_db, *, now, layout=NO_ROOMS):
    """What the floor's operator-chat panel shows: the seats' requests and the operator's mail to them. Read only; a reply does not close a request here, so every ask is informational."""
    feed = floor_feed(office_db, quota_db, now=now, limit=200)
    if feed['roster']['state'] == 'unknown':
        return dict(status='unavailable')
    roles = {r['actor_id']: r['role'] for r in feed['roster']['items']}
    asks = [dict(id=str(i + 1), seat=roles.get(q['actor_id'], q['actor_id']), mark='i', text=_title(q['text'], 600), status='open') for i, q in enumerate(reversed(feed['questions']['items']))]
    messages = [dict(seats=[roles.get(m['to_actor_id'], m['to_actor_id'])], text=_title(m['text'], 600), at=m['created_at']) for m in reversed(feed['mail']['items']) if m['from_actor_id'] == 'operator']
    return dict(status='ok', asks=asks, messages=messages, open_by_seat={}, damaged_rows=0)


def newsroom(office_db, quota_db, *, now, ledgers=(), layout=NO_ROOMS, queries=None, extra=None):
    """What the floor page draws: seats by role with their real state, the rooms they sit in, a live feed of mail, requests, wakes and tool calls, and the paper book.
    Everything this store does not hold is absent, not zero: the page says unknown."""
    feed = floor_feed(office_db, quota_db, now=now, limit=200)
    out = dict(state=feed['state'], as_of=feed['as_of'], office=dict(state='unknown', epoch=None, heartbeat_at=None), rooms={r: [] for r in layout['rooms']}, seats=None, activity=None, mail=None,
               requests=None, stats=None, tasks=None, sources=sources(queries or {}, office_db, quota_db, ledgers, extra) if _finite(now) else {},
               book=book(ledgers, now=now) if _finite(now) else dict(state='unknown', reason='clock_unknown', books=[], last_closed=[]))
    if feed['seats']['state'] != 'known':
        return out
    roles = {r['actor_id']: r['role'] for r in feed['roster']['items']}
    place = lambda role: next((room for room, members in layout['rooms'].items() if role in members), layout['fallback'])
    seen = {r['actor_id']: r for r in overview(office_db, quota_db, now=now)['seats']}
    unread = {a: r['unread_mail'] for a, r in seen.items()}
    seats = {}
    for s in feed['seats']['items']:
        a, kind = s['activity'], s['activity']['kind']
        doing = {'working': ('tool ' + s['current_tool']) if s['current_tool'] else 'awake', 'sleeping': 'sleeping until ' + time.strftime('%H:%M UTC', time.gmtime(a['until'])) if a['until'] else 'sleeping',
                 'parked': 'parked: ' + (a['reason'] or 'reason unknown'), 'idle': 'between wakes'}.get(kind)
        thought = s['messages'][0] if s['messages'] else None
        seats[s['role']] = dict(status=kind if kind in ('working', 'sleeping', 'parked') else None, last_action=doing, last_at=s['last_event_at'], last_tool=s['last_tool'], wakes_today=s['wakes_today'],
                                tokens_today=s['tokens_today'], unread_mail=unread.get(s['actor_id']), last_thought=_title(thought['text'], 400) if thought else None,
                                last_thought_at=thought['corrected_at'] if thought else None, actor_id=s['actor_id'], sleep_until=a['until'], parked_reason=a['reason'], requests=seen.get(s['actor_id'], {}).get('requests'),
                                last_wake=seen.get(s['actor_id'], {}).get('last_wake'), generation=next((r['generation'] for r in feed['roster']['items'] if r['actor_id'] == s['actor_id']), None))
        for room in [r for r, members in layout['rooms'].items() if s['role'] in members] or [layout['fallback']]:      # a seat may sit in several rooms
            if room is not None:
                out['rooms'].setdefault(room, []).append(s['role'])
    named = lambda actor: roles.get(actor, actor)
    room_of = lambda *actors: next((place(named(x)) for x in actors if x in roles), None)
    activity = []
    for e in feed['events']['items']:
        if e['actor_id'] not in roles or e['corrected_at'] is None:
            continue
        refs = e['refs']
        if e['kind'] == 'tool_call':
            kind, title = 'tool', _title(e['text'])
        elif e['kind'] in ('request', 'standup'):
            kind, title = e['kind'], _title(e['text'])
        elif e['kind'] == 'lifecycle' and e['text'] in ('wake_start', 'wake_end', 'sleep', 'parked', 'unparked'):
            kind, title = 'wake', {'wake_start': 'woke' + (' (%s)' % refs['type'] if refs.get('type') else ''), 'wake_end': 'wake ended' + (' (%s)' % refs['outcome'] if refs.get('outcome') else ''),
                                   'sleep': 'went to sleep', 'parked': 'parked: ' + str(refs.get('reason') or 'reason unknown'), 'unparked': 'unparked'}[e['text']]
        else:
            continue
        activity.append(dict(kind=kind, room=room_of(e['actor_id']), item_id=e['event_id'], at=e['corrected_at'], title=title, seat=named(e['actor_id'])))
    mail = [dict(**{'from': named(m['from_actor_id']), 'to': named(m['to_actor_id'])}, at=m['created_at'], text=_title(m['text'], 600), id=m['message_id']) for m in feed['mail']['items']]
    for m, raw in zip(mail, feed['mail']['items']):
        activity.append(dict(kind='mail', room=room_of(raw['from_actor_id'], raw['to_actor_id']), item_id=m['id'], at=m['at'], title='%s to %s: %s' % (m['from'], m['to'], _title(m['text'], 100)), seat=m['to'] if m['from'] == 'operator' else m['from'], **{'from': m['from'], 'to': m['to']}))
    activity.sort(key=lambda e: e['at'], reverse=True)
    requests = [dict(seat=named(q['actor_id']), at=q['corrected_at'], text=_title(q['text'], 600)) for q in feed['questions']['items']]
    out.update(tasks=[dict(seat=named(t['actor_id']), **{k: t[k] for k in ('task_id', 'status', 'title', 'note')}) for t in feed['tasks']['items']] if feed['tasks']['state'] != 'unknown' else None,
               seats=seats, activity=activity[:80], mail=sorted(mail, key=lambda m: m['at'], reverse=True)[:20], requests=requests[:40],
               stats=dict(requests=len(requests), tool_calls_1h=_tool_calls(office_db, now)),
               office=dict(state='running' if any(v['status'] == 'working' for v in seats.values()) else 'quiet', epoch=feed['roster']['items'][0]['epoch_id'] if feed['roster']['items'] else None,
                         heartbeat_at=max((v['last_at'] for v in seats.values() if v['last_at'] is not None), default=None)))
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

