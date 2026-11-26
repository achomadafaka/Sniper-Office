import sqlite3
from types import SimpleNamespace

from sniper_office import seat_events
from sniper_office.markets import api, operator_notes

from conftest import NOW


def test_direct_and_broadcast_notes_track_each_active_recipient(dbs):
    feed, ledger, folder = dbs
    office_path = folder / 'office.db'
    with sqlite3.connect(office_path) as office:
        seat_events.ensure_schema(office)
        office.executemany('INSERT INTO office_v2_roster VALUES(?,?,?,?,?,?,?)', [
            ('desk', 'epoch', 'newsdesk', 1, 1, 0, NOW),
            ('coach', 'epoch', 'coach', 1, 1, 0, NOW),
            ('retired', 'epoch', 'retired', 1, 1, 1, NOW),
        ])
    ledger.execute("INSERT INTO bk_operator_chat(at,sender,text) VALUES(?,'operator','legacy')", (NOW,))
    assert api.operator_say(ledger, office_path, 'retired', 'skip', NOW)['reason'] == 'role_unknown'
    assert api.operator_say(ledger, office_path, 'coach', '  ', NOW)['reason'] == 'text_required'
    directed = api.operator_say(ledger, office_path, 'coach', 'Review this', NOW)['note_id']
    broadcast = api.operator_say(ledger, office_path, 'all', 'Read this', NOW)['note_id']
    pending = lambda actor, epoch='epoch', generation=1: operator_notes.pending(ledger, actor, epoch, generation)
    assert [r['id'] for r in pending('coach')] == [directed, broadcast]
    assert [r['id'] for r in pending('desk')] == [broadcast]
    assert not pending('retired') and not pending('desk', 'different') and not pending('desk', generation=2)

    operator_notes.mark_delivered(ledger, 'coach', 'epoch', 1, [r['receipt_id'] for r in pending('coach')], NOW + 10)
    assert not pending('coach') and [r['id'] for r in pending('desk')] == [broadcast]
    rows = api.control(feed, ledger, NOW + 10, {})['operator_chat']
    assert [(r['delivery'], r['delivered_n'], r['recipient_n']) for r in rows] == [
        ('pending', 1, 2), ('delivered', 1, 1), ('legacy', 0, 0),
    ]


def test_receipt_source_follows_current_incarnation_and_acknowledges_that_batch(dbs):
    _, ledger, folder = dbs
    path = folder / 'office.db'
    with sqlite3.connect(path) as office:
        seat_events.ensure_schema(office)
        office.execute("INSERT INTO office_v2_roster VALUES('desk','epoch','newsdesk',1,2,0,?)", (NOW,))
    note = api.operator_say(ledger, path, '', 'Before the wake', NOW)['note_id']
    with sqlite3.connect(path) as office:
        seat = SimpleNamespace(actor='desk', office=office, clock=lambda: NOW + 5, state=dict(incarnation=1),
                               cfg=dict(epoch='epoch', generation=1, markets_ledger_db=folder / 'ledger.db'))
        assert operator_notes.for_wake(seat) is None
        seat.state['incarnation'] = 2
        batch, acknowledge = operator_notes.for_wake(seat)
        assert [entry['text'] for entry in batch] == ['Before the wake']
        later = api.operator_say(ledger, path, 'newsdesk', 'After the wake started', NOW + 1)['note_id']
        acknowledge()
        assert [r['id'] for r in operator_notes.pending(ledger, 'desk', 'epoch', 1)] == [later]
        assert [r['delivery'] for r in operator_notes.recent(ledger) if r['id'] == note] == ['delivered']
