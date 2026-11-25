"""Narrative matching: did a new launch's name or ticker match a narrative a seat guessed beforehand? The Office edition of the source's narrative guesser store, matcher and labeller.

What the source did: a model wrote up to five guesses (theme, tickers, names) per 10-minute slot, append-only, no re-roll; a labeller read each launch from the Yellowstone Create tape (a Solana gRPC stream run
inside the funded runner stack) and matched it to the guesses written strictly before the launch was first seen. A stale guesser meant UNKNOWN (match_kind NULL), never 'none'.
Here: the narrative seat writes the guesses itself (tool narrative_guess, one generation per slot, immutable rows); launches come from `launch_sources` in the config (below), polled by this process;
matches are stored beside them and read with narrative_matches. The gRPC stream is not ported (needs grpcio and a provider key: see docs/SERVICES.md); the labeller reads any source that yields
(mint, name, symbol, first_seen).

    python3 -m sniper_office.services.narrative --config narrative.json [--once]
Config: narrative_db, clock_file, interval_s (30), launch_sources: [{"kind":"geckoterminal","network":"solana"} | {"kind":"table","db":path,"table":"launches"}]. Provider names are used for matching only; tools never echo them."""
import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
import unicodedata
import uuid
from pathlib import Path

from . import common

SLOT_S, EXPIRY_S, STALE_AFTER_S, N_GUESSES, MAX_CANDIDATES = 600, 7200, 1500., 5, 8
SOURCE, COHORT = 'model', 'office-narrative-v1'
FILLER = ('coin', 'inu', 'ai', 'token', 'sol', 'cto', 'the')
MIN_FUZZY_LEN = 5
RANK = {'exact': 3, 'fuzzy': 2, 'theme_only': 1}
VERSION_HASH = hashlib.sha256(json.dumps({'filler': list(FILLER), 'min_fuzzy_len': MIN_FUZZY_LEN, 'norm': 'nfkc-lower-alnum-tokens-v1', 'rank': RANK}, sort_keys=True).encode()).hexdigest()
GT = 'https://api.geckoterminal.com/api/v2/networks/%s/new_pools?page=1'
FETCH = common.fetch_json

DDL = '''
CREATE TABLE IF NOT EXISTS guesses(guess_id TEXT PRIMARY KEY, cohort TEXT NOT NULL, source TEXT NOT NULL, slot_ts REAL NOT NULL, known_at REAL NOT NULL, expires_at REAL NOT NULL, theme_id TEXT, rationale TEXT,
  confidence INTEGER CHECK(confidence BETWEEN 0 AND 100), ticker_candidates_json TEXT NOT NULL, name_candidates_json TEXT NOT NULL, run_id TEXT NOT NULL, actor TEXT);
CREATE INDEX IF NOT EXISTS guesses_known ON guesses(known_at);
CREATE TABLE IF NOT EXISTS slot_claims(source TEXT NOT NULL, slot_ts REAL NOT NULL, claimed_at REAL NOT NULL, PRIMARY KEY(source, slot_ts));
CREATE TABLE IF NOT EXISTS guesser_runs(run_id TEXT PRIMARY KEY, source TEXT NOT NULL, slot_ts REAL NOT NULL, finished_at REAL NOT NULL, rows_written INTEGER NOT NULL, UNIQUE(source, slot_ts));
CREATE TABLE IF NOT EXISTS launches(network_id TEXT NOT NULL, mint TEXT NOT NULL, name TEXT, symbol TEXT, first_seen_known_at REAL NOT NULL, venue TEXT NOT NULL, PRIMARY KEY(network_id, mint));
CREATE TABLE IF NOT EXISTS matches(network_id TEXT NOT NULL, mint TEXT NOT NULL, source TEXT NOT NULL, guess_id TEXT, match_kind TEXT CHECK(match_kind IS NULL OR match_kind IN ('exact','fuzzy','theme_only','none')),
  guesser_state TEXT NOT NULL CHECK(guesser_state IN ('ok','stale')), matcher_version_hash TEXT NOT NULL, matched_at REAL NOT NULL, guess_age_s REAL, PRIMARY KEY(network_id, mint, source));
CREATE TABLE IF NOT EXISTS cursors(source TEXT PRIMARY KEY, last_rowid INTEGER NOT NULL);
CREATE TRIGGER IF NOT EXISTS guesses_no_update BEFORE UPDATE ON guesses BEGIN SELECT RAISE(ABORT,'guesses are immutable'); END;
CREATE TRIGGER IF NOT EXISTS guesses_no_delete BEFORE DELETE ON guesses BEGIN SELECT RAISE(ABORT,'guesses are immutable'); END;
CREATE TRIGGER IF NOT EXISTS runs_no_update BEFORE UPDATE ON guesser_runs BEGIN SELECT RAISE(ABORT,'guesser_runs are immutable'); END;
CREATE TRIGGER IF NOT EXISTS runs_no_delete BEFORE DELETE ON guesser_runs BEGIN SELECT RAISE(ABORT,'guesser_runs are immutable'); END;
'''


