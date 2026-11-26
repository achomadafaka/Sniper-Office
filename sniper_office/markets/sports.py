"""ESPN scoreboard and fresh bookmaker/Polymarket price comparisons."""
from collections import defaultdict
from contextlib import closing
from datetime import datetime
import json
import re
import time
import unicodedata
from zoneinfo import ZoneInfo

from . import books as B, common as C

ESPN = 'https://site.api.espn.com/apis/site/v2/sports/%s/scoreboard'
LEAGUES = ('basketball/nba', 'football/nfl', 'baseball/mlb', 'hockey/nhl', 'soccer/eng.1')
MIN_PROVIDER_GAP_S = 10
DEFAULTS = dict(leagues=list(LEAGUES), interval_s=300, retention_days=10, min_bookmakers=2)
EVENT_TZ = ZoneInfo('America/New_York')
MAX_LINE_AGE_S = 120
MAX_LINE_LAG_S = 30
UNCHANGED_LINE_S = 300
PM_MOVE_PP = 3
LINE_METADATA = {'bookmakers_json': 'TEXT', 'changed_at': 'REAL', 'observed_since': 'REAL', 'min_bookmakers': 'INTEGER'}
WIN = re.compile(r'Will (.+?) win on (\d{4}-\d{2}-\d{2})\?', re.I)
DRAW = re.compile(r'Will (.+?) vs\.? (.+?) (?:end in a draw|draw)(?: on (\d{4}-\d{2}-\d{2}))?\?', re.I)
CLUB_AFFIXES = {'fc', 'afc', 'cf', 'sc', 'sk', 'fk', 'rc', 'sfp'}
CLUB_ALIASES = {'sport lisboa e benfica': 'benfica', 'celta de vigo': 'celta vigo',
                'olympiakos': 'olympiacos', 'bayern munchen': 'bayern munich',
                'real sociedad de futbol': 'real sociedad'}


def team_key(name):
    folded = unicodedata.normalize('NFKD', str(name or '').casefold())
    words = re.findall(r'[^\W_]+', ''.join(c for c in folded if not unicodedata.combining(c)))
    while words and words[0] in CLUB_AFFIXES:
        words.pop(0)
    while words and words[-1] in CLUB_AFFIXES:
        words.pop()
    key = ' '.join(words)
    return CLUB_ALIASES.get(key, key)


def ml(text):
    """American moneyline to implied probability, with margin still included."""
    t = str(text or '').strip().upper()
    if t in ('EVEN', 'EV'):
        return 0.5
    v = C.num(t)
    if v is None or abs(v) < 100:
        return None
    return -v / (-v + 100.0) if v < 0 else 100.0 / (v + 100.0)


def _aliases(team):
    names = [team.get(k) for k in ('displayName', 'shortDisplayName')]
    # ESPN soccer name is an alternate club name. For US franchises it is a nickname.
    names.append(team.get('name'))
    return sorted({key for name in names if (key := team_key(name))})


def _line(odds, soccer):
    moneyline = odds.get('moneyline') or {}
    keys = [('home', 'homeTeamOdds'), ('away', 'awayTeamOdds')]
    if soccer:
        keys.append(('draw', 'drawOdds'))
    parts = [ml(((moneyline.get(side) or {}).get('close') or {}).get('odds')
                or (odds.get(legacy) or {}).get('moneyLine')) for side, legacy in keys]
    if any(p is None for p in parts):
        return None
    total = sum(parts)
    return parts, [p / total for p in parts]


def _provider_updated_at(odds):
    raw = odds.get('lastUpdated') or odds.get('updatedAt') or odds.get('lastModified')
    try:
        stamp = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
        return stamp.timestamp() if stamp.tzinfo is not None else None
    except ValueError:
        return None


def parse(doc, league, now):
    games, lines = [], []
    soccer = league.startswith('soccer/')
    for ev in (doc or {}).get('events') or ():
        try:
            comp = ev['competitions'][0]
            teams = {c['homeAway']: c for c in comp['competitors']}
            home, away = teams['home'], teams['away']
            hteam, ateam = home['team'], away['team']
            game_id = str(ev['id'])
            start = datetime.fromisoformat(ev['date'].replace('Z', '+00:00'))
            if start.tzinfo is None:
                continue
        except (KeyError, IndexError, TypeError, ValueError):
            continue

        providers = {}
        for odds in comp.get('odds') or ():
            provider = (odds.get('provider') or {}).get('name')
            if provider and (line := _line(odds, soccer)):
                providers.setdefault(provider.casefold(), (provider, _provider_updated_at(odds), *line))
        n = len(providers)
        if n:
            _, _, implied, _ = next(iter(providers.values()))
            ph, pa = implied[:2]
            fair = [sum(p[i] for _, _, _, p in providers.values()) / n for i in range(3 if soccer else 2)]
        else:
            ph, pa, fair = None, None, [None] * (3 if soccer else 2)
        games.append((game_id, league, start.timestamp(), hteam.get('displayName'), ateam.get('displayName'),
                      (ev.get('status') or {}).get('type', {}).get('state'), ph, pa, fair[0], ', '.join(p[0] for p in providers.values()) or None,
                      C.num(home.get('score')), C.num(away.get('score')), 'espn:scoreboard', now))
        lines.append((game_id, fair[1], fair[2] if soccer else None, n,
                      json.dumps(_aliases(hteam)), json.dumps(_aliases(ateam)), now,
                      json.dumps([dict(name=name, updated_at=updated) for name, updated, _, _ in providers.values()])))
    return games, lines


