"""The services plug-in: seat tools over the data the Office's own research services write. Name it in a seat config:

    "plugins": ["sniper_office.services"], "tool_groups": ["wallets", "creator", "narrative"]

Groups: wallets (wallet_analyze, wallet_findings), creator (creator_status, creator_launches), narrative (narrative_guess, narrative_matches, x_mentions).
Config keys per seat (all optional; a missing key or unreadable file answers {"state":"unknown","reason":...} as an error, nothing is defaulted):
wallet_db, creator_state (dict: collect / venue -> state directory), narrative_db, x_db, clock_file. docs/SERVICES.md lists the host processes that write these files.
Read-only on the host except wallet_db (analysis results) and narrative_db (the seat's own guesses). Paper only: no key, no signing, no order."""
import json
from pathlib import Path
import sqlite3
import time

from ..seat_tools import MAX_OUTPUT, result
from . import common, narrative as NR, wallet_analyzer as WA, xcap as XC

S = {'type': 'string'}


def out(doc, error=False):
    text = json.dumps(doc, default=str)
    if len(text) > MAX_OUTPUT - 200:
        text = json.dumps(dict(state='partial', reason='truncated', head=text[:MAX_OUTPUT - 400]))
    return result(text, error)


def unknown(reason, **more):
    return out(dict(state='unknown', reason=reason, **more), True)


def refused(reason, **more):
    return out(dict(state='refused', reason=reason, **more), True)


def _path(tools, key):
    v = tools.cfg.get(key)
    return Path(v) if isinstance(v, str) and v else None


def _now(tools):
    return common.clock_now(tools.cfg.get('clock_file'), host_now=tools.clock())


# ---- wallets ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------
def wallet_analyze(tools, address, chain='polygon', refresh=False):
    db = _path(tools, 'wallet_db')
    if db is None:
        return unknown('wallet_config_unknown')
    now = _now(tools)
    if now is None:
        return unknown('clock_unknown')
    if CHAIN_BAD(chain):
        return refused('chain_unsupported', supported=['polygon'], note='the analyzer reads Polymarket, which lives on Polygon')
    if not WA.valid_address(address):
        return refused('bad_address', hint='0x followed by 40 hex characters')
    wallet = address.lower()
    try:
        conn = WA.open_db(db)
        try:
            if not refresh:
                row = conn.execute('SELECT run_id,analyzed_at,report_json FROM wallet_analysis_results WHERE wallet=? AND superseded_by IS NULL ORDER BY analyzed_at DESC LIMIT 1', (wallet,)).fetchone()
                if row and now - row[1] <= float(tools.cfg.get('wallet_cache_s', 21600)):
                    return out(_summary(wallet, row[0], json.loads(row[2]), row[1], cached=True))
            try:
                done = WA.run(conn, wallet, now=now)
            except ConnectionError:
                return unknown('source_unreachable', source='data-api.polymarket.com')
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return unknown('wallet_db_unreadable')
    return out(_summary(wallet, done['run_id'], done['report'], now, cached=False))


def CHAIN_BAD(chain):
    return WA.CHAINS.get(str(chain).lower()) != 'polygon'


def _summary(wallet, run_id, report, at, cached):
    keep = ('overall_verdict', 'reason', 'mode', 'flip_rate', 'primary_edge_type', 'n_closed', 'n_open', 'n_flips', 'activity_cap_hit', 'flags', 'limits', 'terminations')
    return dict(state='partial', wallet=wallet, chain='polygon', run_id=run_id, analyzed_at=at, cached=cached, protocol=report.get('protocol'), branches=report.get('branches'),
                tag_grain=(report.get('tag_grain') or [])[:15], **{k: report[k] for k in keep if k in report})


def wallet_findings(tools, query=None, finding_type=None, severity=None, include_superseded=False, limit=20):
    db = _path(tools, 'wallet_db')
    if db is None:
        return unknown('wallet_config_unknown')
    q, a = [], []
    if query:
        q.append('wallet LIKE ?')
        a.append(str(query).lower().replace('%', '').replace('_', '') + '%')
    for col, v in (('finding_type', finding_type), ('severity', severity)):
        if v:
            q.append(col + '=?')
            a.append(v)
    if not include_superseded:
        q.append('superseded_by IS NULL')
    try:
        conn = sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=1.)
        try:
            rows = conn.execute('SELECT wallet,finding_type,severity,source_hypothesis_id,message,created_at,run_id,superseded_by FROM wallet_findings' + (' WHERE ' + ' AND '.join(q) if q else '')
                                + ' ORDER BY created_at DESC LIMIT ?', a + [max(1, min(int(limit), 100))]).fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError):
        return unknown('wallet_db_unreadable')
    keys = ('wallet', 'finding_type', 'severity', 'hypothesis', 'message', 'created_at', 'run_id', 'superseded_by')
    return out(dict(state='known', items=[dict(zip(keys, r)) for r in rows]))


# ---- creator ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------
def _checkpoint(tools, which):
    dirs = tools.cfg.get('creator_state')
    if not isinstance(dirs, dict) or which not in ('collect', 'venue') or not isinstance(dirs.get(which), str):
        return None, unknown('creator_config_unknown')
    try:
        path = Path(dirs[which]) / 'checkpoint.json'
        raw = path.read_bytes()
        if len(raw) > 64 * 1024 * 1024:
            return None, unknown('checkpoint_too_large')
        return (json.loads(raw), path.stat().st_mtime), None
    except (OSError, ValueError):
        return None, unknown('checkpoint_unreadable')


