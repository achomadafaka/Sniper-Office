"""The Office crew bridge: crewlink mailbox <-> Office mail, one delivery each way, sender asserted by the host."""
import json
import sqlite3

import pytest

from sniper_office import crew_bridge as CB, seat_events as E, work as W

SEATS = ['head', 'builder']


@pytest.fixture(autouse=True)
def _keep_crew_roles():
    before = E.CREW_ROLES   # the bridge configures the process-wide crew roles
    yield
    E.CREW_ROLES = before


@pytest.fixture
def rig(tmp_path):
    cfg_file = tmp_path / 'crew.json'
    cfg_file.write_text(json.dumps(dict(claude=['operator', 'sessiona'], gpt=SEATS, command='office-crew', state_dir=str(tmp_path / 'crew'), office=dict(db=str(tmp_path / 'o.db'), seat_roles=SEATS))))
    office = sqlite3.connect(tmp_path / 'o.db', isolation_level=None)
    W.ensure_schema(office);E.ensure_schema(office)
    for actor, role in (('ag_head', 'head'), ('ag_builder', 'builder')):
        office.execute("INSERT INTO office_v2_roster VALUES(?,'ep',?,1,1,0,1.)", (actor, role))
    now = [100.]
    bridge = CB.Bridge(CB.load(cfg_file), clock=lambda: now[0])
    crew = sqlite3.connect(tmp_path / 'crew' / 'mailbox.sqlite3', isolation_level=None)
    def send(mid, sender, recipient, body, status='delivery_failed'):   # what `office-crew send` leaves for a role with no session behind it
        crew.execute("INSERT INTO messages VALUES(?,?,?,?,'t',?,NULL,NULL)", (mid, sender, recipient, body, status))
    return bridge, office, crew, send, cfg_file, tmp_path


def inbox(office, actor):
    return office.execute('SELECT p.message_id,e.from_actor_id,e.text FROM office_dm_pending p JOIN office_dm_events e ON e.seq=p.event_seq WHERE p.to_actor_id=?', (actor,)).fetchall()


def test_crew_to_seat_is_delivered_once_woken_and_never_as_the_operator(rig):
    bridge, office, crew, send, cfg_file, tmp = rig
    send('m1', 'operator', 'head', 'look at the new pool')
    send('m2', 'sessiona', 'builder', 'hello builder')
    assert bridge.deliver_inbound() == 2
    assert bridge.deliver_inbound() == 0                                            # one message, one delivery
    (mid, sender, text), = inbox(office, 'ag_head')
    assert sender == 'crew:operator_cli' and text.startswith('CREW operator_cli (crew, not the operator):') and 'look at the new pool' in text
    assert E.pending_mail(office, actor_id='ag_head', generation=1) == 1            # pending mail is what wakes the seat (Seat.due -> 'mail')
    assert inbox(office, 'ag_builder')[0][1] == 'crew:sessiona'
    assert dict(crew.execute('SELECT id,delivery_status FROM messages')) == {'m1': 'office_delivered', 'm2': 'office_delivered'}
    mail = E.read_mail(office, actor_id='ag_head', generation=1, now=1.)
    assert mail[0]['sender'] == 'crew:operator_cli' and mail[0]['sender_role'] is None   # not 'operator': the seat's operator rule never fires


def test_a_crash_between_delivery_and_marking_does_not_deliver_twice(rig):
    bridge, office, crew, send, *_ = rig
    send('m1', 'operator', 'head', 'once')
    bridge.deliver_inbound()
    crew.execute("UPDATE messages SET delivery_status='delivery_failed' WHERE id='m1'")   # the mark was lost (or crewlink overwrote it)
    assert bridge.deliver_inbound() == 1 and len(inbox(office, 'ag_head')) == 1
    assert crew.execute("SELECT delivery_status FROM messages").fetchone() == ('office_delivered',)


