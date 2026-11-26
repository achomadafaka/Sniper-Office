"""The strategy book, ported from the source desk: every paper position belongs to one named strategy and results are read per strategy, never as one total.
Written strategies (strategies.json, the source desk's own) plus seat-registered experiments (bk_strategies: hypothesis, entry rule, exit rule, kill criterion; at most 5 new per seat per UTC day, id = hash of the four fields).
A verdict is computed, never written: under MIN_SETTLED closed results is 'too few to judge'. A position that names no strategy is 'unclassified', never forced into the nearest."""
import hashlib
import json
import time
from pathlib import Path

MIN_SETTLED = 10
EVEN_BAND = 1.0        # mean result within this many percent of margin of zero is "even"
DAILY_LIMIT = 5
NONE_FITS = 'none fits'
KEYS = ('name', 'book', 'thesis', 'entry', 'exit', 'fails_when', 'status', 'owner', 'min_n', 'kill_mean_lt', 'kill_winrate_lt', 'retired_at', 'retired_stats')
FIELDS = ('hypothesis', 'entry_rule', 'exit_rule', 'kill_criterion')
STRATEGIES = json.loads((Path(__file__).parent / 'strategies.json').read_text())


def options(lane):
    return {sid: s['name'] for sid, s in STRATEGIES.items() if s['book'] == lane}


