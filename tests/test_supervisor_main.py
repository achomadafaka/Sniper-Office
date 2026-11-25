"""The supervisor process entry point, driven with an injected runtime."""
import json
import os
from pathlib import Path

import pytest

from sniper_office import store, supervisor as S
from sniper_office.supervisor_main import RuntimeRefused, main
from test_supervisor import Clock, make, office   # noqa: F401  (office is a fixture)


def test_a_refusing_factory_exits_75_and_a_bad_factory_name_is_refused(capsys):
    def refuse(**kw):raise RuntimeRefused('guard_paused')
    assert main(['--once'],runtime_factory=refuse)==75 and json.loads(capsys.readouterr().out)=={'state':'paused','reason':'guard_paused'}
    assert main(['--once','--runtime-factory','no_such_module_xyz:build'])==2
    with pytest.raises(SystemExit):main(['--once','--runtime-factory','os;import x:build'])
    with pytest.raises(SystemExit):main(['--once'])


def test_entrypoint_recovers_expired_running_turns_even_with_no_due_work(office):
    from sniper_office.supervisor_main import main
    conn,clock,lease,gen=office
    clock.now+=101
    def tasks():
        # Arrives after startup reconciliation; exercise the ordinary idle tick.
        S._put(conn,'turn_runs','late-orphan',dict(state='running',created_at=900.,deadline_at=901.,steps=0))
        return []
    runtime={'supervisor':make(office),'deadline_s':1,'max_steps':1,'tasks':tasks}
    assert main(['--once'],runtime_factory=lambda **kw:runtime)==0
    assert S._get(conn,'turn_runs','late-orphan')['state']=='partial'


def test_entrypoint_once_writes_progress_and_releases_lease(office,capsys):
    from sniper_office.supervisor_main import main
    office[1].now+=101
    supervisor=make(office)
    runtime={'supervisor':supervisor,'deadline_s':1,'max_steps':1,
             'tasks':lambda:[{'agent_id':'agent','turn_id':'entrypoint-turn'}]}
    assert main(['--once'],runtime_factory=lambda **kwargs:runtime)==0
    output=json.loads(capsys.readouterr().out)
    assert output['state']=='completed'
    assert office[0].execute("SELECT 1 FROM heartbeats WHERE id='supervisor'").fetchone()
    start=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19]
    assert office[0].execute('SELECT holder FROM supervisor_lease').fetchone()[0]==f'office-{os.getpid()}-{start}'
    assert S.Lease(office[0],clock=office[1]).acquire('restart')>office[3]


def test_entrypoint_initializes_under_lease_then_pauses_on_unready_host(office,capsys):
    from sniper_office.supervisor_main import main
    office[1].now+=101
    supervisor=make(office);seen=[]
    def tasks():raise ValueError('caps_unapplied')
    runtime={'supervisor':supervisor,'deadline_s':1,'max_steps':1,'tasks':tasks,
             'initialize':lambda gen:seen.append(gen)}
    assert main(['--once'],runtime_factory=lambda **kwargs:runtime)==0
    assert len(seen)==1 and seen[0]>office[3]
    assert json.loads(capsys.readouterr().out.splitlines()[0])=={'state':'paused','reason':'caps_unapplied'}
    assert office[0].execute("SELECT 1 FROM heartbeats WHERE id='supervisor'").fetchone() is None


def test_clock_loss_pauses_without_raising(office):
    from sniper_office.supervisor_main import main
    office[1].now+=101
    supervisor=make(office)
    def tasks():
        office[1].now=None
        raise ValueError('clock_unknown')
    runtime={'supervisor':supervisor,'deadline_s':1,'max_steps':1,'tasks':tasks}
    assert main(['--once'],runtime_factory=lambda **kwargs:runtime)==0


def test_entrypoint_flushes_after_turn_and_exposes_health_before_close(office):
    from sniper_office.supervisor_main import main
    office[1].now+=101
    events=[]
    supervisor=make(office,lambda *a:(events.append('model') or S.Step(done=True)))
    runtime={'supervisor':supervisor,'deadline_s':1,'max_steps':1,
             'tasks':lambda:[{'agent_id':'agent','turn_id':'observed-turn'}],
             'flush_observations':lambda:events.append('flush'),
             'observation_health':lambda:{'state':'observed_only','pending_n':0},
             'close':lambda:events.append('close')}
    assert main(['--once'],runtime_factory=lambda **kw:runtime)==0
    assert events==['model','flush','flush','close']
    heartbeat=json.loads(office[0].execute("SELECT payload_json FROM heartbeats WHERE id='supervisor'").fetchone()[0])
    assert heartbeat['observations']['pending_n']==0


def test_entrypoint_clock_loss_during_expiry_recovery_pauses_without_writes(office,capsys):
    from sniper_office.supervisor_main import main
    conn,clock,lease,gen=office
    clock.now+=101
    S._put(conn,'turn_runs','unknown',dict(state='running',created_at=800.,steps=0))
    def tasks():
        # Lose clock after startup recovery/readiness, before idle reconciliation.
        S._put(conn,'turn_runs','unknown',dict(state='running',created_at=800.,steps=0))
        clock.now=None
        return []
    runtime={'supervisor':make(office),'deadline_s':1,'max_steps':1,'tasks':tasks}
    assert main(['--once'],runtime_factory=lambda **kw:runtime)==0
    assert json.loads(capsys.readouterr().out)=={'state':'paused','reason':'clock_unknown'}
    assert S._get(conn,'turn_runs','unknown')['state']=='running'
    assert conn.execute("SELECT 1 FROM heartbeats WHERE id='supervisor'").fetchone() is None