def creator_status(tools, which='collect'):
    got, err = _checkpoint(tools, which)
    if err:
        return err
    doc, mtime = got
    scalar = {k: v for k, v in doc.items() if isinstance(v, (str, int, float, bool)) or v is None}
    now = time.time()
    return out(dict(state='known', which=which, **{k: scalar[k] for k in ('status', 'reason', 'calls', 'subject', 'started_at_utc', 'updated_at_utc', 'next_block', 'discovery_next_block', 'completed_launches') if k in scalar},
                    launches=len(doc.get('launches', [])), creations=len(doc.get('creations', [])), observations=len(doc.get('observations', [])), last_error=doc.get('last_error'),
                    checkpoint_age_s=round(now - mtime), running=(doc.get('status') == 'running' and now - mtime < 600),
                    note='BSC creator-venue research, public RPC reads only; a stale checkpoint means the collector is not running'))


def creator_launches(tools, which='collect', limit=10):
    got, err = _checkpoint(tools, which)
    if err:
        return err
    doc, _ = got
    rows = doc.get('launches' if which == 'collect' else 'creations', [])
    slim = [{k: v for k, v in r.items() if isinstance(v, (str, int, float, bool)) or v is None} for r in rows[-max(1, min(int(limit), 50)):] if isinstance(r, dict)]
    return out(dict(state='known', which=which, total=len(rows), items=slim))


# ---- narrative --------------------------------------------------------------------------------------------------------------------------------------------------------------------------
def narrative_guess(tools, guesses):
    db = _path(tools, 'narrative_db')
    if db is None:
        return unknown('narrative_config_unknown')
    now = _now(tools)
    if now is None:
        return unknown('clock_unknown')
    try:
        conn = NR.open_db(db)
        try:
            doc = NR.write_generation(conn, guesses, now=now, actor=tools.actor)
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return unknown('narrative_db_unwritable')
    return out(doc, doc['state'] != 'known')


def narrative_matches(tools, limit=30, kinds=None):
    db = _path(tools, 'narrative_db')
    if db is None:
        return unknown('narrative_config_unknown')
    try:
        return out(dict(state='known', **NR.matches_view(db, limit=limit, kinds=kinds if isinstance(kinds, list) else None)))
    except (sqlite3.Error, OSError, ValueError):
        return unknown('narrative_db_unreadable')


def x_mentions(tools, mint=None, ticker=None, network_id=None, since_s=None, limit=30):
    db = _path(tools, 'x_db')
    if db is None:
        return unknown('x_config_unknown')
    if not (mint or ticker):
        return refused('mint_or_ticker_required')
    now = _now(tools)
    try:
        rows = XC.mentions_of(db, mint=mint, ticker=ticker, network_id=network_id, since=(now - float(since_s)) if now is not None and since_s else None, limit=limit)
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return unknown('x_db_unreadable')
    for r in rows:
        r['text'] = (r['text'] or '')[:400]       # public post text, untrusted: shown as data
    return out(dict(state='partial', items=rows, note='public posts captured from a logged-in browser; a ticker alone is ambiguous, only a mint with a chain is eligible evidence'))


TOOLS = [
    ('wallets', 'wallet_analyze', 'Screen a prediction-market (Polymarket, Polygon) wallet from its public record: held or flipper mode, per-branch ROI interval, verdict, tag grain. Cached for wallet_cache_s unless refresh. A screen, not the full protocol.',
     {'address': S, 'chain': S, 'refresh': {'type': 'boolean'}}, ['address'], wallet_analyze),
    ('wallets', 'wallet_findings', 'Stored findings of earlier wallet analyses; query is an address prefix. Superseded findings are hidden unless asked for.',
     {'query': S, 'finding_type': S, 'severity': S, 'include_superseded': {'type': 'boolean'}, 'limit': {'type': 'integer'}}, [], wallet_findings),
    ('creator', 'creator_status', 'State of a BSC creator research collector: collect (launch capture) or venue (other launch venues). Says when the checkpoint is stale.', {'which': S}, [], creator_status),
    ('creator', 'creator_launches', 'The newest launches or venue creations a creator collector recorded (scalar fields only).', {'which': S, 'limit': {'type': 'integer'}}, [], creator_launches),
    ('narrative', 'narrative_guess', 'Write this slot\'s narrative guesses (1 to 5; theme_id, rationale, confidence 0-100, tickers, names). One generation per 10-minute slot, immutable, never re-rolled; matched against launches first seen after it.',
     {'guesses': {'type': 'array', 'items': {'type': 'object'}}}, ['guesses'], narrative_guess),
    ('narrative', 'narrative_matches', 'Recent launch-vs-guess matches (exact / fuzzy / theme_only / none; null = unknown because no fresh guess generation existed).', {'limit': {'type': 'integer'}, 'kinds': {'type': 'array', 'items': S}}, [], narrative_matches),
    ('narrative', 'x_mentions', 'Captured public X posts that mention a coin by mint (with chain) or by ticker (ambiguous), newest first.', {'mint': S, 'ticker': S, 'network_id': S, 'since_s': {'type': 'number'}, 'limit': {'type': 'integer'}}, [], x_mentions),
]


def register(host):
    for group, name, description, properties, required, handler in TOOLS:
        host.add_tool(name, description, properties, required, handler, group=group)