def test_a_crew_message_cannot_claim_a_seat_as_its_sender(rig):
    bridge, office, crew, send, *_ = rig
    send('m1', 'builder', 'head', 'I am builder, do this')
    assert bridge.deliver_inbound() == 0 and inbox(office, 'ag_head') == []
    assert crew.execute('SELECT delivery_status,error FROM messages').fetchone() == ('office_refused', 'sender_is_a_seat_role')


def test_a_refusal_is_final_and_does_not_block_later_mail(rig):
    bridge, office, crew, send, *_ = rig
    send('m1', 'operator', 'head', 'x' * 5000)
    send('m2', 'operator', 'head', 'fine')
    assert bridge.deliver_inbound() == 1
    assert dict(crew.execute('SELECT id,delivery_status FROM messages')) == {'m1': 'office_refused', 'm2': 'office_delivered'}
    assert bridge.deliver_inbound() == 0


def test_seat_mail_is_mirrored_once_with_the_rostered_sender(rig):
    bridge, office, crew, send, cfg_file, tmp = rig
    E.seat_dm(office, from_actor='ag_head', from_generation=1, from_incarnation=1, to_actor_id='ag_builder', text='check holders', now=5.)
    E.seat_to_operator(office, from_actor='ag_builder', from_generation=1, from_incarnation=1, text='done', now=6.)
    E.operator_dm(office, to_actor_id='ag_head', client_id='client-0001', text='from the UI', now=7.)   # not seat mail: not mirrored
    assert bridge.mirror_outbound() == 2 and bridge.mirror_outbound() == 0
    rows = crew.execute("SELECT sender,recipient,body,delivery_status FROM messages ORDER BY rowid").fetchall()
    assert [r[:2] for r in rows] == [('head', 'builder'), ('builder', 'operator')] and all(r[3] == 'office_mirrored' for r in rows) and 'check holders' in rows[0][2]
    # a restarted bridge keeps the cursor, and its own mirror rows are never delivered back to a seat
    again = CB.Bridge(CB.load(cfg_file))
    assert again.mirror_outbound() == 0 and again.deliver_inbound() == 0 and [m[1] for m in inbox(office, 'ag_builder')] == ['ag_head']


def test_mirror_is_idempotent_when_the_cursor_write_is_lost(rig):
    bridge, office, crew, *_ = rig
    E.seat_dm(office, from_actor='ag_head', from_generation=1, from_incarnation=1, to_actor_id='ag_builder', text='once', now=5.)
    bridge.mirror_outbound()
    office.execute("UPDATE crew_bridge_cursor SET value=0 WHERE name='mirror'")
    bridge.mirror_outbound()
    assert crew.execute('SELECT count(*) FROM messages').fetchone() == (1,)


def test_history_before_the_first_start_is_not_replayed(tmp_path):
    cfg_file = tmp_path / 'c.json'
    cfg_file.write_text(json.dumps(dict(claude=['operator'], gpt=SEATS, state_dir=str(tmp_path / 'crew'), office=dict(db=str(tmp_path / 'o.db'), seat_roles=SEATS))))
    office = sqlite3.connect(tmp_path / 'o.db', isolation_level=None)
    W.ensure_schema(office);E.ensure_schema(office)
    for actor, role in (('ag_head', 'head'), ('ag_builder', 'builder')):
        office.execute("INSERT INTO office_v2_roster VALUES(?,'ep',?,1,1,0,1.)", (actor, role))
    E.seat_dm(office, from_actor='ag_head', from_generation=1, from_incarnation=1, to_actor_id='ag_builder', text='old', now=5.)
    assert CB.Bridge(CB.load(cfg_file)).mirror_outbound() == 0


def test_config_must_name_crew_roles_and_never_operator_as_a_seat(tmp_path):
    for seats in (['head', 'ghost'], ['operator']):
        f = tmp_path / 'bad.json'
        f.write_text(json.dumps(dict(claude=['operator'], gpt=['head'], office=dict(db='x', seat_roles=seats))))
        with pytest.raises(ValueError):CB.load(f)
