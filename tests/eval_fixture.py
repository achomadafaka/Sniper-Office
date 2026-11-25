"""Synthetic evaluated-epoch inputs: a config directory, a roster, a sealed snapshot and a launcher authority. Not evidence of anything."""
import copy
import hashlib
import json
import os
from pathlib import Path
import uuid

from sniper_office import launcher_policy as LP, memory_store as MS, roster as R, store as S

DESIGN = dict(estimand='mean score per case', unit='points', delta0=0.5, delta1=1.5, cluster_key='day', min_clusters=5, min_cases=20,
              max_censored_share=0.2, min_positive_clusters=3, top3_share_limit=0.5, family_alpha=0.05, k_max=10,
              looks=['single'], counters=['altered_records', 'future_reads'], require_readonly_config=True,
              release_fields=['cases', 'graded', 'censored', 'team_total', 'baseline_total', 'fixed_cost'])
APPROVAL = ('config/design.json', ('reviewer-one', 'reviewer-two'))


def build(conn, root, *, now=1000., design=None):
    root = Path(root);root.mkdir(parents=True, exist_ok=True);root.chmod(0o755)
    refs = {}

    def put(key, name, doc):
        path = root / name;path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():path.chmod(0o644)
        raw = doc.encode() if isinstance(doc, str) else S.canonical_json(doc)
        path.write_bytes(raw);path.chmod(0o444)
        refs[key] = dict(path=name, sha256=hashlib.sha256(raw).hexdigest())
        return refs[key]['sha256']

    if not conn.execute('SELECT 1 FROM agents').fetchone():
        a = dict(agent_id='a', role='Researcher', state='active', generation=1, model='fixture-model', provider='fixture', family='gpt', prompt_hash='pending', policy_hash='p')
        conn.execute('INSERT INTO agents VALUES(?,?)', ('a', S.canonical_json(a).decode()));conn.commit()
    prompts, models = {}, {}
    for aid, payload in conn.execute('SELECT id,payload_json FROM agents').fetchall():
        a = json.loads(payload)
        if a['state'] != 'active':continue
        prompts[aid] = put('prompt:' + aid, 'prompts/' + aid + '.txt', 'fixture prompt ' + aid)
        models[aid] = {key: a[key] for key in ('model', 'provider', 'family')}
        a['prompt_hash'] = prompts[aid]
        conn.execute('UPDATE agents SET payload_json=? WHERE id=?', (S.canonical_json(a).decode(), aid))
    conn.commit()
    snapshot = MS.seal(MS.OfficeMemory(conn), epoch_id='fixture-memory', cut=now - 1)
    digest = put('design', 'design.json', design or DESIGN)
    return dict(design_sha256=digest, memory_snapshot_id=snapshot, prompt_hashes=prompts, model_ids=models), refs


class Launcher:
    """Stands in for the trusted host launcher."""
    def __init__(self, root, clock):
        self.root, self.clock, self.fds, self.probes, self.pinned = Path(root), clock, [], [], None

    def fresh(self, by='crew'):
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY);self.fds.append(fd)
        info = os.fstat(fd);now = self.clock()
        return dict(schema='office-launcher-policy-v2', principal=by, office_uid=os.geteuid() + 1, office_gids=[],
                    approved_design_sha256=hashlib.sha256((self.root / 'design.json').read_bytes()).hexdigest(),
                    approval_origin='launcher_config', approval_ref=dict(commit='a' * 40, path=APPROVAL[0], approver=APPROVAL[1][0], second_approver=APPROVAL[1][1]),
                    config_root_fd=fd, config_root_dev_ino=[info.st_dev, info.st_ino],
                    config_root_readonly_proof=dict(mount_ro=True, source='mountinfo', checked_at=now),
                    issued_at=now, expires_at=now + 60., nonce=uuid.uuid4().hex)

    def policy(self, by):
        return copy.deepcopy(self.pinned) if self.pinned is not None else self.fresh(by)

    def probe(self, relative):
        self.probes.append(relative);return False

    def close(self):
        for fd in self.fds:os.close(fd)
        self.fds.clear()


def open_epoch(conn, lim, clock, root, launcher, same_as=None, **kw):
    """same_as: an earlier epoch row whose exact nominee is frozen again (a replication). Otherwise a fresh nominee with a new snapshot."""
    nominee, refs = (same_as['nominee'], same_as['nominee_refs']) if same_as else build(conn, root, now=clock())
    return R.open_evaluated_epoch(conn, lim, clock=clock, hypothesis_id=kw.pop('hypothesis_id', 'H1'), by='crew', reason='fixture freeze', launcher_check=lambda who: who == 'crew',
                                  nominee=nominee, nominee_refs=refs, config_root=root, launcher_policy=launcher.policy, probe_write=launcher.probe, **kw)