def tick(cfg, now, *, sleep=time.sleep, clock=time.monotonic):
    started = clock()
    cfg = {**DEFAULTS, **cfg}
    today = datetime.fromtimestamp(now, EVENT_TZ).date()
    min_bookmakers = cfg['min_bookmakers']
    if type(min_bookmakers) is not int or min_bookmakers < 1:
        raise ValueError('min_bookmakers_must_be_positive_integer')
    observation_gap = cfg['interval_s'] + max(0, len(cfg['leagues']) - 1)*MIN_PROVIDER_GAP_S + MAX_LINE_AGE_S
    with closing(C.connect(cfg['markets_db'], C.FEED_SCHEMA)) as feed:
        with C.txn(feed):
            columns = _line_columns(feed)
            for column, kind in LINE_METADATA.items():
                if column not in columns:
                    feed.execute(f'ALTER TABLE mk_sports_lines ADD COLUMN {column} {kind}')
        n, errs = 0, []
        for i, league in enumerate(cfg['leagues']):
            if i:
                sleep(MIN_PROVIDER_GAP_S)
            url = ESPN % league
            # The dated soccer request includes cup matchdays the default scoreboard can miss.
            if league.startswith('soccer/'):
                url += '?dates=' + today.strftime('%Y%m%d')
            try:
                doc = C.get_json(url)
                fetched_at = now + (clock() - started)
                rows, lines = parse(doc, league, fetched_at)
                with C.txn(feed):
                    previous = {r[0]: (tuple(r[1:4]), r[4], r[5]) for r in feed.execute('''
                        SELECT game_id,g.fair_home,l.fair_away,l.fair_draw,l.changed_at,l.observed_since
                        FROM mk_sports_games g JOIN mk_sports_lines l USING(game_id)
                        WHERE g.league=? AND g.fair_home IS NOT NULL AND l.changed_at IS NOT NULL
                        AND l.observed_since IS NOT NULL AND l.fetched_at BETWEEN ? AND ?''',
                        (league, fetched_at-observation_gap, fetched_at))}
                    for i, (game, line) in enumerate(zip(rows, lines)):
                        fair = (game[8], line[1], line[2])
                        prior, changed_at, observed_since = previous.get(game[0], (None, fetched_at, fetched_at))
                        lines[i] = (*line, changed_at if fair == prior else fetched_at, observed_since, min_bookmakers)
                    feed.executemany('INSERT OR REPLACE INTO mk_sports_games VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', rows)
                    feed.executemany('''INSERT OR REPLACE INTO mk_sports_lines
                        (game_id,fair_away,fair_draw,bookmaker_n,home_aliases,away_aliases,fetched_at,bookmakers_json,changed_at,observed_since,min_bookmakers)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?)''', lines)
                n += len(rows)
            except Exception as exc:
                errs.append('%s %s' % (league, type(exc).__name__))
        cutoff = now - 86400 * cfg['retention_days']
        feed.execute('DELETE FROM mk_sports_games WHERE fetched_at<?', (cutoff,))
        feed.execute('DELETE FROM mk_sports_lines WHERE fetched_at<?', (cutoff,))
        detail = 'games=%d%s' % (n, ' errors=' + ','.join(errs) if errs else '')
        C.status(feed, 'sports', n > 0 or not errs, detail, now + (clock() - started))
    return detail


def _has(feed, table):
    return feed.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _line_columns(feed):
    return {r[1] for r in feed.execute('PRAGMA table_info(mk_sports_lines)')}


def comparison_status(feed):
    columns = _line_columns(feed)
    if not columns:
        return dict(state='unknown', reason='bookmaker_lines_table_missing')
    if not LINE_METADATA.keys() <= columns:
        return dict(state='unknown', reason='bookmaker_lines_schema_old')
    if not feed.execute('SELECT 1 FROM mk_sports_lines WHERE observed_since IS NOT NULL LIMIT 1').fetchone():
        return dict(state='unknown', reason='bookmaker_lines_not_collected')
    return dict(state='known')


