"""Wallet analyzer (Office edition): is this prediction-market wallet's record an edge, and of what kind? Read-only, public data, Polymarket on Polygon (the one chain the source analyzer supports).

Ported from the source project's wallet protocol (stage-1 gates, mode router, held branch, flipper branch, structured findings, tag grain). What is kept: the same inputs (closed positions, open positions,
trade activity), FIFO flip reconstruction, flip_rate = positions with a pre-resolution sell / closed positions, the mode thresholds 0.30 / 0.70, the 3000-row activity cap and its truncation rule, the
findings matrix (data_insufficient, no_edge, underpowered, operator_review, supported_hypothesis), finding_uid = sha256(wallet|run_id|window_id|type|hypothesis), insert-ignore results, supersession.
What is NOT ported (it needs the source's duckdb ledger, subgraph reads and bootstrap machinery, about 23k lines): the capital ledger, timing / sizing / liquidity / exit-attribution surfaces, copy-latency,
the bootstrap tests and the supabase writers. The verdict here uses a plain normal-approximation interval on per-position ROI, so it is a screen, not the source's full protocol: say so when you quote it.

Tables (the Office's own sqlite file, wallet_db): wallet_analysis_results, wallet_findings, wallet_flipper_tag_grain. Nothing here signs, trades or copies."""
import hashlib
import json
import math
import re
import sqlite3
import statistics
import time
from pathlib import Path

from . import common

PROTOCOL = 'office-wallet-screen-v1'
DATA = 'https://data-api.polymarket.com'
GAMMA = 'https://gamma-api.polymarket.com'
CHAINS = {'polygon': 'polygon', '137': 'polygon', 'polymarket': 'polygon'}
FLIP_HELD_MAX, FLIP_FLIPPER_MIN = 0.30, 0.70       # the source's locked mode thresholds
ACTIVITY_CAP = 3000
PAGE = 500
MIN_CLOSED = 20                                    # below this: data_insufficient (stage-1 R2)
MIN_N = 30                                         # below this a branch is underpowered
DORMANT_S = 90 * 86400                             # no trade this long: data_insufficient (stage-1 R3)
Z95 = 1.96
FETCH = common.fetch_json                          # tests replace it: FETCH(url) -> (status, json)

SCHEMA = '''
CREATE TABLE IF NOT EXISTS wallet_analysis_results(wallet TEXT NOT NULL, run_id TEXT NOT NULL, window_id TEXT NOT NULL, chain TEXT NOT NULL, protocol TEXT NOT NULL, analyzed_at REAL NOT NULL,
  fixture_hash TEXT NOT NULL, mode TEXT, flip_rate REAL, overall_verdict TEXT NOT NULL, primary_edge_type TEXT, reason TEXT, report_json TEXT NOT NULL, superseded_by TEXT,
  PRIMARY KEY(wallet, run_id, window_id));
CREATE TABLE IF NOT EXISTS wallet_findings(finding_uid TEXT PRIMARY KEY, wallet TEXT NOT NULL, run_id TEXT NOT NULL, window_id TEXT NOT NULL, finding_type TEXT NOT NULL, severity TEXT NOT NULL,
  source_hypothesis_id TEXT NOT NULL, message TEXT NOT NULL, created_at REAL NOT NULL, superseded_by TEXT);
CREATE INDEX IF NOT EXISTS wallet_findings_wallet ON wallet_findings(wallet, created_at);
CREATE TABLE IF NOT EXISTS wallet_flipper_tag_grain(wallet TEXT NOT NULL, run_id TEXT NOT NULL, fixture_hash TEXT NOT NULL, tag TEXT NOT NULL, n_flips INTEGER NOT NULL, win_rate REAL, mean_roi REAL,
  net_pnl_usd REAL, median_hold_s REAL, created_at REAL NOT NULL, superseded_by TEXT, PRIMARY KEY(wallet, run_id, tag));
'''


