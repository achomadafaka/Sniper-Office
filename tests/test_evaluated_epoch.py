"""Freezing a configuration for evaluation: who may, what is checked, and what breaks it."""
import json

import pytest

from eval_fixture import APPROVAL, Launcher, build, open_epoch
from sniper_office import launcher_policy as LP, roster as R, store as S

LIM = dict(evaluated_epoch_allowed=True, fixed_seats=[], org_size_ceiling=24, head_forbidden_changes=['tools'], material_changes=['hire', 'fire', 'prompt', 'model', 'persona'], immaterial_changes=['heartbeat'])


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(LP, 'APPROVAL', APPROVAL)
    monkeypatch.setattr(LP, 'readonly_mount', lambda fd: True)   # the temp directory is not a read-only mount; the real check has its own test below
    conn = S.open_store(tmp_path / 'office.db');t = [1000.];clock = lambda: t[0]
    root = tmp_path / 'config';launcher = Launcher(root, clock)
    yield conn, clock, root, launcher, t
    launcher.close();conn.close()


def rows(conn, table):
    return [json.loads(r[0]) for r in conn.execute('SELECT payload_json FROM ' + table + ' ORDER BY seq')]


def epoch(conn, eid):
    return json.loads(conn.execute('SELECT payload_json FROM epochs WHERE id=?', (eid,)).fetchone()[0])


def test_freeze_derives_the_attempt_number_and_its_significance_and_records_one_running_trial(env):
    conn, clock, root, launcher, t = env
    eid = open_epoch(conn, LIM, clock, root, launcher)
    e = epoch(conn, eid)
    assert e['kind'] == 'evaluated' and e['k'] == 1 and e['alpha_k'] == pytest.approx(0.025) and len(e['nominee_config_hash']) == 64
    assert [(r['k'], r['status'], r['epoch_id']) for r in rows(conn, 'trial_registry')] == [(1, 'RUNNING', eid)]
    assert rows(conn, 'receipts')[-1]['kind'] == 'launcher_policy_use' and set(launcher.probes) >= {'.', 'design.json', 'prompts', 'prompts/a.txt'}


def test_a_caller_cannot_choose_k_or_alpha_and_only_one_evaluated_epoch_is_open(env):
    conn, clock, root, launcher, t = env
    for bad in (dict(k=2), dict(alpha_k=0.05), dict(nominee_config_hash='0' * 64)):
        with pytest.raises(R.OrgRefused):open_epoch(conn, LIM, clock, root, launcher, **bad)
    assert rows(conn, 'trial_registry') == [] and not conn.execute("SELECT 1 FROM epochs").fetchone()
    open_epoch(conn, LIM, clock, root, launcher)
    with pytest.raises(R.OrgRefused, match='evaluated_epoch_open'):open_epoch(conn, LIM, clock, root, launcher, hypothesis_id='H2')


def test_later_attempts_spend_less_and_the_family_is_finite(env):
    conn, clock, root, launcher, t = env
    alphas = []
    for n in range(3):
        eid = open_epoch(conn, LIM, clock, root, launcher, k_max=3)
        alphas.append(epoch(conn, eid)['alpha_k'])
        t[0] += 10;R.fire(conn, LIM, clock=clock, agent_id='a', reason='restaff', by='crew') if False else None
        e = epoch(conn, eid);e['closed_at'] = t[0];conn.execute('UPDATE epochs SET payload_json=? WHERE id=?', (S.canonical_json(e).decode(), eid));conn.commit()
    assert alphas == sorted(alphas, reverse=True) and sum(alphas) < 0.05 and alphas[2] == pytest.approx(0.05 / 12)
    with pytest.raises(R.OrgRefused, match='family_exhausted'):open_epoch(conn, LIM, clock, root, launcher, k_max=3)


def test_only_the_operator_side_through_a_trusted_launcher_with_explicit_permission(env):
    conn, clock, root, launcher, t = env
    nominee, refs = build(conn, root)
    args = dict(clock=clock, hypothesis_id='H1', reason='x', nominee=nominee, nominee_refs=refs, config_root=root, launcher_policy=launcher.policy, probe_write=launcher.probe)
    with pytest.raises(R.OrgRefused):R.open_evaluated_epoch(conn, LIM, by='head', launcher_check=lambda who: True, **args)
    with pytest.raises(R.OrgRefused, match='untrusted_launcher'):R.open_evaluated_epoch(conn, LIM, by='crew', launcher_check=lambda who: 1, **args)
    for lim in (dict(LIM, evaluated_epoch_allowed=False), {k: v for k, v in LIM.items() if k != 'evaluated_epoch_allowed'}, dict(LIM, evaluated_epoch_allowed='yes')):
        with pytest.raises(R.OrgRefused, match='not_allowed'):R.open_evaluated_epoch(conn, lim, by='crew', launcher_check=lambda who: True, **args)
    assert rows(conn, 'trial_registry') == []


@pytest.mark.parametrize('change,why', [
    (lambda p: p.update(nonce=''), 'nonce'), (lambda p: p.update(expires_at=p['issued_at'] + 61), 'expired'), (lambda p: p.update(schema='office-launcher-policy-v1'), 'principal'),
    (lambda p: p['approval_ref'].update(approver='someone-else'), 'approval'), (lambda p: p['approval_ref'].update(path='other.json'), 'approval'),
    (lambda p: p['config_root_readonly_proof'].update(mount_ro=False), 'readonly'), (lambda p: p.update(config_root_dev_ino=[1, 2]), 'identity'), (lambda p: p.update(extra=1), 'schema')])
