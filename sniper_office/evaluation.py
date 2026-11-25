"""Evaluation: is one frozen configuration better than a fixed baseline, by enough to act on? See docs/EVALUATION.md.

The package owns the procedure; a project owns the meaning. The project object has four methods, called only by the grader process:

    team_score(case, look_at)     -> (state, value)   state is 'observed', 'censored' or 'pending'; a skipped case scores what skipping scores
    baseline_score(case, look_at) -> (state, value)   the frozen rule with no model in it, on the same case
    fixed_cost(epoch_id, look_at) -> number           charged to the team, never to the baseline
    counters(epoch_id)            -> {name: count or None}   None is unknown and blocks a verdict like a positive count

Cases are logged by a host process with log_case() before any agent sees them, so skipped and never-seen cases stay in the denominator.
Grades live in the evaluation file (store.open_store(path, evaluation=True)), which the supervisor never opens. Only release() writes
results to the public store, and only aggregate fields the design names. A pass files a ticket for a human; nothing here applies it."""
import hashlib
import json
import math

from . import store as S, tickets as T

DESIGN_KEYS = frozenset({'estimand', 'unit', 'delta0', 'delta1', 'cluster_key', 'min_clusters', 'min_cases', 'max_censored_share', 'min_positive_clusters',
                         'top3_share_limit', 'family_alpha', 'k_max', 'looks', 'counters', 'require_readonly_config', 'release_fields'})
CORE_FIELDS = ('cases', 'graded', 'censored', 'team_total', 'baseline_total', 'fixed_cost')
STATES = ('observed', 'censored', 'pending')


class DesignInvalid(ValueError):
    pass


class LookRefused(ValueError):
    """Nothing was read and nothing was spent."""


def _finite(v):
    return type(v) in (int, float) and math.isfinite(v)


def check_design(d):
    """Every threshold is stated; code carries no default for any of them."""
    if not isinstance(d, dict) or set(d) != DESIGN_KEYS:
        raise DesignInvalid('design_keys')
    if (not all(isinstance(d[k], str) and d[k] for k in ('estimand', 'unit', 'cluster_key'))
            or not all(_finite(d[k]) for k in ('delta0', 'delta1', 'max_censored_share', 'top3_share_limit', 'family_alpha'))
            or d['delta1'] <= d['delta0'] or not 0 <= d['max_censored_share'] < 1 or not 0 < d['top3_share_limit'] <= 1 or not 0 < d['family_alpha'] < 1
            or not all(type(d[k]) is int and d[k] >= 1 for k in ('min_clusters', 'min_cases', 'k_max')) or d['min_clusters'] < 2
            or type(d['min_positive_clusters']) is not int or not 0 <= d['min_positive_clusters'] <= d['min_clusters']
            or not isinstance(d['looks'], list) or not d['looks'] or not all(isinstance(x, str) and x for x in d['looks']) or len(set(d['looks'])) != len(d['looks'])
            or not isinstance(d['counters'], list) or not all(isinstance(x, str) and x for x in d['counters'])
            or type(d['require_readonly_config']) is not bool
            or not isinstance(d['release_fields'], list) or set(d['release_fields']) - set(CORE_FIELDS)):
        raise DesignInvalid('design_values')
    return d


def load_design(path, expected_sha256):
    raw = open(path, 'rb').read()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise DesignInvalid('design_sha256')
    return check_design(json.loads(raw))


def alpha_k(design, k):
    """Significance for attempt k. Sums to under family_alpha over any number of attempts, so trying again always costs."""
    if type(k) is not int or not 1 <= k <= design['k_max']:
        raise DesignInvalid('attempt_number')
    return design['family_alpha'] / (k * (k + 1))


# ---- case log (host process, public store)

