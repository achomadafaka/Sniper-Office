"""Standing sessions: a seat's session runs across wakes and is handed over after rotate_after_wakes wakes. The size knob stays as the safety net."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from sniper_office import plugins as P,quota_measurement as Q,seat_events as E,seat_host as H,work as W

SESSION='11111111-1111-1111-1111-111111111111'
USAGE=dict(input_tokens=10,output_tokens=2,cache_read_input_tokens=9,cache_creation_input_tokens=3)


@pytest.fixture
def seat_factory(tmp_path,monkeypatch):
    def make(**cfg_extra):
        office=sqlite3.connect(':memory:',isolation_level=None);W.ensure_schema(office);E.ensure_schema(office)
        office.execute("INSERT INTO office_v2_roster VALUES('ag1','ep','head',1,1,0,1.)")
        quota=sqlite3.connect(':memory:',isolation_level=None);Q.ensure_schema(quota);H.ensure_schema(quota)
        prompt=tmp_path/'prompt';prompt.write_text('system')
        cfg=dict(actor='ag1',role='head',epoch='ep',generation=1,host_state=str(tmp_path/'s.json'),workspace=str(tmp_path),office_db='unused',model='t',prompt_file=str(prompt),
                 wake_deadline_s=1,max_turns=1,state_dir=str(tmp_path),account_alias='a',absolute_ceiling=200000,default_sleep_s=60,sdk_version='t',usage_scope='per_wake',uncapped=True)
        cfg.update(cfg_extra)
        s=H.Seat(cfg=cfg,office_conn=office,quota_conn=quota,clock=lambda:1000.)
        s._snapshot_credential=lambda:None;s._rotate_notebook=lambda:None;s._publish_workspace=lambda:None;s._cleanup_proven=lambda:True;s._recover_session=lambda note:note
        s.events=[];s._event=lambda kind,text,**k:s.events.append((kind,text,k.get('refs')))
        return s
    sessions=[SESSION]
    fake=SimpleNamespace(start=lambda:None,stop=lambda:True,sleep_until=None,tainted=False,operator_mail=False)
    monkeypatch.setattr(H.T,'SeatTools',lambda **k:fake)
    monkeypatch.setattr(H.T,'stop_unit',lambda *a:True)
    monkeypatch.setattr(H.B,'unit_args',lambda *a,**k:[]);monkeypatch.setattr(H.B,'harness_args',lambda *a,**k:[])
    monkeypatch.setattr(H.B,'office_uid',lambda:1)
    def child(argv,**k):
        line=json.dumps(dict(kind='result',session_id=sessions[-1],result_id='r',usage=USAGE,sdk_version='t',is_error=False,num_turns=1))
        return dict(outcome='success',returncode=0,stdout=(line+'\n').encode(),stderr=b'')
    monkeypatch.setattr(H,'run_child',child)
    make.sessions=sessions
    return make


def test_rotation_is_due_only_after_n_wakes(seat_factory):
    s=seat_factory(rotate_after_wakes=3)
    for n in (1,2):
        s.wake('mail');assert s.state['session_wakes']==n and not s.state.get('rotate_due')
    s.wake('mail')
    assert s.state['session_wakes']==3 and s.state['rotate_due'] is True
    assert ('lifecycle','rotate_after_wakes',dict(wakes=3,after=3)) in s.events


def test_default_is_sixty_and_zero_turns_it_off(seat_factory):
    assert H.ROTATE_AFTER_WAKES==60
    s=seat_factory(rotate_after_wakes=0)
    for _ in range(5):s.wake('mail')
    assert not s.state.get('rotate_due')


def test_the_counter_survives_a_restart_and_resets_with_a_new_session(seat_factory):
    s=seat_factory(rotate_after_wakes=3)
    s.wake('mail');s.wake('mail')
    again=seat_factory(rotate_after_wakes=3)   # same host_state file: a restarted host
    assert again.state['session_wakes']==2
    again.wake('mail');assert again.state['rotate_due'] is True
    again._new_incarnation();again.state['rotate_due']=False   # what rotate() does
    assert again.state['session_wakes']==0
    seat_factory.sessions.append('22222222-2222-2222-2222-222222222222')
    again.wake('mail');assert again.state['session_wakes']==1 and not again.state['rotate_due']


def test_handover_wakes_are_not_counted(seat_factory):
    s=seat_factory(rotate_after_wakes=5)
    s.wake('mail');s.wake('swap',extra='write it');s.wake('swap',extra='write it')
    assert s.state['session_wakes']==1


def test_the_size_guard_still_rotates_early(seat_factory):
    s=seat_factory(rotate_after_wakes=60,rotate_cache_read=5)   # cache read 9 >= 5, no per-request knob
    s.wake('mail');assert s.state['rotate_due'] is True and s.state['session_wakes']==1


def test_a_completed_handover_goes_to_the_plugins_once(seat_factory):
    s=seat_factory(rotate_after_wakes=1);s.state['rotate_due']=True
    got=[];s.plugins.add_handover_hook(lambda seat,note:got.append((seat.actor,note)))
    s.plugins.add_handover_hook(lambda seat,note:1/0)   # a broken hook never blocks the handover
    notes=['old','new note'];s._read_note=lambda:notes.pop(0)
    s._note_bytes=lambda:10;s.wake=lambda *a,**k:None
    assert s.rotate()=='rotated' and got==[('ag1','new note')]


def test_a_fresh_session_gets_boot_context_and_a_resumed_one_does_not(seat_factory):
    s=seat_factory();s.plugins.add_boot_source(lambda seat:'BOOT for '+seat.cfg['role']);s.plugins.add_boot_source(lambda seat:1/0)
    assert 'BOOT for head' in s._prompt('mail',None,True) and 'BOOT' not in s._prompt('mail',None,False)


def test_a_session_already_running_when_the_counter_arrives_is_counted_from_the_ledger(seat_factory):
    s=seat_factory(rotate_after_wakes=3)
    for i in range(2):   # two earlier wakes of this session, recorded before the counter existed
        Q.record_wake(s.quota,epoch_id='ep',actor_id='ag1',generation=1,incarnation='1',session_id=SESSION,wake_id='w%d'%i,account_alias='a',outcome='ok',counters=USAGE,result_id='r',
                      started_at=1.,ended_at=2.+i,per_wake_max=None,usage_scope='per_wake',absolute_ceiling=200000,calibration=False,sdk_version='t',uncapped=True)
    s.wake('mail')
    assert s.state['session_wakes']==3 and s.state['rotate_due'] is True