def open_db(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript(DDL)
    return conn


# ---- matcher (pure) -----------------------------------------------------------------------------------------------------------------------------------------------------------------------
def normalise(s):
    if not s:
        return ''
    toks = [t for t in re.split(r'[^0-9a-z]+', unicodedata.normalize('NFKC', str(s)).lower()) if t]
    return ''.join([t for t in toks if t not in FILLER] or toks)


def _edit_le1(a, b):
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    a, b = (a, b) if len(a) < len(b) else (b, a)
    return any(a == b[:i] + b[i + 1:] for i in range(len(b)))


def _kind(keys, guess, theme_id):
    gkeys = {k for k in (normalise(x) for x in list(guess.get('tickers') or []) + list(guess.get('names') or [])) if k}
    if keys & gkeys:
        return 'exact'
    if any(len(a) >= MIN_FUZZY_LEN and len(b) >= MIN_FUZZY_LEN and _edit_le1(a, b) for a in sorted(keys) for b in sorted(gkeys)):
        return 'fuzzy'
    return 'theme_only' if theme_id and guess.get('theme_id') == theme_id else None


def match(launch, guesses, *, guesser_state):
    """-> (match_kind, guess_id). (None, None) = unknown (guesser not ok), never 'none'. A guess counts only if known_at < first_seen < expires_at; best rank wins, earliest guess breaks ties."""
    if guesser_state != 'ok':
        return None, None
    seen = float(launch['first_seen'])
    keys = {k for k in (normalise(launch.get('name')), normalise(launch.get('symbol'))) if k}
    best = None
    for g in guesses:
        if not float(g['known_at']) < seen < float(g['expires_at']):
            continue
        kind = _kind(keys, g, launch.get('theme_id'))
        if kind is None:
            continue
        rank = (-RANK[kind], float(g['known_at']), str(g['id']))
        if best is None or rank < best[0]:
            best = (rank, kind, g['id'])
    return ('none', None) if best is None else (best[1], best[2])


# ---- seat side: write guesses, read matches -----------------------------------------------------------------------------------------------------------------------------------------------
def write_generation(conn, guesses, *, now, actor):
    """One generation per 10-minute slot, all rows or none, never re-rolled. -> dict(state, slot_ts, written) or dict(state='refused', reason)."""
    slot = now - now % SLOT_S
    if not isinstance(guesses, list) or not 1 <= len(guesses) <= N_GUESSES:
        return dict(state='refused', reason='guesses_1_to_%d' % N_GUESSES)
    rows = []
    for g in guesses:
        if not isinstance(g, dict) or not isinstance(g.get('theme_id'), str) or not 1 <= len(g['theme_id']) <= 60 or not isinstance(g.get('rationale', ''), str):
            return dict(state='refused', reason='bad_guess')
        tk, nm = g.get('tickers') or [], g.get('names') or []
        conf = g.get('confidence')
        if (not isinstance(tk, list) or not isinstance(nm, list) or len(tk) > MAX_CANDIDATES or len(nm) > MAX_CANDIDATES or not (tk or nm)
                or any(not isinstance(x, str) or not 1 <= len(x) <= 40 for x in tk + nm) or type(conf) is not int or not 0 <= conf <= 100):
            return dict(state='refused', reason='bad_guess')
        rows.append((g['theme_id'], g.get('rationale', '')[:200], conf, [x.upper().lstrip('$') for x in tk], nm))
    run = uuid.uuid4().hex
    try:
        with conn:
            if conn.execute('INSERT OR IGNORE INTO slot_claims VALUES(?,?,?)', (SOURCE, slot, now)).rowcount != 1:
                return dict(state='refused', reason='slot_already_used', slot_ts=slot, next_slot_at=slot + SLOT_S)
            conn.execute('INSERT INTO guesser_runs VALUES(?,?,?,?,?)', (run, SOURCE, slot, now, len(rows)))
            for i, (theme, why, conf, tk, nm) in enumerate(rows):
                conn.execute('INSERT INTO guesses VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', ('%s-%d' % (run, i), COHORT, SOURCE, slot, now, now + EXPIRY_S, theme, why, conf, json.dumps(tk), json.dumps(nm), run, actor))
    except sqlite3.Error:
        return dict(state='refused', reason='store_error')
    return dict(state='known', slot_ts=slot, written=len(rows), expires_at=now + EXPIRY_S)


def matches_view(path, *, limit=30, kinds=None):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=1.)
    try:
        q, a = '', []
        if kinds:
            q = 'WHERE m.match_kind IN (%s)' % ','.join('?' * len(kinds))
            a = list(kinds)
        rows = conn.execute('SELECT m.network_id,m.mint,m.match_kind,m.guesser_state,m.guess_id,m.guess_age_s,m.matched_at,g.theme_id FROM matches m LEFT JOIN guesses g ON g.guess_id=m.guess_id ' + q
                            + ' ORDER BY m.matched_at DESC LIMIT ?', a + [max(1, min(int(limit), 200))]).fetchall()
        kinds_n = dict(conn.execute('SELECT COALESCE(match_kind,"unknown"),COUNT(*) FROM matches GROUP BY 1').fetchall())
    finally:
        conn.close()
    keys = ('network_id', 'mint', 'match_kind', 'guesser_state', 'guess_id', 'guess_age_s', 'matched_at', 'theme_id')
    return dict(counts=kinds_n, rows=[dict(zip(keys, r)) for r in rows], note='match_kind null = unknown (no fresh guess generation), not "none"')