def open_db(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.executescript(SCHEMA)
    return conn


def valid_address(a):
    return isinstance(a, str) and re.fullmatch(r'0x[0-9a-fA-F]{40}', a) is not None


def num(v, default=0.):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


# ---- fetch ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
def _pages(endpoint, wallet, *, extra='', max_offset, fetch):
    rows, offset, term = [], 0, 'empty_page'
    while offset <= max_offset:
        status, data = fetch('%s/%s?user=%s&sizeThreshold=0&limit=%d&offset=%d%s' % (DATA, endpoint, wallet, PAGE, offset, extra))
        if status is None or status >= 400:
            term = 'api_refused:%s' % status
            break
        if not isinstance(data, list) or not data:
            break
        rows.extend(r for r in data if isinstance(r, dict))
        if len(data) < PAGE:
            term = 'partial_page'
            break
        offset += PAGE
    else:
        term = 'offset_cap'
    return rows, term


def fetch_wallet(wallet, fetch=None):
    fetch = fetch or FETCH
    closed, t1 = _pages('closed-positions', wallet, extra='&sortBy=TIMESTAMP&sortDirection=DESC', max_offset=10000, fetch=fetch)
    held, t2 = _pages('positions', wallet, max_offset=3000, fetch=fetch)
    acts, t3 = _pages('activity', wallet, extra='&type=TRADE', max_offset=ACTIVITY_CAP - PAGE, fetch=fetch)
    return dict(closed=closed, positions=held, activity=acts, terminations=dict(closed=t1, positions=t2, activity=t3))


def fixture_hash(data):
    keys = lambda rows, *f: sorted(tuple(str(r.get(k)) for k in f) for r in rows)
    blob = json.dumps([keys(data['closed'], 'conditionId', 'outcome', 'realizedPnl', 'totalBought'), keys(data['activity'], 'transactionHash', 'asset', 'side', 'size', 'timestamp')]).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


# ---- analysis ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------
def fifo_flips(activity):
    """Flips by FIFO match of BUY then SELL fills on one (conditionId, outcome). Bad rows are skipped."""
    groups = {}
    for r in activity:
        if r.get('conditionId') and r.get('outcome'):
            groups.setdefault((r['conditionId'], r['outcome']), []).append(r)
    flips, sold = [], set()
    for (cid, outcome), rows in groups.items():
        rows.sort(key=lambda r: int(num(r.get('timestamp'))))
        queue = []
        for r in rows:
            side, size, price, ts = str(r.get('side') or '').upper(), num(r.get('size')), num(r.get('price')), int(num(r.get('timestamp')))
            if size <= 0 or ts <= 0:
                continue
            if side == 'BUY':
                queue.append([size, ts, price])
            elif side == 'SELL':
                sold.add((cid, outcome))
                left = size
                while left > 1e-9 and queue:
                    head = queue[0]
                    take = min(head[0], left)
                    roi = (price - head[2]) / head[2] if head[2] > 0 else 0.
                    flips.append(dict(condition_id=cid, outcome=outcome, buy_ts=head[1], sell_ts=ts, size=take, roi=roi, pnl=take * (price - head[2]), hold_s=max(0, ts - head[1])))
                    head[0] -= take
                    left -= take
                    if head[0] <= 1e-9:
                        queue.pop(0)
    return flips, len(sold)


def interval(values):
    n = len(values)
    if n < 2:
        return None, None
    m, sd = statistics.fmean(values), statistics.stdev(values)
    half = Z95 * sd / math.sqrt(n)
    return m - half, m + half


def branch(name, rois, pnl):
    n = len(rois)
    lo, hi = interval(rois)
    mean = statistics.fmean(rois) if rois else None
    if n < MIN_N or lo is None:
        verdict = 'underpowered'
    elif pnl > 0 and lo > 0:
        verdict = 'edge_detected'
    elif pnl <= 0 or hi <= 0:
        verdict = 'no_edge'
    else:
        verdict = 'underpowered'
    return dict(branch=name, n=n, mean_roi=mean, roi_lo95=lo, roi_hi95=hi, net_pnl_usd=pnl, win_rate=(sum(r > 0 for r in rois) / n) if n else None, verdict=verdict)


def analyze(data, *, now, tags=None):
    """The report for one wallet's fetched data. tags: {condition_id: [tag, ...]} for the flipper tag grain, or None (grain not computed)."""
    closed, acts = data['closed'], data['activity']
    flips, n_sold = fifo_flips(acts)
    n_closed = len(closed)
    cap = len(acts) >= ACTIVITY_CAP
    flip_rate = n_sold / n_closed if n_closed else 0.
    last = max([int(num(r.get('timestamp'))) for r in acts + closed] or [0])
    report = dict(protocol=PROTOCOL, n_closed=n_closed, n_open=len(data['positions']), n_activity=len(acts), activity_cap_hit=cap, flip_rate=flip_rate, n_flips=len(flips),
                  last_activity_at=last or None, terminations=data['terminations'], limits='screen only: no capital ledger, timing, sizing, liquidity or bootstrap surfaces', branches=[], tag_grain=[])
    if n_closed < MIN_CLOSED:
        return dict(report, mode=None, overall_verdict='data_insufficient', reason='insufficient_sample', primary_edge_type=None)
    if not last or now - last > DORMANT_S:
        return dict(report, mode=None, overall_verdict='data_insufficient', reason='dormant', primary_edge_type=None)
    truncated = flip_rate > 0.50 and cap
    mode = 'held_only' if flip_rate < FLIP_HELD_MAX else ('held_only_degraded' if truncated else 'flipper_only' if flip_rate > FLIP_FLIPPER_MIN else 'mixed')
    run_held, run_flip = mode in ('held_only', 'held_only_degraded', 'mixed'), mode in ('flipper_only', 'mixed')
    if run_held:
        rois, pnl = [], 0.
        for r in closed:
            cost = num(r.get('totalBought')) * num(r.get('avgPrice'))
            p = num(r.get('realizedPnl'))
            pnl += p
            if cost > 0:
                rois.append(p / cost)
        report['branches'].append(branch('held', rois, pnl))
    if run_flip:
        report['branches'].append(branch('flipper', [f['roi'] for f in flips], sum(f['pnl'] for f in flips)))
        if tags is not None:
            by = {}
            for f in flips:
                for t in (tags.get(f['condition_id'].lower()) or ['untagged']):
                    by.setdefault(t, []).append(f)
            report['tag_grain'] = [dict(tag=t, n_flips=len(v), win_rate=sum(f['roi'] > 0 for f in v) / len(v), mean_roi=statistics.fmean(f['roi'] for f in v), net_pnl_usd=sum(f['pnl'] for f in v),
                                        median_hold_s=statistics.median(f['hold_s'] for f in v)) for t, v in sorted(by.items())]
    verdicts = [b['verdict'] for b in report['branches']]
    if mode == 'mixed':
        overall = 'edge_detected' if verdicts == ['edge_detected'] * 2 else 'no_edge' if verdicts == ['no_edge'] * 2 else 'operator_review' if set(verdicts) == {'edge_detected', 'no_edge'} else 'underpowered'
    else:
        overall = verdicts[0]
    if truncated:
        report['flags'] = ['FLIPPER_ANALYSIS_TRUNCATED']
    return dict(report, mode=mode, overall_verdict=overall, reason=None,
                primary_edge_type=(next((b['branch'] for b in report['branches'] if b['verdict'] == 'edge_detected'), None) if overall == 'edge_detected' else None))


def findings_of(wallet, run_id, window_id, report):
    """The structured findings for a report (every analysis emits at least one)."""
    v = report['overall_verdict']
    rows = []
    if v == 'data_insufficient':
        rows.append(('data_insufficient', 'info', 'stage1', 'rejected at stage 1: %s' % report['reason']))
    elif v == 'edge_detected':
        rows += [('supported_hypothesis', 'insight', b['branch'], '%s branch: n=%d mean ROI %.3f (95%% %.3f..%.3f) net %.2f' % (b['branch'], b['n'], b['mean_roi'], b['roi_lo95'], b['roi_hi95'], b['net_pnl_usd']))
                 for b in report['branches'] if b['verdict'] == 'edge_detected']
    else:
        rows.append((v, 'warn' if v == 'operator_review' else 'info', 'overall', 'verdict %s from branches %s' % (v, ', '.join('%s=%s' % (b['branch'], b['verdict']) for b in report['branches']))))
    if 'FLIPPER_ANALYSIS_TRUNCATED' in report.get('flags', []):
        rows.append(('unsupported_market_structure', 'warn', 'flipper', 'activity cap hit with flip_rate above 0.50: flipper branch suppressed'))
    return [dict(finding_uid=hashlib.sha256('|'.join((wallet, run_id, window_id, t, h)).encode()).hexdigest(), finding_type=t, severity=s, source_hypothesis_id=h, message=m) for t, s, h, m in rows]


# ---- store ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
def store(conn, wallet, chain, data, report, *, now, window_id='lifetime'):
    """Insert-ignore the result, its findings and tag grains; mark the wallet's earlier runs superseded. -> run_id. The same fixture is the same run (a no-op)."""
    fh = fixture_hash(data)
    run_id = hashlib.sha256((fh + PROTOCOL).encode()).hexdigest()[:16]
    with conn:
        new = conn.execute('INSERT OR IGNORE INTO wallet_analysis_results VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)',
                           (wallet, run_id, window_id, chain, PROTOCOL, now, fh, report['mode'], report['flip_rate'], report['overall_verdict'], report['primary_edge_type'], report['reason'], json.dumps(report))).rowcount
        if new:
            conn.execute('UPDATE wallet_analysis_results SET superseded_by=? WHERE wallet=? AND run_id!=? AND superseded_by IS NULL', (run_id, wallet, run_id))
            conn.execute('UPDATE wallet_findings SET superseded_by=? WHERE wallet=? AND run_id!=? AND superseded_by IS NULL', (run_id, wallet, run_id))
            conn.execute('UPDATE wallet_flipper_tag_grain SET superseded_by=? WHERE wallet=? AND run_id!=? AND superseded_by IS NULL', (run_id, wallet, run_id))
            for f in findings_of(wallet, run_id, window_id, report):
                conn.execute('INSERT OR IGNORE INTO wallet_findings VALUES(?,?,?,?,?,?,?,?,?,NULL)', (f['finding_uid'], wallet, run_id, window_id, f['finding_type'], f['severity'], f['source_hypothesis_id'], f['message'], now))
            for g in report['tag_grain']:
                conn.execute('INSERT OR IGNORE INTO wallet_flipper_tag_grain VALUES(?,?,?,?,?,?,?,?,?,?,NULL)', (wallet, run_id, fh, g['tag'], g['n_flips'], g['win_rate'], g['mean_roi'], g['net_pnl_usd'], g['median_hold_s'], now))
    return run_id


def fetch_tags(condition_ids, fetch=None, max_pages=6):
    """{condition_id: [tag slug]} from gamma /events (active, then closed), bounded to max_pages of 500 events per pass; unresolved ids stay untagged. Provider data, best effort."""
    fetch, want, out = fetch or FETCH, {c.lower() for c in condition_ids}, {}
    for params in ('active=true&closed=false', 'active=false&closed=true'):
        for page in range(max_pages):
            status, events = fetch('%s/events?%s&limit=500&offset=%d' % (GAMMA, params, page * 500))
            if status is None or status >= 400 or not isinstance(events, list) or not events:
                break
            for ev in events:
                slugs = sorted({str(t.get('slug') or '').lower() for t in (ev.get('tags') or []) if isinstance(t, dict) and t.get('slug')}) if isinstance(ev, dict) else []
                for m in (ev.get('markets') or []) if isinstance(ev, dict) else []:
                    cid = str(m.get('conditionId') or '').lower() if isinstance(m, dict) else ''
                    if cid in want and slugs and cid not in out:
                        out[cid] = slugs
            if len(events) < 500 or want <= set(out):
                break
    return out


def run(conn, address, *, now, fetch=None, chain='polygon', with_tags=True):
    """Fetch, analyze, store. -> dict(run_id, report). Raises ValueError for a bad address or chain."""
    if CHAINS.get(str(chain).lower()) != 'polygon':
        raise ValueError('chain_unsupported')
    if not valid_address(address):
        raise ValueError('bad_address')
    wallet = address.lower()
    data = fetch_wallet(wallet, fetch)
    if any(str(t).startswith('api_refused') for t in (data['terminations']['closed'], data['terminations']['activity'])) and not data['closed'] and not data['activity']:
        raise ConnectionError('source_unreachable')
    flips, _ = fifo_flips(data['activity'])
    tags = fetch_tags({f['condition_id'] for f in flips}, fetch) if with_tags and flips else None
    report = analyze(data, now=now, tags=tags)
    return dict(run_id=store(conn, wallet, 'polygon', data, report, now=now), report=report)
