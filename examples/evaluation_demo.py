"""A whole evaluation, end to end, with a made-up team. No model, no network.

    python3 examples/evaluation_demo.py

The "team" answers questions and is right about 90% of the time. The baseline always gives the most common answer and is right about 40% of
the time. One point per right answer. The demo freezes the team's configuration, logs 120 questions over 12 batches, grades them in a separate
evaluation file, computes the verdict, does it a second time with the identical configuration, and files the promotion ticket.
Lower the team's 0.9 towards 0.8 and the replication starts to fail on many runs: the second attempt is judged at a third of the significance.
The config directory here is an ordinary temp folder, so its custody is unproven; the design says it does not require the proof."""
import hashlib
import json
import os
from pathlib import Path
import random
import tempfile
import uuid

from sniper_office import evaluation as EV, launcher_policy as LP, memory_store as MS, roster as R, store as S

DESIGN = dict(estimand='points per question', unit='points', delta0=0.15, delta1=0.4, cluster_key='batch', min_clusters=12, min_cases=120,
              max_censored_share=0.1, min_positive_clusters=8, top3_share_limit=0.5, family_alpha=0.05, k_max=10, looks=['single'],
              counters=['altered_records'], require_readonly_config=False, release_fields=['cases', 'graded', 'censored', 'team_total', 'baseline_total', 'fixed_cost'])
LIMITS = dict(evaluated_epoch_allowed=True, fixed_seats=[], org_size_ceiling=8, head_forbidden_changes=[], material_changes=['hire', 'fire', 'prompt', 'model'], immaterial_changes=[])


class QuizProject:
    """The project side: what a case is worth for the team and for the baseline."""
    def __init__(self, seed):
        self.rng = random.Random(seed);self.answers = {}

    def _right(self, case_id, arm, chance):
        return self.answers.setdefault((case_id, arm), self.rng.random() < chance)

    def team_score(self, case, look_at):return 'observed', 1. if self._right(case['case_id'], 'team', .9) else 0.
    def baseline_score(self, case, look_at):return 'observed', 1. if self._right(case['case_id'], 'base', .4) else 0.
    def fixed_cost(self, epoch_id, look_at):return 3.                 # three points' worth of running cost, charged to the team
    def counters(self, epoch_id):return dict(altered_records=0)


class Clock:
    now = 1_000_000.
    def __call__(self):return self.now


def write_config(root, conn, clock):
    """Design, one prompt per agent, and a sealed memory snapshot: together, the nominee."""
    refs = {}
    def put(key, name, raw):
        path = root / name;path.parent.mkdir(parents=True, exist_ok=True);path.write_bytes(raw);path.chmod(0o444)
        refs[key] = dict(path=name, sha256=hashlib.sha256(raw).hexdigest());return refs[key]['sha256']
    prompts, models = {}, {}
    for aid, payload in conn.execute('SELECT id,payload_json FROM agents').fetchall():
        agent = json.loads(payload)
        prompts[aid] = put('prompt:' + aid, 'prompts/%s.txt' % aid, ('You answer questions. Agent %s.' % aid).encode())
        models[aid] = {k: agent[k] for k in ('model', 'provider', 'family')}
        agent['prompt_hash'] = prompts[aid]
        conn.execute('UPDATE agents SET payload_json=? WHERE id=?', (S.canonical_json(agent).decode(), aid))
    conn.commit()
    snapshot = MS.seal(MS.OfficeMemory(conn), epoch_id='demo-memory', cut=clock() - 1)
    return dict(design_sha256=put('design', 'design.json', S.canonical_json(DESIGN)), memory_snapshot_id=snapshot, prompt_hashes=prompts, model_ids=models), refs