# ---- labeller -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
def guesser_state_at(conn, seen):
    row = conn.execute('SELECT MAX(finished_at) FROM guesser_runs WHERE source=? AND finished_at<=?', (SOURCE, seen)).fetchone()
    return 'ok' if row and row[0] is not None and seen - row[0] <= STALE_AFTER_S else 'stale'


def label(conn, network_id, mint, name, symbol, seen, venue, now):
    """Record a launch once (first sighting is never moved) and its match. -> True when new."""
    with conn:
        if conn.execute('INSERT OR IGNORE INTO launches VALUES(?,?,?,?,?,?)', (network_id, mint, name, symbol, seen, venue)).rowcount != 1:
            return False
        state = guesser_state_at(conn, seen)
        gs = [dict(id=i, theme_id=t, known_at=k, expires_at=e, tickers=json.loads(tk), names=json.loads(nm))
              for i, t, k, e, tk, nm in conn.execute('SELECT guess_id,theme_id,known_at,expires_at,ticker_candidates_json,name_candidates_json FROM guesses WHERE known_at<? AND expires_at>?', (seen, seen))]
        kind, gid = match(dict(name=name, symbol=symbol, first_seen=seen), gs, guesser_state=state)
        age = next((seen - g['known_at'] for g in gs if g['id'] == gid), None)
        conn.execute('INSERT OR IGNORE INTO matches VALUES(?,?,?,?,?,?,?,?,?)', (network_id, mint, SOURCE, gid, kind, state, VERSION_HASH, now, age))
    return True


def poll_geckoterminal(src, now, fetch=None):
    """New pools of one network: [(network_id, mint, name, symbol, first_seen_at)]. first_seen is OUR fetch time (the provider's pool time is not a sighting). An unreachable provider is [] with no row written."""
    status, doc = (fetch or FETCH)(GT % src.get('network', 'solana'))
    out = []
    if status != 200 or not isinstance(doc, dict):
        return out
    for item in (doc.get('data') or [])[:100]:
        try:
            attrs, base = item['attributes'], item['relationships']['base_token']['data']['id']
            net, _, mint = base.partition('_')
            name = str(attrs.get('name') or '').split(' / ')[0]
            if mint and net:
                out.append((net, mint, name, name, now))
        except (KeyError, TypeError):
            continue
    return out


def poll_table(conn, src, now):
    """Rows of a launches table (mint, name, symbol, received_at, optional network_id) after the stored cursor; first_seen = received_at. A row stamped in the future is held back."""
    path = src['db']
    name = 'table:%s:%s' % (path, src.get('table', 'launches'))
    last = (conn.execute('SELECT last_rowid FROM cursors WHERE source=?', (name,)).fetchone() or (0,))[0]
    ext = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=1.)
    out, top = [], last
    table = re.sub(r'\W', '', src.get('table', 'launches'))
    try:
        net_col = '"solana"' if 'network_id' not in {r[1] for r in ext.execute('PRAGMA table_info(%s)' % table)} else 'COALESCE(network_id,"solana")'
        for rid, net, mint, nm, sym, seen in ext.execute('SELECT rowid,%s,mint,name,symbol,received_at FROM %s WHERE rowid>? ORDER BY rowid LIMIT 2000' % (net_col, table), (last,)):
            if seen is None or seen > now:
                break
            out.append((net, mint, nm, sym, seen))
            top = rid
    finally:
        ext.close()
    with conn:
        conn.execute('INSERT OR REPLACE INTO cursors VALUES(?,?)', (name, top))
    return out


def cycle(cfg, conn, now, fetch=None):
    n = 0
    for src in cfg.get('launch_sources', []):
        rows = poll_geckoterminal(src, now, fetch) if src.get('kind') == 'geckoterminal' else poll_table(conn, src, now) if src.get('kind') == 'table' else []
        for net, mint, name, symbol, seen in rows:
            n += label(conn, net, mint, name, symbol, seen, src['kind'], now)
    return n


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    cfg = common.load_config(args.config)
    conn = open_db(cfg['narrative_db'])
    while True:
        now = common.clock_now(cfg['clock_file'])
        n = cycle(cfg, conn, now) if now is not None else None      # no fresh clock sample: no sighting is recorded
        print(json.dumps(dict(new_launches=n, clock='ok' if now is not None else 'unknown')), flush=True)
        if args.once:
            return 0
        time.sleep(float(cfg.get('interval_s', 30)))


if __name__ == '__main__':
    sys.exit(main())
