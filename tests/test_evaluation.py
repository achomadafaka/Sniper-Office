"""The verdict procedure: what it refuses to read, what it calls a pass, and what a pass may do."""
import json
import math

import pytest

from eval_fixture import APPROVAL, DESIGN, Launcher, open_epoch
from sniper_office import evaluation as EV, launcher_policy as LP, store as S

LIM = dict(evaluated_epoch_allowed=True, fixed_seats=[], org_size_ceiling=24, head_forbidden_changes=[], material_changes=['hire'], immaterial_changes=[])


class Project:
    """Team scores `team` per case, the baseline `base`; case ids listed in `censored` or `pending` say so."""
    def __init__(self, team=3., base=1., cost=0., noise=None):
        self.team, self.base, self.cost, self.noise = team, base, cost, noise or (lambda case: 0.)
        self.censored, self.pending, self.counts = set(), set(), dict(altered_records=0, future_reads=0)

    def _state(self, case):
        return 'pending' if case['case_id'] in self.pending else 'censored' if case['case_id'] in self.censored else 'observed'

    def team_score(self, case, look_at):return self._state(case), self.team + self.noise(case)
    def baseline_score(self, case, look_at):return self._state(case), self.base
    def fixed_cost(self, epoch_id, look_at):return self.cost
    def counters(self, epoch_id):return dict(self.counts)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(LP, 'APPROVAL', APPROVAL);monkeypatch.setattr(LP, 'readonly_mount', lambda fd: True)
    office = S.open_store(tmp_path / 'office.db');private = S.open_store(tmp_path / 'evaluation.db', evaluation=True)
    t = [1000.];clock = lambda: t[0];launcher = Launcher(tmp_path / 'config', clock)
    yield office, private, clock, t, launcher, tmp_path / 'config'
    launcher.close();office.close();private.close()


def run_epoch(env, project, *, n=30, days=6, hypothesis='H1', same_as=None):
    office, private, clock, t, launcher, root = env
    eid = open_epoch(office, LIM, clock, root, launcher, hypothesis_id=hypothesis, same_as=same_as)
    for i in range(n):
        t[0] += 1
        EV.log_case(office, epoch_id=eid, case_id='c%d' % i, eligible_at=t[0], cluster='day%d' % (i % days), source='feed', now=t[0])
    t[0] += 10
    return eid


def do_look(env, project, eid, custody='proven'):
    office, private, clock, t, launcher, root = env
    return EV.look(office, private, project, design=DESIGN, epoch_id=eid, look_id='single', look_at=clock(), custody=custody)


@pytest.mark.parametrize('p,df,expected', [(.975, 10, 2.2281), (.95, 4, 2.1318), (.999, 5, 5.8934), (.975, 1, 12.7062), (.95, 120, 1.6577), (.99975, 9, 5.2907)])
def test_t_quantile_matches_published_tables(p, df, expected):
    assert EV.t_quantile(p, df) == pytest.approx(expected, abs=2e-3)