def test_a_malformed_unapproved_or_expired_authority_refuses_and_writes_nothing(env, change, why):
    conn, clock, root, launcher, t = env
    build(conn, root);launcher.pinned = launcher.fresh();change(launcher.pinned)
    with pytest.raises(R.OrgRefused, match=why):open_epoch(conn, LIM, clock, root, launcher)
    assert rows(conn, 'trial_registry') == [] and rows(conn, 'receipts') == []


def test_no_approvers_configured_a_writable_answer_or_a_real_writable_mount_each_refuse(env, monkeypatch):
    conn, clock, root, launcher, t = env
    launcher.probe = lambda relative: True
    with pytest.raises(R.OrgRefused, match='probe_not_false'):open_epoch(conn, LIM, clock, root, launcher)
    launcher.probe = lambda relative: False
    monkeypatch.undo();monkeypatch.setattr(LP, 'APPROVAL', APPROVAL)
    with pytest.raises(R.OrgRefused, match='config_root_not_readonly'):open_epoch(conn, LIM, clock, root, launcher)   # the real mount check: a temp directory is writable
    monkeypatch.setattr(LP, 'APPROVAL', None)
    with pytest.raises(R.OrgRefused, match='unconfigured'):open_epoch(conn, LIM, clock, root, launcher)
    with pytest.raises(ValueError):LP.configure(approval_path='d.json', approvers=('same', 'same'))


def test_an_authority_is_spent_once(env):
    conn, clock, root, launcher, t = env
    build(conn, root);launcher.pinned = launcher.fresh()
    eid = open_epoch(conn, LIM, clock, root, launcher)
    with pytest.raises(R.OrgRefused, match='nonce_replayed'):
        R.verify_nominee(conn, epoch_id=eid, config_root=root, clock=clock, launcher_policy=launcher.policy, probe_write=launcher.probe)
    launcher.pinned = None
    assert R.verify_nominee(conn, epoch_id=eid, config_root=root, clock=clock, launcher_policy=launcher.policy, probe_write=launcher.probe) == epoch(conn, eid)['nominee_config_hash']


@pytest.mark.parametrize('tamper', ['prompt_file', 'design_file', 'roster_model', 'snapshot'])
def test_a_changed_nominee_spends_the_trial_as_broken(env, tamper):
    conn, clock, root, launcher, t = env
    eid = open_epoch(conn, LIM, clock, root, launcher)
    if tamper == 'prompt_file':
        f = root / 'prompts/a.txt';f.chmod(0o644);f.write_text('edited');f.chmod(0o444)
    elif tamper == 'design_file':
        f = root / 'design.json';f.chmod(0o644);f.write_text('{}');f.chmod(0o444)
    elif tamper == 'roster_model':
        a = json.loads(conn.execute("SELECT payload_json FROM agents WHERE id='a'").fetchone()[0]);a['model'] = 'another-model'
        conn.execute("UPDATE agents SET payload_json=? WHERE id='a'", (S.canonical_json(a).decode(),));conn.commit()
    else:
        conn.execute('DROP TRIGGER memory_snapshots_no_delete');conn.execute('DELETE FROM memory_snapshots');conn.commit()
    with pytest.raises(R.OrgRefused):
        R.verify_nominee(conn, epoch_id=eid, config_root=root, clock=clock, launcher_policy=launcher.policy, probe_write=launcher.probe)
    if tamper != 'design_file':     # an unapproved design is refused by the authority itself, before the epoch is touched
        assert epoch(conn, eid)['status'] == 'broken' and [r['status'] for r in rows(conn, 'trial_registry')] == ['RUNNING', 'BROKEN']


def test_a_material_change_inside_an_evaluated_epoch_breaks_it_and_keeps_the_attempt_spent(env):
    conn, clock, root, launcher, t = env
    eid = open_epoch(conn, LIM, clock, root, launcher);t[0] += 5
    R.hire(conn, LIM, clock=clock, role='Reviewer', pod='pod1', provider='fixture', model='m', family='claude', idempotency_key='k1',
           persona_fn=lambda role, pod: dict(name='N', personality='plain', backstory='none'), policy_hash='p' * 64, prompt_hash='q' * 64, by='crew')
    assert epoch(conn, eid)['status'] == 'broken' and [r['status'] for r in rows(conn, 'trial_registry')] == ['RUNNING', 'BROKEN']
    t[0] += 5;nxt = open_epoch(conn, LIM, clock, root, launcher, hypothesis_id='H1')
    assert epoch(conn, nxt)['k'] == 2


def test_a_project_rule_can_refuse_the_nominee(env):
    conn, clock, root, launcher, t = env
    def check(nominee, docs, *, k, opened_at):
        if docs['design']['delta1'] <= docs['design']['delta0'] * 10:raise ValueError('project_rule_failed')
    with pytest.raises(R.OrgRefused, match='project_rule_failed'):open_epoch(conn, LIM, clock, root, launcher, check=check)
    assert rows(conn, 'trial_registry') == []


def test_without_a_readonly_mount_the_freeze_is_allowed_only_when_asked_and_is_labelled_unproven(env, monkeypatch):
    conn, clock, root, launcher, t = env
    monkeypatch.setattr(LP, 'readonly_mount', lambda fd: False)
    with pytest.raises(R.OrgRefused, match='config_root_not_readonly'):open_epoch(conn, LIM, clock, root, launcher)
    eid = open_epoch(conn, LIM, clock, root, launcher, require_readonly=False)
    assert epoch(conn, eid)['config_custody'] == 'unproven' and rows(conn, 'receipts')[-1]['config_custody'] == 'unproven'
    with pytest.raises(R.OrgRefused):R.verify_nominee(conn, epoch_id=eid, config_root=root, clock=clock, launcher_policy=launcher.policy, probe_write=launcher.probe)
    assert R.verify_nominee(conn, epoch_id=eid, config_root=root, clock=clock, launcher_policy=launcher.policy, probe_write=launcher.probe, require_readonly=False)
