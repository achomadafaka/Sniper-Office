"""The UI's read models against a store built with the seat runtime's own writers."""
import importlib.util
from pathlib import Path
import sqlite3

import pytest

from sniper_office import views as V

_spec = importlib.util.spec_from_file_location('ui_demo_data', Path(__file__).resolve().parents[1] / 'examples/ui_demo_data.py')
demo = importlib.util.module_from_spec(_spec);_spec.loader.exec_module(demo)
NOW = demo.NOW
HEAD, RESEARCHER, REVIEWER, ARCHIVIST = (s[0] for s in demo.SEATS)


@pytest.fixture
def dbs(tmp_path):
    return demo.build(tmp_path)


def by_role(out):
    return {s['role']: s for s in out['seats']}


def test_overview_reads_each_seat_state_from_what_the_host_recorded(dbs):
    out = V.overview(*dbs, now=NOW);seats = by_role(out)
    assert out['state'] == 'known' and len(out['seats']) == 4
    assert seats['head']['status'] == 'asleep' and seats['head']['sleep_until'] == pytest.approx(NOW - 2802 + 900) and seats['head']['last_wake']['outcome'] == 'ok'
    assert (seats['researcher']['status'], seats['researcher']['reason']) == ('parked', 'wake_usage_unknown')
    assert (seats['reviewer']['status'], seats['reviewer']['reason']) == ('awake', 'mail') and seats['reviewer']['requests'] == 1
    assert seats['archivist']['status'] == 'unknown' and seats['archivist']['wakes_today'] == 0
    assert {(a['kind'], a['role']) for a in out['alerts']} == {('parked', 'researcher'), ('no_signal', 'archivist')}
    assert seats['researcher']['unread_mail'] == 1 and seats['head']['unread_mail'] == 1 and out['operator_inbox'] == 1


def test_tokens_are_a_total_only_when_every_wake_was_measured(dbs):
    seats = by_role(V.overview(*dbs, now=NOW))
    assert seats['head']['wakes_today'] == 3 and seats['head']['tokens_today'] == sum(int(1.1 * (12000 + n * 500)) for n in range(3))
    assert seats['researcher']['wakes_today'] == 2 and seats['researcher']['tokens_today'] is None and seats['researcher']['usage_reason'] == 'some_wakes_unmeasured'
    no_quota = by_role(V.overview(dbs[0], None, now=NOW))
    assert no_quota['head']['tokens_today'] is None and no_quota['head']['usage_reason'] == 'quota_unavailable'


def test_a_missing_or_broken_store_is_unknown_with_a_reason_never_an_empty_office(tmp_path, dbs):
    out = V.overview(tmp_path / 'absent.db', None, now=NOW)
    assert out['state'] == 'unknown' and out['seats'] == [] and out['reason'].startswith('store_unavailable')
    (tmp_path / 'junk.db').write_text('not a database')
    assert V.mail(tmp_path / 'junk.db', now=NOW)['state'] == 'unknown'
    assert V.overview(*dbs, now=None)['reason'] == 'clock_unknown' and V.seat(*dbs, 'nobody', now=NOW)['reason'] == 'unknown_seat'
    for bad in (dict(limit=0), dict(limit=500), dict(before='7')):
        assert V.seat(*dbs, HEAD, now=NOW, **bad)['reason'] == 'bad_arguments'


def test_seat_timeline_pages_back_marks_older_identities_and_keeps_hostile_text_as_text(dbs):
    out = V.seat(*dbs, REVIEWER, now=NOW)
    assert out['state'] == 'known' and out['seat']['status'] == 'awake' and [e['seq'] for e in out['events']] == sorted((e['seq'] for e in out['events']), reverse=True)
    assert out['events'][0]['earlier_identity'] is True and out['events'][1]['earlier_identity'] is False
    hostile = [e for e in out['events'] if e['text'] == demo.HOSTILE]
    assert len(hostile) == 1 and hostile[0]['kind'] == 'thought'
    assert any(e['web_tainted'] for e in out['events']) and out['wakes'][0]['used'] == 24200
    first = V.seat(*dbs, HEAD, now=NOW, limit=5)
    assert first['limited'] and first['state'] == 'partial' and len(first['events']) == 5
    older = V.seat(*dbs, HEAD, now=NOW, limit=5, before=first['cursor'])
    assert older['events'] and max(e['seq'] for e in older['events']) < min(e['seq'] for e in first['events'])
    assert [m['sender'] for m in out['mail']] == [REVIEWER] and out['mail'][0]['unread'] is True


def test_mail_shows_host_assigned_roles_and_what_is_still_unread(dbs):
    out = V.mail(dbs[0], now=NOW)
    assert [(m['sender_role'], m['to_role'], m['unread']) for m in out['items']] == [('head', 'operator', False), ('reviewer', 'head', True), ('head', 'researcher', True)]
    assert {s['role'] for s in out['seats']} == {'head', 'researcher', 'reviewer', 'archivist'} and V.mail(dbs[0], now=NOW, limit=2)['limited']


def test_reads_never_write_and_cannot_hold_the_store(dbs):
    before = [Path(p).read_bytes() for p in dbs]
    V.overview(*dbs, now=NOW);V.seat(*dbs, HEAD, now=NOW);V.mail(dbs[0], now=NOW)
    assert [Path(p).read_bytes() for p in dbs] == before
    with V._ro(dbs[0]) as conn:
        with pytest.raises(sqlite3.OperationalError):conn.execute("INSERT INTO office_v2_roster VALUES('x','e','r',1,1,0,1.)")


def test_operator_mail_reaches_one_seat_once_and_refusals_are_typed(dbs):
    sent = V.send_operator_mail(dbs[0], to_actor_id=RESEARCHER, client_id='client-0001', text='Use the 2025 filing.', now=NOW)
    assert sent['state'] == 'sent'
    assert V.send_operator_mail(dbs[0], to_actor_id=RESEARCHER, client_id='client-0001', text='Use the 2025 filing.', now=NOW)['state'] == 'duplicate'
    newest = V.mail(dbs[0], now=NOW)['items'][0]
    assert (newest['sender'], newest['sender_role'], newest['to_role'], newest['unread']) == ('operator', 'operator', 'researcher', True)
    for kw, reason in ((dict(to_actor_id='nobody'), 'unknown_actor'), (dict(text=' '), 'text_invalid'), (dict(text='x' * 5000), 'text_invalid'), (dict(client_id='short'), 'client_id_invalid'),
                       (dict(client_id='client-0001', text='changed'), 'client_id_conflict')):
        assert V.send_operator_mail(dbs[0], **{**dict(to_actor_id=RESEARCHER, client_id='client-0002', text='ok', now=NOW), **kw}) == dict(state='refused', reason=reason)


def test_clear_park_is_logged_as_the_operator_and_needs_a_real_seat_and_a_reason(dbs):
    assert V.clear_park(*dbs, actor_id=RESEARCHER, reason='usage reading fixed', now=NOW) == dict(state='cleared', actor_id=RESEARCHER)
    assert sqlite3.connect(dbs[1]).execute('SELECT actor_id,cleared_by,reason FROM seat_park_clears').fetchall() == [(RESEARCHER, 'operator', 'usage reading fixed')]
    assert V.clear_park(*dbs, actor_id='nobody', reason='x', now=NOW)['reason'] == 'unknown_seat'
    assert V.clear_park(*dbs, actor_id=RESEARCHER, reason='  ', now=NOW)['state'] == 'refused'