def log_case(conn, *, epoch_id, case_id, eligible_at, cluster, source, now):
    """First write of a case wins and keeps its time. A case from the future, or logged into an epoch that is not the open evaluated one, is refused."""
    if (not all(isinstance(x, str) and x and len(x) <= 200 for x in (epoch_id, case_id, cluster, source))
            or not _finite(eligible_at) or not _finite(now) or eligible_at > now):
        raise ValueError('case_invalid')
    with S.write_transaction(conn):
        row = conn.execute('SELECT payload_json FROM epochs WHERE id=?', (epoch_id,)).fetchone()
        e = json.loads(row[0]) if row else {}
        if e.get('kind') != 'evaluated' or e.get('closed_at') is not None or eligible_at < e.get('opened_at', math.inf):
            raise ValueError('case_outside_open_evaluated_epoch')
        if conn.execute('SELECT 1 FROM evaluation_cases WHERE epoch_id=? AND case_id=?', (epoch_id, case_id)).fetchone():
            return None
        return S.append(conn, 'evaluation_cases', dict(epoch_id=epoch_id, case_id=case_id, eligible_at=eligible_at, cluster=cluster, source=source, logged_at=now))


def record_gap(conn, *, epoch_id, source, from_at, to_at, now):
    """The logger was down or blind for this span. A recorded gap is honest; an unrecorded one makes the denominator a guess."""
    if not all(isinstance(x, str) and x for x in (epoch_id, source)) or not all(_finite(x) for x in (from_at, to_at, now)) or not from_at < to_at <= now:
        raise ValueError('gap_invalid')
    return S.append(conn, 'evaluation_gaps', dict(epoch_id=epoch_id, source=source, from_at=from_at, to_at=to_at, recorded_at=now))


def cases(conn, epoch_id):
    return [dict(json.loads(p), seq=seq) for seq, p in conn.execute('SELECT seq,payload_json FROM evaluation_cases WHERE epoch_id=? ORDER BY seq', (epoch_id,))]


# ---- grading (grader process, evaluation file)

def grade_epoch(office, evaluation, project, *, epoch_id, look_id, look_at):
    """One grade row per logged case for this look. Re-running adds nothing: a frozen look never changes. Returns counts by state."""
    if not isinstance(look_id, str) or not look_id or not _finite(look_at):
        raise ValueError('look_invalid')
    done = {json.loads(p)['case_id'] for (p,) in evaluation.execute("SELECT payload_json FROM grades WHERE epoch_id=? AND kind=?", (epoch_id, 'case:' + look_id))}
    counts = dict.fromkeys(STATES, 0)
    for case in cases(office, epoch_id):
        if case['case_id'] in done or case['eligible_at'] > look_at:
            continue
        team, base = project.team_score(case, look_at), project.baseline_score(case, look_at)
        for state, value in (team, base):
            if state not in STATES or (state == 'observed' and not _finite(value)):
                raise ValueError('score_invalid')            # a broken scorer stops the grade; it never becomes a zero
        state = 'pending' if 'pending' in (team[0], base[0]) else 'censored' if 'censored' in (team[0], base[0]) else 'observed'
        counts[state] += 1
        if state == 'pending':
            continue                                         # not graded yet; a later run of the same look picks it up
        S.append(evaluation, 'grades', dict(epoch_id=epoch_id, kind='case:' + look_id, candidate_seq=case['seq'], case_id=case['case_id'], cluster=case['cluster'],
                                            state=state, team=team[1] if state == 'observed' else None, baseline=base[1] if state == 'observed' else None, graded_at=look_at))
    return counts


# ---- statistics (pure, standard library only)

def _betacf(a, b, x):
    tiny = 1e-300;c = 1.;d = 1. - (a + b) * x / (a + 1.);d = 1. / (d if abs(d) > tiny else tiny);h = d
    for m in range(1, 300):
        for num in (m * (b - m) * x / ((a + 2 * m - 1) * (a + 2 * m)), -(a + m) * (a + b + m) * x / ((a + 2 * m) * (a + 2 * m + 1))):
            d = 1. + num * d;d = 1. / (d if abs(d) > tiny else tiny)
            c = 1. + num / c;c = c if abs(c) > tiny else tiny
            h *= d * c
        if abs(d * c - 1.) < 1e-14:
            break
    return h


