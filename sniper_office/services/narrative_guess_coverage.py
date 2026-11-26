"""Replay one immutable guess on the observed pool sample in its own window."""

import json

from . import narrative
from ..approved_tools._read import db_read

NAME = 'narrative_guess_coverage'
LIMIT = 3000


@db_read('narrative_db')
def read(conn, now, guess_id=None, theme_id=None, slot_ts=None):
    if isinstance(guess_id, str) and 1 <= len(guess_id) <= 80 and theme_id is None and slot_ts is None:
        where, args = 'guess_id=?', (guess_id,)
    elif (guess_id is None and isinstance(theme_id, str) and 1 <= len(theme_id) <= 60
          and type(slot_ts) is int and 0 <= slot_ts <= now):
        where, args = 'theme_id=? AND slot_ts=?', (theme_id, slot_ts)
    else:
        return dict(state='refused', reason='use_guess_id_or_theme_and_slot')
    guesses = conn.execute("""
        SELECT guess_id, theme_id, slot_ts, known_at, expires_at, ticker_candidates_json, name_candidates_json
        FROM guesses WHERE """ + where + ' AND known_at<=? ORDER BY guess_id LIMIT 6', (*args, now)).fetchall()
    if not guesses:
        return dict(state='unknown', reason='guess_unknown')
    if len(guesses) > 1:
        return dict(state='refused', reason='ambiguous_guess', guess_ids=[g['guess_id'] for g in guesses[:5]])
    row = guesses[0]
    guess_id = row['guess_id']
    guess = dict(id=guess_id, theme_id=row['theme_id'], known_at=row['known_at'], expires_at=row['expires_at'],
                 tickers=json.loads(row['ticker_candidates_json']), names=json.loads(row['name_candidates_json']))
    launches = conn.execute("""
        SELECT l.name, l.symbol, l.first_seen_known_at, m.guesser_state
        FROM launches l LEFT JOIN matches m
          ON m.network_id=l.network_id AND m.mint=l.mint AND m.source=? AND m.matched_at<=?
        WHERE l.first_seen_known_at>? AND l.first_seen_known_at<? AND l.first_seen_known_at<=?
        ORDER BY l.first_seen_known_at, l.network_id, l.mint LIMIT ?
    """, (narrative.SOURCE, now, row['known_at'], row['expires_at'], now, LIMIT + 1)).fetchall()
    counts = dict(exact=0, fuzzy=0, theme_only=None, none=0, unknown=0)
    known = 0
    for launch in launches[:LIMIT]:
        has_identity = bool(narrative.normalise(launch['name']) or narrative.normalise(launch['symbol']))
        known += has_identity
        if not has_identity or launch['guesser_state'] != 'ok':
            counts['unknown'] += 1
            continue
        kind, _ = narrative.match(dict(name=launch['name'], symbol=launch['symbol'],
                                      first_seen=launch['first_seen_known_at']), [guess], guesser_state='ok')
        counts[kind] += 1
    limited = len(launches) > LIMIT
    sampled = min(len(launches), LIMIT)
    return dict(state='partial' if limited or counts['unknown'] else 'known', guess_id=guess_id,
                theme_id=row['theme_id'], slot_ts=row['slot_ts'], known_at=row['known_at'], expires_at=row['expires_at'],
                observation_cutoff=now, eligible_observed=sampled, limited=limited,
                name_or_symbol_known=known, name_or_symbol_unknown=sampled-known, counts=counts,
                note='Independent name/symbol replay, not the stored winning guess. Sample is observed pools only; theme labels are unavailable.')


def register(host):
    host.add_tool(NAME, 'Count name/symbol matches and unknowns for one immutable guess. Specify guess_id or both theme_id and slot_ts from the generation.',
                  dict(guess_id={'type': 'string'}, theme_id={'type': 'string'}, slot_ts={'type': 'integer'}), [], read)
