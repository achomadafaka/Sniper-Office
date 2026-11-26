"""Per-seat receipts for authenticated Control notes."""
from contextlib import closing
from pathlib import Path
import sqlite3
import uuid

from . import common as C


def queue(ledger, office_path, to_role, text, now):
    if not text.strip():
        return dict(state='refused', reason='text_required')
    if len(text) > 2000:
        return dict(state='refused', reason='text_too_long')
    role = (to_role or '').strip()
    role = None if not role or role.lower() == 'all' else role
    try:
        text.encode('utf-8')
        if role:
            role.encode('utf-8')
    except UnicodeError:
        return dict(state='refused', reason='invalid_unicode')
    try:
        with closing(C.ro(office_path)) as office:
            recipients = office.execute(
                'SELECT actor_id,epoch_id,generation FROM office_v2_roster WHERE sealed=0 AND (? IS NULL OR role=?)',
                (role, role)).fetchall()
    except sqlite3.Error:
        return dict(state='refused', reason='roster_unavailable')
    if not recipients:
        return dict(state='refused', reason='role_unknown' if role else 'no_active_seats')
    with C.txn(ledger):
        note_id = ledger.execute("INSERT INTO bk_operator_chat(at,sender,to_role,text) VALUES(?,'operator',?,?)",
                                 (now, role, text)).lastrowid
        ledger.executemany('INSERT INTO bk_operator_note_delivery(receipt_id,note_id,actor_id,epoch_id,generation) VALUES(?,?,?,?,?)',
                           [(uuid.uuid4().hex, note_id, *recipient) for recipient in recipients])
    return dict(state='recorded', note_id=note_id, recipient_n=len(recipients))


def _has_receipts(ledger):
    return ledger.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bk_operator_note_delivery'").fetchone() is not None


def recent(ledger):
    notes = [dict(row) for row in ledger.execute('SELECT * FROM bk_operator_chat ORDER BY id DESC LIMIT 40')]
    has_receipts = _has_receipts(ledger)
    for note in notes:
        total, delivered = ledger.execute(
            'SELECT COUNT(*),COUNT(delivered_at) FROM bk_operator_note_delivery WHERE note_id=?',
            (note['id'],)).fetchone() if has_receipts else (0, 0)
        note.update(recipient_n=total, delivered_n=delivered,
                    delivery='legacy' if not total else 'delivered' if total == delivered else 'pending')
    return notes


def pending(ledger, actor, epoch, generation):
    if not _has_receipts(ledger):
        return []
    return ledger.execute('''SELECT n.id,n.at,n.text,d.receipt_id FROM bk_operator_note_delivery d
                          JOIN bk_operator_chat n ON n.id=d.note_id
                          WHERE d.actor_id=? AND d.epoch_id=? AND d.generation=? AND d.delivered_at IS NULL
                          ORDER BY n.id LIMIT 8''', (actor, epoch, generation)).fetchall()


def mark_delivered(ledger, actor, epoch, generation, receipts, now):
    with C.txn(ledger):
        ledger.executemany('''UPDATE bk_operator_note_delivery SET delivered_at=?
                           WHERE receipt_id=? AND actor_id=? AND epoch_id=? AND generation=? AND delivered_at IS NULL''',
                           [(now, receipt, actor, epoch, generation) for receipt in receipts])



def for_wake(seat):
    path = seat.cfg.get('markets_ledger_db')
    if not path or not Path(path).is_file():
        return None
    epoch, generation = seat.cfg['epoch'], seat.cfg['generation']
    identity = seat.office.execute(
        'SELECT 1 FROM office_v2_roster WHERE actor_id=? AND epoch_id=? AND generation=? AND incarnation=? AND sealed=0',
        (seat.actor, epoch, generation, seat.state['incarnation'])).fetchone()
    if not identity:
        return None
    with closing(C.ro(path)) as ledger:
        rows = pending(ledger, seat.actor, epoch, generation)
    if not rows:
        return None
    notes = [dict(id=r['receipt_id'], at=r['at'], text=r['text']) for r in rows]

    def acknowledge():
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=rw', uri=True, isolation_level=None)) as ledger:
            mark_delivered(ledger, seat.actor, epoch, generation, [r['receipt_id'] for r in rows], seat.clock())

    return notes, acknowledge