def test_lower_bound_counts_clusters_not_cases():
    values = [2., 2.2, 1.8, 2.1, 1.9, 2.0] * 5
    spread = [str(i % 6) for i in range(30)];lumped = [str(i // 15) for i in range(30)]
    mean, many = EV.lower_bound(values, spread, .05);_, few = EV.lower_bound([v + (1 if i < 15 else -1) for i, v in enumerate(values)], lumped, .05)
    assert mean == pytest.approx(2.) and 1.8 < many < 2. and few < 0          # two clusters that disagree prove almost nothing
    assert EV.lower_bound(values, ['one'] * 30, .05) == (pytest.approx(2.), None) and EV.lower_bound([], [], .05) == (None, None)


def test_a_clear_effect_passes_pending_replication_and_a_second_separate_epoch_makes_it_a_pass(env):
    office, private, clock, t, launcher, root = env
    project = Project(noise=lambda case: (hash(case['case_id']) % 7 - 3) / 10.)
    first = do_look(env, project, run_epoch(env, project))
    assert first['state'] == 'pass_pending_replication' and first['lift_lower'] > DESIGN['delta0'] and first['k'] == 1
    EV.release(office, design=DESIGN, result=first, released_at=clock())
    fresh = do_look(env, project, run_epoch(env, project))            # a new snapshot is a different nominee: it does not replicate the first
    assert fresh['state'] == 'pass_pending_replication' and fresh['nominee_config_hash'] != first['nominee_config_hash']
    EV.release(office, design=DESIGN, result=fresh, released_at=clock())
    frozen = json.loads(office.execute('SELECT payload_json FROM epochs WHERE id=?', (first['epoch_id'],)).fetchone()[0])
    second = do_look(env, project, run_epoch(env, project, same_as=frozen))
    assert second['state'] == 'pass' and second['k'] == 3 and second['alpha_k'] < first['alpha_k'] and second['nominee_config_hash'] == first['nominee_config_hash']
    digest = EV.release(office, design=DESIGN, result=second, released_at=clock())
    statuses = [json.loads(p)['status'] for (p,) in office.execute('SELECT payload_json FROM trial_registry ORDER BY seq')]
    assert statuses == ['RUNNING', 'PASS_PENDING_REPLICATION'] * 2 + ['RUNNING', 'PASS']
    ticket = EV.request_promotion(office, release_hash=digest, author='grader', done_when='a human approves or declines', guard=lambda: None)
    stored = json.loads(office.execute('SELECT payload_json FROM tickets WHERE id=?', (ticket,)).fetchone()[0])
    assert stored['state'] == 'proposed' and stored['evidence_refs'] == ['sealed_views:' + digest] and stored['untrusted_office_request'] is True


@pytest.mark.parametrize('setup,state,reason', [
    (dict(team=1.2, base=1.), 'fail', 'lift_lower_bound_not_above_delta0'),
    (dict(team=3., base=1., cost=80.), 'fail', 'net_lower_bound_not_above_delta0'),
    (dict(team=3., base=1., noise=lambda c: 400. if c['case_id'] in ('c0', 'c1', 'c2') else 0.), 'fail', 'result_concentrated_in_three_cases'),
])
def test_small_costly_or_concentrated_results_fail(env, setup, state, reason):
    project = Project(**setup)
    result = do_look(env, project, run_epoch(env, project))
    assert result['state'] == state and reason in result['reasons']


def test_dirty_counters_unknown_counters_and_heavy_censoring_give_no_verdict_never_a_fail(env):
    office, private, clock, t, launcher, root = env
    project = Project();project.counts['future_reads'] = 1
    result = do_look(env, project, run_epoch(env, project))
    assert result['state'] == 'no_verdict' and 'counter:future_reads:1' in result['reasons'] and 'net_lower' not in result
    EV.release(office, design=DESIGN, result=result, released_at=clock())
    unknown = Project();unknown.counts['altered_records'] = None;unknown.censored = {'c%d' % i for i in range(10)}
    result = do_look(env, unknown, run_epoch(env, unknown))
    assert result['state'] == 'no_verdict' and {'counter:altered_records:unknown', 'too_much_censored'} <= set(result['reasons']) and result['censored'] == 10


def test_unproven_custody_cannot_be_a_plain_pass(env):
    project = Project()
    assert do_look(env, project, run_epoch(env, project), custody='unproven')['state'] == 'pass_unproven_custody'


def test_an_early_look_is_refused_reads_nothing_and_spends_nothing(env):
    office, private, clock, t, launcher, root = env
    project = Project()
    eid = run_epoch(env, project, n=10)
    with pytest.raises(EV.LookRefused, match='plan_not_reached'):do_look(env, project, eid)
    for i in range(10, 30):
        EV.log_case(office, epoch_id=eid, case_id='c%d' % i, eligible_at=t[0], cluster='day%d' % (i % 6), source='feed', now=t[0])
    project.pending = {'c29'}
    with pytest.raises(EV.LookRefused, match='cases_pending'):do_look(env, project, eid)
    with pytest.raises(EV.LookRefused, match='look_not_scheduled'):EV.look(office, private, project, design=DESIGN, epoch_id=eid, look_id='peek', look_at=clock(), custody='proven')
    assert private.execute('SELECT count(*) FROM grades').fetchone()[0] == 0
    assert [json.loads(p)['status'] for (p,) in office.execute('SELECT payload_json FROM trial_registry')] == ['RUNNING']
    project.pending = set()
    assert do_look(env, project, eid)['state'] == 'pass_pending_replication'


def test_grading_twice_adds_nothing_and_a_broken_scorer_stops_the_grade(env):
    office, private, clock, t, launcher, root = env
    project = Project();eid = run_epoch(env, project)
    assert EV.grade_epoch(office, private, project, epoch_id=eid, look_id='single', look_at=clock())['observed'] == 30
    assert EV.grade_epoch(office, private, project, epoch_id=eid, look_id='single', look_at=clock()) == dict(observed=0, censored=0, pending=0)
    assert private.execute('SELECT count(*) FROM grades').fetchone()[0] == 30 and S.verify_chain(private, 'grades') == (True, None)
    broken = Project();broken.team_score = lambda case, look_at: ('observed', math.nan)
    with pytest.raises(ValueError, match='score_invalid'):EV.grade_epoch(office, private, broken, epoch_id=eid, look_id='other', look_at=clock())


def test_cases_keep_their_first_time_and_cannot_come_from_the_future_or_another_epoch(env):
    office, private, clock, t, launcher, root = env
    eid = run_epoch(env, Project(), n=1)
    assert EV.log_case(office, epoch_id=eid, case_id='c0', eligible_at=clock(), cluster='dayX', source='feed', now=clock()) is None
    assert EV.cases(office, eid)[0]['cluster'] == 'day0'
    for bad in (dict(eligible_at=clock() + 1), dict(epoch_id='e999'), dict(eligible_at=1.), dict(case_id='')):
        with pytest.raises(ValueError):EV.log_case(office, **{**dict(epoch_id=eid, case_id='late', eligible_at=clock(), cluster='d', source='feed', now=clock()), **bad})
    EV.record_gap(office, epoch_id=eid, source='feed', from_at=clock() - 5, to_at=clock(), now=clock())
    with pytest.raises(ValueError):EV.record_gap(office, epoch_id=eid, source='feed', from_at=clock(), to_at=clock() - 1, now=clock())


def test_release_publishes_aggregates_only_closes_the_epoch_and_only_a_pass_can_ask_for_promotion(env):
    office, private, clock, t, launcher, root = env
    project = Project();eid = run_epoch(env, project);result = do_look(env, project, eid)
    digest = EV.release(office, design=dict(DESIGN, release_fields=['cases', 'graded']), result=result, released_at=clock())
    body = json.loads(office.execute('SELECT payload_json FROM sealed_views WHERE row_hash=?', (digest,)).fetchone()[0])
    assert body['cases'] == 30 and 'team_total' not in body and 'grades' not in body and body['state'] == 'pass_pending_replication'
    assert json.loads(office.execute('SELECT payload_json FROM epochs WHERE id=?', (eid,)).fetchone()[0])['status'] == 'completed'
    with pytest.raises(ValueError, match='release_epoch_mismatch'):EV.release(office, design=DESIGN, result=result, released_at=clock())
    with pytest.raises(ValueError, match='promotion_needs_a_pass'):EV.request_promotion(office, release_hash=digest, author='g', done_when='d', guard=lambda: None)
    with pytest.raises(ValueError, match='release_unknown'):EV.request_promotion(office, release_hash='0' * 64, author='g', done_when='d', guard=lambda: None)


@pytest.mark.parametrize('change', [dict(delta1=0.1), dict(min_clusters=1), dict(looks=[]), dict(release_fields=['grades']), dict(extra=1), dict(family_alpha=1.5), dict(require_readonly_config='yes')])
def test_a_design_must_state_every_threshold_sanely(change, tmp_path):
    with pytest.raises(EV.DesignInvalid):EV.check_design({**DESIGN, **change})
    missing = dict(DESIGN);missing.pop('delta0')
    with pytest.raises(EV.DesignInvalid):EV.check_design(missing)
    path = tmp_path / 'd.json';path.write_text(json.dumps(DESIGN))
    with pytest.raises(EV.DesignInvalid):EV.load_design(path, '0' * 64)


def test_a_freeze_without_a_readonly_mount_can_never_look_proven(env):
    office, private, clock, t, launcher, root = env
    project = Project()
    eid = open_epoch(office, LIM, clock, root, launcher, require_readonly=False)
    for i in range(30):
        t[0] += 1;EV.log_case(office, epoch_id=eid, case_id='c%d' % i, eligible_at=t[0], cluster='day%d' % (i % 6), source='feed', now=t[0])
    t[0] += 1
    assert do_look(env, project, eid, custody='proven')['state'] == 'pass_unproven_custody'