def _t_cdf(t, df):
    x = df / (df + t * t)
    front = math.exp(math.lgamma((df + 1) / 2.) - math.lgamma(df / 2.) - math.lgamma(.5) + (df / 2.) * math.log(x) + .5 * math.log(1. - x)) if 0 < x < 1 else 0.
    a, b = df / 2., .5
    tail = front * _betacf(a, b, x) / a if x < (a + 1.) / (a + b + 2.) else 1. - front * _betacf(b, a, 1. - x) / b
    return 1. - tail / 2. if t >= 0 else tail / 2.


def t_quantile(p, df):
    """Student t quantile by bisection on the CDF. p in (0.5, 1), df >= 1."""
    if not .5 < p < 1 or type(df) is not int or df < 1:
        raise ValueError('t_quantile_domain')
    lo, hi = 0., 1.
    while _t_cdf(hi, df) < p:
        hi *= 2.
        if hi > 1e9:
            raise ValueError('t_quantile_domain')
    for _ in range(200):
        mid = (lo + hi) / 2.
        lo, hi = (mid, hi) if _t_cdf(mid, df) < p else (lo, mid)
    return (lo + hi) / 2.


def lower_bound(values, clusters, alpha):
    """One-sided lower confidence bound on the mean per case, robust to dependence inside a cluster (ratio estimator, G-1 degrees of freedom).
    Returns (mean, bound). With one cluster there is no variance estimate and the bound is None."""
    n = len(values)
    if n == 0 or len(clusters) != n:
        return None, None
    mean = sum(values) / n
    groups = {}
    for v, c in zip(values, clusters):
        s = groups.setdefault(c, [0., 0]);s[0] += v;s[1] += 1
    g = len(groups)
    if g < 2:
        return mean, None
    se = math.sqrt(g / (g - 1.) * sum((s - mean * m) ** 2 for s, m in groups.values())) / n
    return mean, mean - t_quantile(1. - alpha, g - 1) * se