def games(feed, now, league=None, limit=100):
    rows = [dict(r) for r in feed.execute('SELECT * FROM mk_sports_games WHERE fetched_at>? AND (? IS NULL OR league=?) ORDER BY start_ts LIMIT ?',
                                         (now - 3600, league, league, limit))]
    if LINE_METADATA.keys() <= _line_columns(feed):
        lines = {r['game_id']: r for r in feed.execute('SELECT * FROM mk_sports_lines WHERE fetched_at>?', (now - 3600,))}
        for row in rows:
            ref = lines.get(row['game_id'])
            if ref and ref['bookmakers_json'] and ref['fetched_at'] == row['fetched_at']:
                row.update({k: ref[k] for k in ('fair_away', 'fair_draw', 'bookmaker_n')})
                row.update(line_changed_at=ref['changed_at'], bookmakers=json.loads(ref['bookmakers_json']))
    return rows


def _match(market, matchups, by_team):
    question = (market['question'] or '').strip()
    if win := WIN.fullmatch(question):
        team, date = win.groups()
        return by_team.get((team_key(team), date), ())
    if draw := DRAW.fullmatch(question):
        team, opponent, date = draw.groups()
        if not date and (end := C.num(market['end_ts'])) is not None:
            date = datetime.fromtimestamp(end, EVENT_TZ).date().isoformat()
        return [(g, 'draw') for g, direction in by_team.get((team_key(team), date), ())
                if g['league'].startswith('soccer/') and any(
                    other['game_id'] == g['game_id'] and other_direction != direction
                    for other, other_direction in by_team.get((team_key(opponent), date), ()))]
    if (match := matchups.get(market['condition_id'])) is not None:
        date = datetime.fromtimestamp(match['start_ts'], EVENT_TZ).date().isoformat()
        opposite = {g['game_id']: side for g, side in by_team.get((team_key(match['second_team']), date), ())}
        return [(g, side) for g, side in by_team.get((team_key(match['first_team']), date), ())
                if g['league'] == 'football/nfl' and abs(g['start_ts'] - match['start_ts']) <= 1800
                and g['game_id'] in opposite and opposite[g['game_id']] != side]
    return ()


def _quote_side(feed, market, side, now, pol):
    token = market[side.lower() + '_token']
    if not token:
        return None
    q = feed.execute('''SELECT * FROM mk_pm_quotes WHERE token_id=? AND condition_id=?
                      ORDER BY fetched_at DESC,id DESC LIMIT 1''', (token, market['condition_id'])).fetchone()
    if not q or q['source'] != 'clob:book' or q['fetched_at'] < market['fetched_at'] or not 0 <= now - q['fetched_at'] <= pol['quote_max_age_s']:
        return None
    bid, ask, size = C.num(q['bid']), C.num(q['ask']), C.num(q['ask_size'])
    if bid is None or ask is None or not 0 < bid <= ask < 1 or not size or size < 0:
        return None
    rate, exp = C.num(market['fee_rate']), C.num(market['fee_exp'])
    if rate is None or exp is None or rate < 0 or exp < 0:
        return None
    fee = B.pm_fee(rate, exp, ask)
    return dict(side=side, token_id=token, bid=bid, ask=ask, ask_size=size, spread=ask-bid, fee=fee,
                quote_at=q['fetched_at'], quote_age_s=now-q['fetched_at'], fee_source=market['fee_source'],
                round_trip_cost=ask-bid+fee+B.pm_fee(rate, exp, bid))


def _pm_move(feed, q, condition, changed_at, now):
    """Compare the current midpoint to the first valid one observed during this unchanged line."""
    if now - changed_at <= UNCHANGED_LINE_S:
        return None
    prior = feed.execute('''SELECT bid,ask FROM mk_pm_quotes WHERE token_id=? AND condition_id=? AND source='clob:book'
                          AND fetched_at>=? AND fetched_at<? AND 0<bid AND bid<=ask AND ask<1
                          ORDER BY fetched_at,id LIMIT 1''', (q['token_id'], condition, changed_at, q['quote_at'])).fetchone()
    if prior:
        return 50*abs(q['bid']+q['ask']-prior['bid']-prior['ask'])
    return None