def register(led, role, lane, raw, now=None):
    now = now or time.time()
    if any(not isinstance(raw.get(f), str) or not 1 <= len(raw[f].strip()) <= 600 for f in FIELDS):
        return dict(state='refused', reason='fields_required', rule='hypothesis, entry_rule, exit_rule, kill_criterion: 1 to 600 characters each')
    exp = raw.get('expected_triggers_per_day')
    if exp is not None and (not isinstance(exp, (int, float)) or isinstance(exp, bool) or not 0 < exp <= DAILY_LIMIT):
        return dict(state='refused', reason='expected_triggers_per_day', rule='greater than 0 and at most %d' % DAILY_LIMIT)
    nums = {}
    for k, lo in (('min_n', 1), ('kill_mean_lt', None), ('kill_winrate_lt', 0.0)):
        v = raw.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or (k == 'min_n' and (int(v) != v or v < lo)) or (k == 'kill_winrate_lt' and not 0 <= v <= 1)):
            return dict(state='refused', reason=k, rule='min_n a whole number >= 1; kill_mean_lt a number (mean realized USD per trade); kill_winrate_lt 0 to 1')
        nums[k] = v
    spec = {f: ' '.join(raw[f].split()) for f in FIELDS}
    sid = 'experiment-' + hashlib.sha256(json.dumps([role, lane, *(spec[f].casefold() for f in FIELDS)]).encode()).hexdigest()[:24]
    if led.execute('SELECT 1 FROM bk_strategies WHERE strategy_id=?', (sid,)).fetchone():
        return dict(state='known', strategy_id=sid, note='already registered')
    if led.execute('SELECT COUNT(*) FROM bk_strategies WHERE owner_role=? AND created_at>=?', (role, now - now % 86400)).fetchone()[0] >= DAILY_LIMIT:
        return dict(state='refused', reason='daily_limit', limit=DAILY_LIMIT)
    led.execute('INSERT INTO bk_strategies(strategy_id,lane,owner_role,hypothesis,entry_rule,exit_rule,kill_criterion,expected_triggers_per_day,status,created_at,min_n,kill_mean_lt,kill_winrate_lt) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (sid, lane, role, *(spec[f] for f in FIELDS), exp, 'testing', now, int(nums['min_n'] or MIN_SETTLED), nums['kill_mean_lt'], nums['kill_winrate_lt']))
    return dict(state='registered', strategy_id=sid, status='testing', min_n=int(nums['min_n'] or MIN_SETTLED))


def usable(led, lane, sid):
    """None when an order may name `sid` in `lane`, else the refusal reason. Written strategies (strategies.json) are always active; an experiment must be registered for the lane and not retired or killed."""
    if sid in options(lane):
        return None
    row = led.execute('SELECT status FROM bk_strategies WHERE strategy_id=? AND lane=?', (sid, lane)).fetchone()
    return 'strategy_unregistered' if row is None else 'strategy_retired' if row[0] in ('retired', 'killed') else None


def retire_check(led, sid, now):
    """After a close: an experiment with its settled n >= min_n whose mean realized USD or win rate is under its machine kill field is marked retired with the stats. Returns the stats dict when it retired."""
    st = led.execute('SELECT status,min_n,kill_mean_lt,kill_winrate_lt FROM bk_strategies WHERE strategy_id=?', (sid,)).fetchone()
    if not st or st[0] in ('retired', 'killed') or (st[2] is None and st[3] is None):
        return None
    r = [x[0] or 0.0 for x in led.execute("SELECT realized FROM bk_positions WHERE strategy_id=? AND status='closed'", (sid,))]
    if len(r) < (st[1] or MIN_SETTLED):
        return None
    stats = dict(n=len(r), mean_realized=sum(r) / len(r), winrate=sum(1 for x in r if x > 0) / len(r), kill_mean_lt=st[2], kill_winrate_lt=st[3])
    if (st[2] is not None and stats['mean_realized'] < st[2]) or (st[3] is not None and stats['winrate'] < st[3]):
        led.execute("UPDATE bk_strategies SET status='retired',retired_at=?,stats_json=? WHERE strategy_id=?", (now, json.dumps(stats), sid))
        return stats
    return None


def review(led, role, sid, status, now=None):
    row = led.execute('SELECT owner_role FROM bk_strategies WHERE strategy_id=?', (sid,)).fetchone()
    if not row or row[0] != role or status not in ('kept', 'killed'):
        return dict(state='refused', reason='not_yours_or_bad_status')
    led.execute('UPDATE bk_strategies SET status=? WHERE strategy_id=?', (status, sid))
    return dict(state='known', strategy_id=sid, status=status)


def verdict(n, mean):
    if n < MIN_SETTLED:
        return 'too few to judge (%d settled, %d needed)' % (n, MIN_SETTLED)
    return 'works' if mean > EVEN_BAND else 'does not work' if mean < -EVEN_BAND else 'even'


def results(led, lane=None):
    """Per strategy: records, settled count, mean result in percent of margin (net of both fees and funding), open count, verdict. Totals are per lane, never added across lanes."""
    rows = {}
    specs = {**STRATEGIES, **{r['strategy_id']: dict(name=r['hypothesis'][:60], book=r['lane'], status=r['status'], thesis=r['hypothesis'], entry=r['entry_rule'], exit=r['exit_rule'], fails_when=r['kill_criterion'],
                                                     owner=r['owner_role'], min_n=r['min_n'], kill_mean_lt=r['kill_mean_lt'], kill_winrate_lt=r['kill_winrate_lt'], retired_at=r['retired_at'],
                                                     retired_stats=json.loads(r['stats_json']) if r['stats_json'] else None) for r in led.execute('SELECT * FROM bk_strategies')}}
    for p in led.execute('SELECT * FROM bk_positions' + (' WHERE lane=?' if lane else ''), (lane,) if lane else ()).fetchall():
        sid = p['strategy_id'] or 'unclassified'
        row = rows.setdefault(sid, dict(strategy_id=sid, **{k: specs.get(sid, {}).get(k) for k in KEYS}, settled=[], open=0))
        row['book'] = row['book'] or p['lane']
        if p['status'] == 'closed':
            row['settled'].append(100.0 * (p['realized'] or 0.0) / p['margin'] if p['margin'] else 0.0)
        else:
            row['open'] += 1
    out, totals = [], {}
    for sid, spec in specs.items():
        if (not lane or spec['book'] == lane) and sid not in rows:
            rows[sid] = dict(strategy_id=sid, **{k: spec.get(k) for k in KEYS}, settled=[], open=0)
    for row in rows.values():
        s = row.pop('settled')
        mean = sum(s) / len(s) if s else None
        row.update(settled_n=len(s), wins=sum(1 for x in s if x > 0), settled_mean_pct=None if mean is None else round(mean, 2), settled_sum_pct=round(sum(s), 2), open_n=row.pop('open'),
                   verdict=verdict(len(s), mean) if s else 'no settled result yet', unit='percent of margin')
        t = totals.setdefault(row['book'], dict(settled_n=0, wins=0, settled_sum_pct=0.0, open_n=0))
        t['settled_n'] += row['settled_n']; t['wins'] += row['wins']; t['settled_sum_pct'] = round(t['settled_sum_pct'] + row['settled_sum_pct'], 2); t['open_n'] += row['open_n']
        out.append(row)
    order = {'works': 0, 'even': 1, 'does not work': 2}
    out.sort(key=lambda r: (order.get(r['verdict'], 3), -r['settled_sum_pct'], -r['open_n']))
    return dict(strategies=out, totals_by_lane=totals, read_me='Each position belongs to one strategy. Settled = closed result in percent of margin, net of fees and funding; open marks are shown apart and never added. Totals are per lane.')