def verdict(design, snapshot):
    """Pure. snapshot: k, grades (rows of grade_epoch), fixed_cost, counters, custody ('proven' or 'unproven'), look_id, replicates (bool: an earlier,
    separate epoch of the identical nominee already passed). -> {state, reasons, ...numbers}. Underpowered, incomplete or unclean is no_verdict."""
    check_design(design)
    rows = snapshot['grades'];observed = [r for r in rows if r['state'] == 'observed']
    reasons = []
    alpha = alpha_k(design, snapshot['k'])
    if snapshot['look_id'] not in design['looks']:
        reasons.append('look_not_scheduled')
    for name in design['counters']:
        count = snapshot['counters'].get(name)
        if type(count) is not int or count != 0:
            reasons.append('counter:%s:%s' % (name, 'unknown' if type(count) is not int else count))
    if not _finite(snapshot['fixed_cost']) or snapshot['fixed_cost'] < 0:
        reasons.append('fixed_cost_unknown')
    if len(rows) < design['min_cases']:
        reasons.append('too_few_cases')
    if rows and (len(rows) - len(observed)) / len(rows) > design['max_censored_share']:
        reasons.append('too_much_censored')
    clusters = [r['cluster'] for r in observed]
    if len(set(clusters)) < design['min_clusters']:
        reasons.append('too_few_clusters')
    out = dict(k=snapshot['k'], alpha_k=alpha, cases=len(rows), graded=len(observed), censored=len(rows) - len(observed), clusters=len(set(clusters)),
               custody=snapshot['custody'], look_id=snapshot['look_id'])
    if reasons or not observed:
        return dict(out, state='no_verdict', reasons=reasons or ['nothing_observed'])
    share = snapshot['fixed_cost'] / len(observed)            # the team carries the cost, spread over the cases it was judged on
    net = [r['team'] - share for r in observed]
    lift = [r['team'] - share - r['baseline'] for r in observed]
    net_mean, net_low = lower_bound(net, clusters, alpha)
    lift_mean, lift_low = lower_bound(lift, clusters, alpha)
    by_cluster = {}
    for v, c in zip(net, clusters):
        by_cluster[c] = by_cluster.get(c, 0.) + v
    positive = sum(v > 0 for v in by_cluster.values())
    gains = sorted((v for v in net if v > 0), reverse=True)
    top3 = sum(gains[:3]) / sum(gains) if gains else None
    out.update(net_mean=net_mean, net_lower=net_low, lift_mean=lift_mean, lift_lower=lift_low, positive_clusters=positive, top3_share=top3,
               team_total=sum(r['team'] for r in observed), baseline_total=sum(r['baseline'] for r in observed), fixed_cost=snapshot['fixed_cost'])
    failed = []
    if net_low is None or net_low <= design['delta0']:
        failed.append('net_lower_bound_not_above_delta0')
    if lift_low is None or lift_low <= design['delta0']:
        failed.append('lift_lower_bound_not_above_delta0')
    if positive < design['min_positive_clusters']:
        failed.append('too_few_positive_clusters')
    if top3 is None or top3 > design['top3_share_limit']:
        failed.append('result_concentrated_in_three_cases')
    if failed:
        return dict(out, state='fail', reasons=failed)
    if design['require_readonly_config'] and snapshot['custody'] != 'proven':
        return dict(out, state='pass_unproven_custody', reasons=['config_custody_unproven'])
    if not snapshot.get('replicates'):
        return dict(out, state='pass_pending_replication', reasons=['needs_a_second_separate_epoch_of_the_identical_nominee'])
    return dict(out, state='pass', reasons=[])


# ---- look and release

def look(office, evaluation, project, *, design, epoch_id, look_id, look_at, custody):
    """Grade, then compute the verdict. Refused while the plan is not met or any case is still pending: results are not readable early,
    and a refused look spends nothing. `custody` is 'proven' only when roster.verify_nominee succeeded for this look with a read-only config mount;
    it can never be better than what the freeze itself recorded."""
    check_design(design)
    row = office.execute('SELECT payload_json FROM epochs WHERE id=?', (epoch_id,)).fetchone()
    e = json.loads(row[0]) if row else {}
    if e.get('kind') != 'evaluated' or e.get('status') == 'broken' or e.get('closed_at') is not None:
        raise LookRefused('not_an_open_intact_evaluated_epoch')
    if look_id not in design['looks'] or custody not in ('proven', 'unproven') or not _finite(look_at) or look_at <= e['opened_at']:
        raise LookRefused('look_not_scheduled')
    logged = cases(office, epoch_id)
    if len(logged) < design['min_cases'] or len({c['cluster'] for c in logged}) < design['min_clusters']:
        raise LookRefused('plan_not_reached')
    for case in logged:                                       # ask before grading, so an early look writes nothing
        if 'pending' in (project.team_score(case, look_at)[0], project.baseline_score(case, look_at)[0]):
            raise LookRefused('cases_pending')
    grade_epoch(office, evaluation, project, epoch_id=epoch_id, look_id=look_id, look_at=look_at)
    grades = [json.loads(p) for (p,) in evaluation.execute('SELECT payload_json FROM grades WHERE epoch_id=? AND kind=? ORDER BY seq', (epoch_id, 'case:' + look_id))]
    prior = [json.loads(p) for (p,) in office.execute('SELECT payload_json FROM trial_registry ORDER BY seq')]
    replicates = any(t.get('status') == 'PASS_PENDING_REPLICATION' and t.get('nominee_config_hash') == e['nominee_config_hash'] and t.get('epoch_id') != epoch_id for t in prior)
    result = verdict(design, dict(k=e['k'], grades=grades, fixed_cost=project.fixed_cost(epoch_id, look_at), counters=project.counters(epoch_id),
                                  custody=custody if e.get('config_custody') == 'proven' else 'unproven', look_id=look_id, replicates=replicates))
    return dict(result, epoch_id=epoch_id, nominee_config_hash=e['nominee_config_hash'], hypothesis_id=e['hypothesis_id'], looked_at=look_at,
                gaps=office.execute('SELECT count(*) FROM evaluation_gaps WHERE epoch_id=?', (epoch_id,)).fetchone()[0],
                grades_head=(evaluation.execute('SELECT row_hash FROM grades ORDER BY seq DESC LIMIT 1').fetchone() or [None])[0])