def launcher(root, clock):
    """Stands in for the trusted host launcher: a fresh one-use authority per call."""
    def policy(by):
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY);info = os.fstat(fd)
        return dict(schema='office-launcher-policy-v2', principal=by, office_uid=os.geteuid() + 1, office_gids=[],
                    approved_design_sha256=hashlib.sha256((root / 'design.json').read_bytes()).hexdigest(), approval_origin='launcher_config',
                    approval_ref=dict(commit='0' * 40, path='config/design.json', approver='first-reviewer', second_approver='second-reviewer'),
                    config_root_fd=fd, config_root_dev_ino=[info.st_dev, info.st_ino], config_root_readonly_proof=dict(mount_ro=False, source='mountinfo', checked_at=clock()),
                    issued_at=clock(), expires_at=clock() + 60., nonce=uuid.uuid4().hex)
    return policy


def run_epoch(office, private, project, clock, root, nominee, refs, label):
    eid = R.open_evaluated_epoch(office, LIMITS, clock=clock, hypothesis_id='quiz-team-v1', by='crew', reason=label, launcher_check=lambda who: who == 'crew',
                                 nominee=nominee, nominee_refs=refs, config_root=root, launcher_policy=launcher(root, clock), probe_write=lambda rel: False,
                                 family_alpha=DESIGN['family_alpha'], k_max=DESIGN['k_max'], require_readonly=False)
    for n in range(120):                                             # the host logs every question before anyone answers it
        clock.now += 60
        EV.log_case(office, epoch_id=eid, case_id='%s-q%02d' % (eid, n), eligible_at=clock(), cluster='batch%d' % (n // 10), source='quiz-feed', now=clock())
    clock.now += 3600
    R.verify_nominee(office, epoch_id=eid, config_root=root, clock=clock, launcher_policy=launcher(root, clock), probe_write=lambda rel: False, require_readonly=False)
    result = EV.look(office, private, project, design=DESIGN, epoch_id=eid, look_id='single', look_at=clock(), custody='proven')
    digest = EV.release(office, design=DESIGN, result=result, released_at=clock())
    print('%s: attempt %d at significance %.4f -> %s' % (label, result['k'], result['alpha_k'], result['state']))
    print('  team %.0f, baseline %.0f, cost %.0f over %d questions; lift %.3f per question, lower bound %.3f (needs above %.2f); custody %s'
          % (result['team_total'], result['baseline_total'], result['fixed_cost'], result['graded'], result['lift_mean'], result['lift_lower'], DESIGN['delta0'], result['custody']))
    return digest, result


def main():
    LP.configure(approval_path='config/design.json', approvers=('first-reviewer', 'second-reviewer'))
    work = Path(tempfile.mkdtemp());root = work / 'config';root.mkdir();clock = Clock()
    office = S.open_store(work / 'office.db');private = S.open_store(work / 'evaluation.db', evaluation=True)
    for aid in ('ada', 'bo'):
        office.execute('INSERT INTO agents VALUES(?,?)', (aid, json.dumps(dict(agent_id=aid, role='Researcher', state='active', generation=1, model='demo-model',
                                                                                provider='demo', family='claude', prompt_hash='pending', policy_hash='p'))))
    office.commit()
    nominee, refs = write_config(root, office, clock)
    project = QuizProject(seed=7)
    run_epoch(office, private, project, clock, root, nominee, refs, 'first epoch')
    clock.now += 86400
    digest, result = run_epoch(office, private, project, clock, root, nominee, refs, 'replication')
    if result['state'] != 'pass':
        print('no promotion request: the verdict is', result['state']);return
    ticket = EV.request_promotion(office, release_hash=digest, author='grader', done_when='a human approves or declines this configuration', guard=lambda: None)
    print('promotion ticket filed:', json.loads(office.execute('SELECT payload_json FROM tickets WHERE id=?', (ticket,)).fetchone()[0])['problem'])
    print('trial registry:', [json.loads(p)['status'] for (p,) in office.execute('SELECT payload_json FROM trial_registry ORDER BY seq')])
    print('public store holds no per-question result:', office.execute("SELECT count(*) FROM sqlite_master WHERE name='grades'").fetchone()[0] == 0)
    office.close();private.close()


if __name__ == '__main__':
    main()
