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


def test_requests_show_what_seats_asked_and_where_each_ticket_stands(dbs):
    out = V.requests(dbs[0], now=NOW)
    assert out['state'] == 'known' and [(r['role'], r['kind'], r['why']) for r in out['asks']['items']] == [('reviewer', 'data', 'to check citations')]
    ticket = out['tickets']['items'][0]
    assert ticket['state'] == 'proposed' and ticket['problem'].startswith('The archive index') and ticket['steps'] == 0 and ticket['evidence_refs'] == ['event:request']


def test_budget_separates_measured_from_unmeasured_and_dates_each_account_reading(dbs):
    out = V.budget(*dbs, now=NOW);seats = {s['role']: s for s in out['seats']}
    assert seats['head']['largest_wake_today'] == 14300 and seats['head']['wakes_last_hour'] == 1 and seats['head']['unmeasured_wakes_today'] == 0
    assert seats['researcher']['tokens_today'] is None and seats['researcher']['unmeasured_wakes_today'] == 1 and seats['researcher']['largest_wake_today'] == 33000
    account = out['accounts']['items'][0]
    assert account['account'] == 'main' and account['age_s'] == 120 and [(w['name'], w['used_percent']) for w in account['windows']] == [('five_hour', 37.0), ('seven_day', 81.0)]
    V.clear_park(*dbs, actor_id=RESEARCHER, reason='fixed', now=NOW)
    assert [(c['role'], c['by']) for c in V.budget(*dbs, now=NOW)['clears']['items']] == [('researcher', 'operator')]
    without = V.budget(dbs[0], None, now=NOW)
    assert without['state'] == 'partial' and without['accounts']['state'] == 'unavailable' and all(s['tokens_today'] is None for s in without['seats'])


def test_evaluation_shows_released_aggregates_and_a_store_without_those_tables_says_unavailable(dbs, tmp_path):
    out = V.evaluation(dbs[0], now=NOW)
    assert [e['epoch_id'] for e in out['epochs']['items']] == ['e2', 'e1'] and [t['status'] for t in out['trials']['items']] == ['PASS_PENDING_REPLICATION', 'RUNNING']
    verdict = out['verdicts']['items'][0]
    assert verdict['state'] == 'pass_pending_replication' and verdict['lift_lower'] == 0.31 and len(verdict['release_hash']) == 64 and 'grades' not in verdict
    from sniper_office import seat_events as E, work as W
    bare = tmp_path / 'seat-only.db';conn = sqlite3.connect(bare);W.ensure_schema(conn);E.ensure_schema(conn);conn.commit();conn.close()
    out = V.evaluation(bare, now=NOW)
    assert out['state'] == 'known' and {out[k]['state'] for k in ('epochs', 'trials', 'verdicts')} == {'unavailable'}
    health = V.health(bare, now=NOW)
    assert health['lease'] is None and health['heartbeat'] is None and health['turns']['reason'] == 'no_turn_table' and V.requests(bare, now=NOW)['tickets']['state'] == 'unavailable'


def test_health_reads_lease_and_heartbeat_and_checks_chains_only_when_asked(dbs, monkeypatch):
    out = V.health(dbs[0], now=NOW)
    assert out['lease'] == dict(holder='supervisor-demo', generation=1, expires_at=NOW + 120, held=True) and out['heartbeat']['age_s'] == 4 and out['heartbeat']['progress'] == 'healthy_empty'
    assert {(t['state'], t['count']) for t in out['turns']['items']} == {('completed', 1), ('partial', 1)} and out['chains']['state'] == 'unavailable'
    assert V.health(dbs[0], now=NOW + 500)['lease']['held'] is False
    checked = {c['table']: c for c in V.health(dbs[0], now=NOW, verify=True)['chains']['items']}
    assert checked['trial_registry'] == dict(table='trial_registry', rows=2, result='intact') and checked['sealed_views']['result'] == 'intact' and 'grades' not in checked
    conn = sqlite3.connect(dbs[0]);conn.execute('DROP TRIGGER trial_registry_no_update');conn.execute("UPDATE trial_registry SET payload_json=replace(payload_json,'RUNNING','PASS') WHERE seq=1");conn.commit();conn.close()
    assert {c['table']: c for c in V.health(dbs[0], now=NOW, verify=True)['chains']['items']}['trial_registry']['result'] == 'broken_at_row_1'
    monkeypatch.setattr(V, 'CHAIN_ROWS', 1)
    assert {c['table']: c for c in V.health(dbs[0], now=NOW, verify=True)['chains']['items']}['trial_registry']['result'] == 'too_large_to_check_here'


def test_panels_are_forced_into_three_shapes_and_a_bad_panel_is_unavailable_without_its_error_text():
    table = V.panel(lambda now: dict(shape='table', columns=['a', 'b'], rows=[[1, object()], ['x' * 5000, None, 'extra']] + [[0, 0]] * 300), now=NOW)
    assert table['state'] == 'known' and len(table['rows']) == 200 and table['limited'] and table['rows'][0][1].startswith('<object') and len(table['rows'][1]) == 2 and len(table['rows'][1][0]) == 2000
    facts = V.panel(lambda now: dict(shape='facts', items=[dict(label='n', value=3, unit='items', as_of=now), dict(label='missing', value=None)]), now=NOW)
    assert facts['items'] == [dict(label='n', value=3, unit='items', as_of=NOW), dict(label='missing', value=None, unit=None, as_of=None)]
    series = V.panel(lambda now: dict(shape='series', unit='per hour', points=[[i, i * 2.] for i in range(2500)] + [[1, float('nan')], ['x', 1]]), now=NOW)
    assert len(series['points']) == 2000 and series['limited'] and series['dropped'] == 2 and series['points'][-1] == (2499, 4998.)
    def boom(now):raise RuntimeError('password=hunter2')
    for bad, reason in ((boom, 'panel_failed'), (lambda now: dict(shape='pie'), 'panel_shape_invalid'), (lambda now: [1], 'panel_shape_invalid'), (lambda now: dict(shape='table', columns=[], rows=[]), 'panel_shape_invalid')):
        out = V.panel(bad, now=NOW)
        assert out == dict(state='unavailable', reason=reason) and 'hunter2' not in str(out)