def release(office, *, design, result, released_at):
    """Publish the verdict and the design's aggregate fields, close the epoch and append the trial's terminal row, in one transaction.
    Raw grades never enter the public store. Returns the released row's hash."""
    check_design(design)
    if result.get('state') not in ('pass', 'pass_pending_replication', 'pass_unproven_custody', 'fail', 'no_verdict') or not _finite(released_at):
        raise ValueError('release_invalid')
    keep = ('state', 'reasons', 'k', 'alpha_k', 'clusters', 'custody', 'look_id', 'looked_at', 'epoch_id', 'hypothesis_id', 'nominee_config_hash', 'gaps', 'grades_head',
            'net_mean', 'net_lower', 'lift_mean', 'lift_lower', 'positive_clusters', 'top3_share')
    body = {key: result[key] for key in keep + tuple(design['release_fields']) if key in result}
    body.update(kind='evaluation_verdict', released_at=released_at, unit=design['unit'], estimand=design['estimand'], delta0=design['delta0'])
    with S.write_transaction(office):
        row = office.execute('SELECT payload_json FROM epochs WHERE id=?', (result['epoch_id'],)).fetchone()
        e = json.loads(row[0]) if row else {}
        if e.get('kind') != 'evaluated' or e.get('closed_at') is not None or e.get('status') == 'broken' or e.get('nominee_config_hash') != result['nominee_config_hash']:
            raise ValueError('release_epoch_mismatch')
        digest = S.append(office, 'sealed_views', body)
        e.update(closed_at=released_at, status='completed', verdict=result['state'], release_hash=digest)
        office.execute('UPDATE epochs SET payload_json=? WHERE id=?', (S.canonical_json(e).decode(), result['epoch_id']))
        S.append(office, 'trial_registry', dict(k=e['k'], hypothesis_id=e['hypothesis_id'], alpha_k=e['alpha_k'], nominee_config_hash=e['nominee_config_hash'],
                                                epoch_id=result['epoch_id'], status=result['state'].upper(), look_id=result['look_id'], release_hash=digest))
    return digest


def request_promotion(office, *, release_hash, author, done_when, guard, known_at=None):
    """The only effect of a pass: a ticket for a human, citing the sealed release. Any other verdict is refused."""
    row = office.execute('SELECT payload_json,prev_hash FROM sealed_views WHERE row_hash=?', (release_hash,)).fetchone()
    if row is None or hashlib.sha256(S.canonical_json(json.loads(row[0])) + row[1].encode()).hexdigest() != release_hash:
        raise ValueError('release_unknown')
    body = json.loads(row[0])
    if body.get('kind') != 'evaluation_verdict' or body.get('state') != 'pass':
        raise ValueError('promotion_needs_a_pass')
    problem = 'Promotion request: %s passed evaluation (%s: lower bound %.4g %s, required minimum %.4g).' % (
        body['hypothesis_id'], body['estimand'], body['lift_lower'], body['unit'], body['delta0'])
    return T.file_ticket(office, author=author, problem=problem, expected_impact=body['lift_mean'], evidence_refs=['sealed_views:' + release_hash],
                         done_when=done_when, guard=guard, known_at=known_at)
