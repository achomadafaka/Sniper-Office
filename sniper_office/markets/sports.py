"""Sports feed host process: ESPN's public scoreboard (no key), games with the bookmaker moneyline ESPN carries, de-vigged to a fair home win probability. `python3 -m sniper_office.markets.sports --config sports.json [--once]`.
The source project's sports lane compared the same fair line against the prediction market's ask for an exact event match (its closing-line study). The comparison is `edges()`, name-matching a game to an open prediction market
question by both team names; a game with no matching market, or no line, shows nothing rather than a guess. A moneyline is a provider claim with the provider named in the row."""
from datetime import datetime

from . import common as C

ESPN = 'https://site.api.espn.com/apis/site/v2/sports/%s/scoreboard'
LEAGUES = ('basketball/nba', 'football/nfl', 'baseball/mlb', 'hockey/nhl', 'soccer/eng.1')
DEFAULTS = dict(leagues=list(LEAGUES), interval_s=300, retention_days=10)


def ml(text):
    """American moneyline text ('-150', '+130', 'EVEN') to the probability it implies (margin still in), or None."""
    t = str(text or '').strip().upper()
    if t in ('EVEN', 'EV'):
        return 0.5
    v = C.num(t.replace('+', ''))
    if not v:
        return None
    return -v / (-v + 100.0) if v < 0 else 100.0 / (v + 100.0)


def parse(doc, league, now):
    rows = []
    for ev in (doc or {}).get('events') or ():
        try:
            comp = ev['competitions'][0]
            teams = {c['homeAway']: c for c in comp['competitors']}
            home, away = teams['home'], teams['away']
        except (KeyError, IndexError, TypeError):
            continue
        odds = (comp.get('odds') or [{}])[0]
        hm = ((odds.get('moneyline') or {}).get('home') or {}).get('close', {}).get('odds') or (odds.get('homeTeamOdds') or {}).get('moneyLine')
        am = ((odds.get('moneyline') or {}).get('away') or {}).get('close', {}).get('odds') or (odds.get('awayTeamOdds') or {}).get('moneyLine')
        ph, pa = ml(hm), ml(am)
        fair = ph / (ph + pa) if ph and pa else None
        rows.append((str(ev.get('id')), league, datetime.fromisoformat(ev['date'].replace('Z', '+00:00')).timestamp() if ev.get('date') else None,
                     home['team'].get('displayName'), away['team'].get('displayName'), (ev.get('status') or {}).get('type', {}).get('state'), ph, pa, fair,
                     (odds.get('provider') or {}).get('name'), C.num(home.get('score')), C.num(away.get('score')), 'espn:scoreboard', now))
    return rows


def tick(cfg, now):
    cfg = {**DEFAULTS, **cfg}
    feed = C.connect(cfg['markets_db'], C.FEED_SCHEMA)
    n, errs = 0, []
    for league in cfg['leagues']:
        try:
            rows = parse(C.get_json(ESPN % league), league, now)
            feed.executemany('INSERT OR REPLACE INTO mk_sports_games VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', rows)
            n += len(rows)
        except Exception as exc:
            errs.append('%s %s' % (league, type(exc).__name__))
    feed.execute('DELETE FROM mk_sports_games WHERE fetched_at<?', (now - 86400 * cfg['retention_days'],))
    detail = 'games=%d%s' % (n, ' errors=' + ','.join(errs) if errs else '')
    C.status(feed, 'sports', n > 0 or not errs, detail, now)
    return detail


def edges(feed, now, max_age_s=900):
    """Games with a fair line and an open prediction market naming both teams: fair home probability against the market's ask for the home side. Gap in points, before fees; a claim, not a fill."""
    out = []
    for g in feed.execute('SELECT * FROM mk_sports_games WHERE fair_home IS NOT NULL AND fetched_at>?', (now - max_age_s,)).fetchall():
        last = lambda name: (name or '').split()[-1].lower()
        for m in feed.execute('SELECT condition_id,question,yes_token,no_token FROM mk_pm_markets WHERE closed=0').fetchall():
            q = m['question'].lower()
            if last(g['home']) in q and last(g['away']) in q:
                ask = feed.execute('SELECT ask,fetched_at FROM mk_pm_quotes WHERE token_id=? ORDER BY fetched_at DESC LIMIT 1', (m['yes_token'],)).fetchone()
                out.append(dict(game_id=g['game_id'], home=g['home'], away=g['away'], fair_home=g['fair_home'], provider=g['odds_provider'], condition_id=m['condition_id'], question=m['question'],
                                yes_ask=ask['ask'] if ask else None, quote_at=ask['fetched_at'] if ask else None,
                                note='YES is the home side only when the question says so; check the rules before ordering', gap_pp=None if not ask or ask['ask'] is None else round((g['fair_home'] - ask['ask']) * 100, 2)))
    return out


def main(argv=None):
    return C.main_loop('markets-sports', tick, argv, 300.0)


if __name__ == '__main__':
    raise SystemExit(main())
