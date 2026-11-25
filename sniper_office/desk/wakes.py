"""Wake sources of the desk plug-in: a new feed ticket, a closed paper order, a position alert. Each says why a seat should wake and names the facts in the prompt (host data, not instructions).

Once-only per seat is a row in the seat's own office db (desk_wakes), written when the wake is handed to the seat, so a restart never repeats one.
Config keys, per seat: ticket_wakes (true to get ticket wakes; false or absent: none), ticket_wake_min_s (default 300), alert_loss_pct (15), alert_gain_pct (50), alert_trail_arm_pct (20),
alert_mark_max_age_s (300), wake_lookback_s (closed orders and tickets older than this never wake; default 86400 for orders, feed_window_s for tickets)."""
from contextlib import closing
import json
import sqlite3

from . import paper as P

NO_FEED_ROLES = ('researcher', 'narrative')
SCHEMA = 'CREATE TABLE IF NOT EXISTS desk_wakes(actor_id TEXT NOT NULL,kind TEXT NOT NULL,key TEXT NOT NULL,at REAL NOT NULL,PRIMARY KEY(actor_id,kind,key))'


def _done(seat, kind, key):
    return seat.office.execute('SELECT 1 FROM desk_wakes WHERE actor_id=? AND kind=? AND key=?', (seat.actor, kind, key)).fetchone() is not None


def _mark(seat, kind, key, now):
    seat.office.execute('INSERT OR IGNORE INTO desk_wakes VALUES(?,?,?,?)', (seat.actor, kind, key, now))


def _ledger(seat):
    """A read-only connection to the Office ledger, or None."""
    path = P.path_of(seat, 'ledger_db')
    try:
        return P.ro(path) if path else None
    except (OSError, sqlite3.Error):
        return None


def _book(seat):
    return seat.cfg.get('ledger_epoch') or seat.cfg.get('epoch')


def _ticket_wake(seat, now):
    cfg = seat.cfg
    if cfg.get('ticket_wakes') is not True or cfg.get('role') in NO_FEED_ROLES or cfg.get('role') is None:
        return None
    gap = cfg.get('ticket_wake_min_s', 300)
    last = seat.office.execute("SELECT max(at) FROM desk_wakes WHERE actor_id=? AND kind='ticket'", (seat.actor,)).fetchone()[0]
    if last is not None and now - last < gap:
        return None
    rows, _, bad = P._stored_tickets(seat, now, 200)
    if bad or not rows:
        return None
    row = seat.office.execute('SELECT rules_json FROM desk_feed_filters WHERE epoch_id=? AND actor_id=? ORDER BY seq DESC LIMIT 1', (cfg.get('epoch'), seat.actor)).fetchone()
    rules = json.loads(row[0]) if row else []
    fresh = [r for r in rows if not _done(seat, 'ticket', r[0]) and not any(P.rule_hides(rule, P._checks(r[4])) for rule in rules)]
    if not fresh:
        return None
    for r in fresh:   # one wake covers every new ticket now visible: the rate limit would otherwise leave a backlog of wakes
        _mark(seat, 'ticket', r[0], now)
    more = len(fresh) - 1
    return dict(type='new_ticket', urgent=False, prompt='New feed ticket %s (mint %s, %d s old) passed the hard filter.%s Read it with sheet_read ticket_id=%s.' % (
        fresh[0][0], fresh[0][1], round(now - (P._stamp(fresh[0][3]) or now)), ' %d other new ticket(s) are in feed_list.' % more if more else '', fresh[0][0]))


def _plan(seat, order_id):
    row = seat.office.execute("SELECT detail_json FROM desk_paper_requests WHERE kind='order' AND request_id=?", (order_id,)).fetchone()
    try:
        return json.loads(row[0]).get('plan') if row else None
    except ValueError:
        return None


def _price(proof):
    try:
        return json.loads(proof).get('price_usd')
    except (TypeError, ValueError, AttributeError):
        return None


