import json

import pytest

from sniper_office import host_guard as G, launcher_runtime as LR, models as M


def cgroup(tmp_path,name='office.slice',cpu='200000 100000',memory='4294967296',pids='256'):
    d=tmp_path/name;d.mkdir(exist_ok=True)
    (d/'cpu.max').write_text(cpu);(d/'memory.max').write_text(memory);(d/'pids.max').write_text(pids)
    return d


def test_caps_must_match_exactly_and_the_expected_values_are_configurable(tmp_path):
    assert G.caps_applied(cgroup(tmp_path))
    for n,change in enumerate((dict(cpu='max 100000'),dict(cpu='400000 100000'),dict(memory='max'),dict(pids='512'),dict(name='other.slice'))):
        folder=tmp_path/('case%d'%n);folder.mkdir()
        assert not G.caps_applied(cgroup(folder,**change)),change
    big=cgroup(tmp_path,name='agents.slice',cpu='800000 100000',memory='8589934592',pids='512')
    assert not G.caps_applied(big) and G.caps_applied(big,dict(slice='agents.slice',cpu_cores=8,memory_max=8589934592,pids_max=512))
    assert not G.caps_applied(tmp_path/'missing'/'office.slice')


def test_guard_is_ready_only_when_fresh_and_well_formed(tmp_path):
    path=tmp_path/'guard.json'
    assert G.external_guard(path,now=10.)=='guard_unknown'
    path.write_text(json.dumps(dict(state='ready',reason=None,at=8.)));assert G.external_guard(path,now=10.) is None
    assert G.external_guard(path,now=20.)=='guard_stale' and G.external_guard(path,now=7.)=='guard_stale'
    path.write_text(json.dumps(dict(state='paused',reason='cpu_pressure',at=9.)));assert G.external_guard(path,now=10.)=='cpu_pressure'
    for bad in ('{}','[]','not json',json.dumps(dict(state='ready',reason='x',at=9.)),json.dumps(dict(state='ready',reason=None,at='9'))):
        path.write_text(bad);assert G.external_guard(path,now=10.)=='guard_unknown'


def test_launcher_refuses_until_its_host_layout_is_set_in_code(tmp_path,monkeypatch):
    for name in ('CONTROL_ROOT','SUPERVISOR_UNIT','SITE_PACKAGES'):monkeypatch.setattr(LR,name,None)
    with pytest.raises(ValueError,match='launcher_layout_unconfigured'):LR.build_auth(tmp_path/'c.json',expected_sha256='0'*64,clock=lambda:1.,alert=lambda *a:None)
    with pytest.raises(ValueError,match='launcher_layout'):LR.configure(control_root='relative',supervisor_unit='/u',site_packages='/s')


def test_a_model_call_with_no_transport_is_refused_never_sent_to_a_default(monkeypatch):
    monkeypatch.setattr(M,'TRANSPORTS',{})
    got=M.call('claude','claude-x','s','u',deadline_s=2,clock=lambda:10.)
    assert got.outcome=='refused' and got.error_reason=='configuration'
    sent=[];ok=M.call('claude','claude-x','s','u',deadline_s=2,clock=lambda:10.,transport=lambda *a,**k:sent.append(k) or {'text':'{}','model_echo':'claude-x-20260101'})
    assert ok.outcome=='success' and sent and M.call('claude','claude-x','s','u',deadline_s=2,clock=lambda:10.,transport=lambda *a,**k:{'text':'{}','model_echo':'claude-xl'}).error_reason=='model_mismatch'