def edges(feed, now, max_age_s=MAX_LINE_AGE_S, *, pol=None, league=None, limit=100):
    """Prematch two-token comparisons. A de-vigged line is a model input, not a hedge."""
    if not LINE_METADATA.keys() <= _line_columns(feed):
        return []
    pol = B.policy(pol)
    by_team = defaultdict(list)
    for r in feed.execute('''SELECT g.*,l.fair_away,l.fair_draw,l.bookmaker_n,l.home_aliases,l.away_aliases,l.changed_at,l.bookmakers_json,l.observed_since,l.min_bookmakers
                           FROM mk_sports_games g JOIN mk_sports_lines l USING(game_id)
                           WHERE g.state='pre' AND g.start_ts>? AND g.fetched_at BETWEEN ? AND ?
                           AND l.fetched_at=g.fetched_at AND l.observed_since IS NOT NULL AND (? IS NULL OR g.league=?)''',
                          (now, now-max_age_s, now, league, league)):
        date = datetime.fromtimestamp(r['start_ts'], EVENT_TZ).date().isoformat()
        for side in ('home', 'away'):
            for team in json.loads(r[side+'_aliases']):
                by_team[team, date].append((r, side))
    if not by_team:
        return []
    matchups = {r['condition_id']: r for r in feed.execute('SELECT * FROM mk_pm_matchups WHERE start_ts>?', (now,))} if _has(feed, 'mk_pm_matchups') else {}
    out = []
    for m in feed.execute('''SELECT * FROM mk_pm_markets WHERE closed=0 AND accepting=1
                           AND (end_ts IS NULL OR end_ts>?) AND fetched_at BETWEEN ? AND ?''',
                          (now, now-pol['quote_max_age_s'], now)):
        matches = _match(m, matchups, by_team)
        if len(matches) != 1:
            continue
        g, direction = matches[0]
        yes_fair = g['fair_'+direction]
        if yes_fair is None:
            continue
        common = dict(game_id=g['game_id'], home=g['home'], away=g['away'], league=g['league'], event_at=g['start_ts'],
                      fair_home=g['fair_home'], fair_away=g['fair_away'], fair_draw=g['fair_draw'],
                      condition_id=m['condition_id'], question=m['question'], outcome=direction,
                      team=g[direction] if direction != 'draw' else 'Draw', provider=g['odds_provider'],
                      bookmaker_n=g['bookmaker_n'], min_bookmakers=g['min_bookmakers'], bookmakers=json.loads(g['bookmakers_json']), line_at=g['fetched_at'], line_age_s=now-g['fetched_at'],
                      line_changed_at=g['changed_at'], line_unchanged_s=now-g['changed_at'])
        line_reasons = ['line_age_unknown'] if now-g['observed_since'] < UNCHANGED_LINE_S else []
        if g['bookmaker_n'] == 1:
            line_reasons.append('single_book')
        both = []
        for side, fair in [('YES', yes_fair), ('NO', 1-yes_fair)]:
            q = _quote_side(feed, m, side, now, pol)
            if not q:
                continue
            if not (pol['pm_min_price'] <= q['ask'] <= pol['pm_max_price'] and q['spread'] <= pol['pm_max_spread']):
                continue
            both.append(q)
            if q['quote_at'] - g['fetched_at'] > MAX_LINE_LAG_S:
                continue
            gross = fair-q['ask']
            hurdle = 3*q['round_trip_cost']
            pm_move_pp = _pm_move(feed, q, m['condition_id'], g['changed_at'], now)
            reasons = line_reasons.copy()
            if fair < .15 or fair > .85:
                reasons.append('extreme_fair_probability')
            if pm_move_pp is not None and pm_move_pp > PM_MOVE_PP:
                reasons.append('unchanged_line_vs_pm')
            low_trust = bool(reasons)
            out.append(dict(common, **q, kind='model_comparison', fair=fair, gap_pp=(gross-q['fee'])*100,
                            gross_gap_pp=100*gross, cost_hurdle_pp=100*hurdle, low_trust=low_trust,
                            low_trust_reasons=reasons, pm_move_pp=pm_move_pp,
                            qualifies=gross > hurdle and g['bookmaker_n'] >= g['min_bookmakers']
                            and not any(reason != 'single_book' for reason in reasons)))
        if len(both) == 2 and abs(both[0]['quote_at']-both[1]['quote_at']) <= 5:
            cost = sum(q['ask']+q['fee'] for q in both)
            if cost < 1:
                out.append(dict(common, kind='book_arb', side='BOTH', legs=both, ask=cost,
                                ask_size=min(q['ask_size'] for q in both), quote_age_s=max(q['quote_age_s'] for q in both),
                                gap_pp=(1-cost)*100, qualifies=False, low_trust=bool(line_reasons), low_trust_reasons=line_reasons))
    return sorted(out, key=lambda row: -row['gap_pp'])[:limit]


def main(argv=None):
    return C.main_loop('markets-sports', tick, argv, DEFAULTS['interval_s'])


if __name__ == '__main__':
    raise SystemExit(main())