def _close_wake(seat, now):
    if seat.cfg.get('role') not in P.ORDER_ROLES:
        return None
    with closing(_ledger(seat) or sqlite3.connect(':memory:')) as conn:
        try:
            rows = conn.execute("SELECT order_id,mint,state,exit_reason,entry_price_usd,exit_proof,spend_usd,closed_at,exit_usd FROM orders WHERE book_id=? AND state='closed' AND closed_at>=? ORDER BY closed_at",
                                (_book(seat), now - seat.cfg.get('wake_lookback_s', 86400))).fetchall()
            for oid, mint, _, reason, entry, proof, spend, closed_at, gross in rows:
                if _done(seat, 'close', oid):
                    continue
                net = conn.execute('SELECT COALESCE(SUM(usd),0) FROM cashflows WHERE order_id=?', (oid,)).fetchone()[0]
                _mark(seat, 'close', oid, now)
                return dict(type='order_closed', urgent=False, prompt='Your paper order %s (mint %s) closed %d s ago. Exit reason: %s. Entry price USD %s, exit price USD %s (modelled, no landing proof). Net result USD %.2f on %.2f spent. Plan at entry: %s.' % (
                    oid, mint, round(now - closed_at), reason, entry, _price(proof), net, spend or 0., json.dumps(_plan(seat, oid), default=str)))
        except sqlite3.Error:
            return None
    return None


def _alert_wake(seat, now):
    if seat.cfg.get('role') not in P.ORDER_ROLES:
        return None
    cfg = seat.cfg
    loss, gain, arm, max_age = cfg.get('alert_loss_pct', 15.), cfg.get('alert_gain_pct', 50.), cfg.get('alert_trail_arm_pct', 20.), cfg.get('alert_mark_max_age_s', 300)
    with closing(_ledger(seat) or sqlite3.connect(':memory:')) as conn:
        try:
            for oid, mint, spend, peak, pol_json in conn.execute("SELECT order_id,mint,spend_usd,peak_value_usd,policy_json FROM orders WHERE book_id=? AND state='open'", (_book(seat),)).fetchall():
                mark = conn.execute('SELECT at,value_usd,price_usd FROM marks WHERE order_id=? AND value_usd IS NOT NULL ORDER BY at DESC,id DESC LIMIT 1', (oid,)).fetchone()
                if not mark or not spend or spend <= 0 or now - mark[0] > max_age:
                    continue
                pnl = (mark[1] / spend - 1.) * 100.
                try:
                    trail = json.loads(pol_json).get('trail_pct') or 0
                except (TypeError, ValueError, AttributeError):
                    trail = 0
                hits = [name for name, hit in (('loss', pnl <= -loss), ('gain', pnl >= gain), ('trail_armed', trail > 0 and (peak or 0) >= spend * (1 + arm / 100.))) if hit]
                for name in hits:
                    if _done(seat, 'alert', oid + ':' + name):
                        continue
                    _mark(seat, 'alert', oid + ':' + name, now)
                    return dict(type='position_alert', urgent=True, prompt='Position alert %s on your open paper order %s (mint %s): %.1f%% from entry on the newest modelled mark (%d s old, USD value %.2f on %.2f spent), peak value USD %s, trail %s%%. Close it with paper_exit if your plan says so.' % (
                        name, oid, mint, pnl, round(now - mark[0]), mark[1], spend, peak, trail))
        except sqlite3.Error:
            return None
    return None


def source(seat, urgent):
    """Seat.due hands over a wake source's answer only for its own urgency, so each call computes (and marks) only what the caller will take."""
    seat.office.executescript(SCHEMA + ';' + P.SCHEMA)
    now = P._now(seat)
    if now is None:
        return None   # no trusted clock: no wake rather than a wrong age
    if urgent:
        return _alert_wake(seat, now)
    return _close_wake(seat, now) or _ticket_wake(seat, now)


def register(host):
    host.add_wake_source(source)
