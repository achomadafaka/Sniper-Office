from datetime import datetime, timezone

import pytest

from sniper_office.markets import api, books, sports


NOW = datetime(2026, 11, 26, 17, tzinfo=timezone.utc).timestamp()


def game(fixture='1', start='2026-11-26T20:00Z', draw='+200'):
    odds = dict(provider=dict(name='Book'), homeTeamOdds=dict(moneyLine='+100'),
                awayTeamOdds=dict(moneyLine='+200'), drawOdds=dict(moneyLine=draw))
    return dict(id=fixture, date=start, status=dict(type=dict(state='pre')), competitions=[dict(
        competitors=[dict(homeAway=side, team=dict(displayName=team)) for side, team in
                     [('home', 'Plzeň'), ('away', 'Crystal Palace')]], odds=[odds])])


def add_market(feed, cid, question, yes=.38, no=.60, at=NOW):
    feed.execute("""INSERT INTO mk_pm_markets(condition_id,question,yes_token,no_token,closed,accepting,fee_rate,fee_exp,source,fetched_at)
                  VALUES(?,?,?,?,0,1,.05,2,'gamma:markets',?)""", (cid, question, cid+'y', cid+'n', at))
    for side, ask in [('y', yes), ('n', no)]:
        feed.execute("""INSERT INTO mk_pm_quotes(token_id,condition_id,bid,bid_size,ask,ask_size,source,fetched_at)
                      VALUES(?,?,?,10,?,10,'clob:book',?)""", (cid+side, cid, ask-.02, ask, at))


def test_three_way_reference_and_independent_yes_no_costs(fake, dbs):
    feed, _, path = dbs
    fake.routes = {sports.ESPN % 'soccer/uefa.europa': dict(events=[game()])}
    sports.tick(dict(markets_db=path/'feed.db', leagues=['soccer/uefa.europa']), NOW)
    add_market(feed, 'home', 'Will FC Plzen SK win on 2026-11-26?', yes=.38, no=.64)
    add_market(feed, 'away', 'Will Crystal Palace FC win on 2026-11-26?', yes=.32, no=.70)
    add_market(feed, 'wrong-date', 'Will FC Plzen win on 2026-11-27?')
    add_market(feed, 'wrong-team', 'Will Palace win on 2026-11-26?')
    add_market(feed, 'draw', 'Will FC Plzen vs. Crystal Palace FC end in a draw on 2026-11-26?', yes=.28, no=.74)
    payload = api.sports(feed, NOW+5)
    assert payload['games'][0]['fair_draw'] == pytest.approx(2/7)
    edges = {(row['condition_id'], row['side']): row for row in payload['edges']}
    assert edges.keys() == {(cid, side) for cid in ('home','away','draw') for side in ('YES','NO')}
    home = edges['home','YES']
    assert home['fair'] == pytest.approx(3/7) and home['quote_age_s'] == 5 and home['bookmaker_n'] == 1
    assert home['fee'] == pytest.approx(books.pm_fee(.05, 2, .38))
    assert home['gap_pp'] == pytest.approx((3/7 - .38 - home['fee'])*100)
    assert home['round_trip_cost'] == pytest.approx(.02 + home['fee'] + books.pm_fee(.05, 2, .36))
    assert edges['away','NO']['fair'] == pytest.approx(5/7)
    assert edges['draw','YES']['fair'] == pytest.approx(2/7)
    assert not home['qualifies']
    assert sports.edges(feed, NOW+121) == []


@pytest.mark.parametrize('events', [[game(draw=None)], [game(), game('2')]])
def test_skips_missing_soccer_draw_or_ambiguous_same_day_game(fake, dbs, events):
    feed, _, path = dbs
    fake.routes = {sports.ESPN % 'soccer/uefa.europa': dict(events=events)}
    sports.tick(dict(markets_db=path/'feed.db', leagues=['soccer/uefa.europa']), NOW)
    add_market(feed, 'm', 'Will Plzeň win on 2026-11-26?')
    assert sports.edges(feed, NOW+5) == []


@pytest.mark.parametrize('state,line_age', [('in', 0), ('post', 0), ('pre', 60)])
def test_rejects_started_games_and_bookmaker_lag(fake, dbs, state, line_age):
    feed, _, path = dbs
    fixture = game()
    fixture['status']['type']['state'] = state
    fake.routes = {sports.ESPN % 'soccer/uefa.europa': dict(events=[fixture])}
    sports.tick(dict(markets_db=path/'feed.db', leagues=['soccer/uefa.europa']), NOW-line_age)
    add_market(feed, 'm', 'Will Plzeň win on 2026-11-26?', yes=.42, no=.60)
    assert sports.edges(feed, NOW+5) == []


def test_consensus_hurdle_rails_low_trust_and_paired_cost(fake, dbs):
    feed, _, path = dbs
    fixture = game()
    comp = fixture['competitions'][0]
    comp['odds'].append(dict(comp['odds'][0], provider=dict(name='Book 2')))
    longshot = game('2')
    long_comp = longshot['competitions'][0]
    long_comp['competitors'][0]['team']['displayName'] = 'Team Longshot'
    long_comp['competitors'][1]['team']['displayName'] = 'Opponent'
    long_comp['odds'][0]['homeTeamOdds'] = dict(moneyLine='+2000')
    long_comp['odds'].append(dict(long_comp['odds'][0], provider=dict(name='Book 2')))
    fake.routes = {sports.ESPN % 'soccer/uefa.europa': dict(events=[fixture, longshot])}
    cfg = dict(markets_db=path/'feed.db', leagues=['soccer/uefa.europa'])
    sports.tick(cfg, NOW-340)
    sports.tick(cfg, NOW)
    market = 'Will Plzeň win on 2026-11-26?'
    add_market(feed, 'pass', market, yes=.32, no=.69)
    add_market(feed, 'hurdle', market, yes=.39, no=.61)
    add_market(feed, 'arb', market, yes=.38, no=.55)
    add_market(feed, 'spread', market, yes=.40, no=.70)
    add_market(feed, 'price', market, yes=.025, no=.975)
    add_market(feed, 'low', 'Will Team Longshot win on 2026-11-26?', yes=.04, no=.96)
    feed.execute("UPDATE mk_pm_quotes SET bid=.32 WHERE token_id='spready'")
    edges = {(r['condition_id'], r['side']): r for r in sports.edges(feed, NOW+5)}
    assert edges['pass','YES']['qualifies'] and edges['pass','YES']['bookmaker_n'] == 2
    assert edges['pass','YES']['cost_hurdle_pp'] == pytest.approx(300*edges['pass','YES']['round_trip_cost'])
    assert edges['hurdle','YES']['gap_pp'] > 0 and not edges['hurdle','YES']['qualifies']
    assert ('spread', 'YES') not in edges and ('price', 'YES') not in edges
    assert edges['low','YES']['low_trust'] and not edges['low','YES']['qualifies']
    pair = edges['arb','BOTH']
    assert pair['kind'] == 'book_arb' and pair['gap_pp'] == pytest.approx((1-sum(q['ask']+q['fee'] for q in pair['legs']))*100)


def test_nfl_tag_stores_dated_team_labels_and_respects_token_order(fake, dbs):
    import json

    from sniper_office.markets import polymarket

    feed, _, path = dbs
    fixture = game(draw=None)
    fixture['competitions'][0]['competitors'][0]['team']['displayName'] = 'Detroit Lions'
    fixture['competitions'][0]['competitors'][1]['team']['displayName'] = 'Chicago Bears'
    winner = dict(conditionId='nfl-game', slug='nfl-chi-det-2026-11-26', question='Chicago Bears vs. Detroit Lions',
                  gameStartTime='2026-11-26T20:00Z', endDate='2026-11-26T20:00Z', sportsMarketType='moneyline',
                  outcomes=json.dumps(['Chicago Bears', 'Detroit Lions']), clobTokenIds=json.dumps(['bears','lions']),
                  acceptingOrders=True, feeSchedule=dict(rate=.03, exponent=1))
    def book(token, ask):
        return dict(asset_id=token, bids=[dict(price=ask-.025,size=50)], asks=[dict(price=ask,size=50)])
    fake.routes = {
        sports.ESPN % 'football/nfl': dict(events=[fixture]),
        polymarket.GAMMA: [],
        polymarket.EVENTS+'?tag_slug=nfl': [dict(markets=[winner])],
        polymarket.EVENTS+'?tag_slug=soccer': [],
        polymarket.BOOKS: [book('bears', .34), book('lions', .68)],
    }
    sports.tick(dict(markets_db=path/'feed.db', leagues=['football/nfl']), NOW)
    polymarket.tick(dict(markets_db=path/'feed.db'), NOW, sleep=lambda _: None)
    rows = {r['side']: r for r in sports.edges(feed, NOW+5)}
    assert rows['YES']['token_id'] == 'bears' and rows['YES']['fair'] == pytest.approx(.4)
    assert rows['NO']['token_id'] == 'lions' and rows['NO']['fair'] == pytest.approx(.6)
    assert rows['YES']['outcome'] == 'away'
    assert feed.execute("SELECT room FROM mk_pm_markets WHERE condition_id='nfl-game'").fetchone()[0] == 'sports'


def test_collector_tracks_observed_changes_provider_timestamp_and_pm_move(fake, dbs):
    feed, _, path = dbs
    fixture = game()
    odds = fixture['competitions'][0]['odds'][0]
    odds['lastUpdated'] = '2026-11-26T16:59:00Z'
    fake.routes = {sports.ESPN % 'soccer/uefa.europa': dict(events=[fixture])}
    cfg = dict(markets_db=path/'feed.db', leagues=['soccer/uefa.europa'])
    for step in range(0, 361, 60):
        sports.tick(cfg, NOW+step, clock=lambda: 0)
    add_market(feed, 'm', 'Will Plzeň win on 2026-11-26?', yes=.38, no=.64)
    feed.execute("UPDATE mk_pm_markets SET fetched_at=? WHERE condition_id='m'", (NOW+360,))
    feed.execute("""INSERT INTO mk_pm_quotes(token_id,condition_id,bid,bid_size,ask,ask_size,source,fetched_at)
                   VALUES('my','m',.27,10,.29,10,'clob:book',?)""", (NOW+360,))
    payload = api.sports(feed, NOW+361)
    row = next(e for e in payload['edges'] if e['side']=='YES')
    assert row['line_changed_at'] == NOW and row['line_unchanged_s'] == 361
    assert row['bookmakers'] == [dict(name='Book', updated_at=NOW-60)]
    assert row['pm_move_pp'] == pytest.approx(9) and 'unchanged_line_vs_pm' in row['low_trust_reasons']
    assert not row['qualifies']

    odds['homeTeamOdds']['moneyLine'] = '-110'
    sports.tick(cfg, NOW+362, clock=lambda: 0)
    assert sports.games(feed, NOW+363)[0]['line_changed_at'] == NOW+362


def test_missing_lines_and_one_request_per_ten_seconds(fake, dbs):
    feed, _, path = dbs
    assert api.sports(feed, NOW)['edges_status']['reason'] == 'bookmaker_lines_not_collected'
    feed.execute('DROP TABLE mk_sports_lines')
    assert api.sports(feed, NOW)['edges_status'] == dict(state='unknown', reason='bookmaker_lines_table_missing')
    leagues = ('soccer/eng.1', 'soccer/esp.1')
    fake.routes = {sports.ESPN % league: dict(events=[]) for league in leagues}
    sleeps = []
    sports.tick(dict(markets_db=path/'feed.db', leagues=leagues), NOW, sleep=sleeps.append, clock=lambda: 0)
    assert sleeps == [10]
    assert [url for url, _ in fake.calls] == [sports.ESPN % league + '?dates=20261126' for league in leagues]


def test_legacy_sports_lines_migrate_and_first_observation_is_low_trust(fake, dbs):
    feed, _, path = dbs
    feed.executescript("""
        DROP TABLE mk_sports_lines;
        CREATE TABLE mk_sports_lines(game_id TEXT PRIMARY KEY, fair_away REAL, fair_draw REAL,
          bookmaker_n INTEGER NOT NULL, home_aliases TEXT NOT NULL, away_aliases TEXT NOT NULL, fetched_at REAL NOT NULL);
        INSERT INTO mk_sports_lines VALUES ('legacy',.3,.2,1,'[]','[]',1);
    """)
    assert api.sports(feed, NOW)['edges_status']['reason'] == 'bookmaker_lines_schema_old'
    fixture = game()
    odds = fixture['competitions'][0]['odds']
    odds.append(dict(odds[0], provider=dict(name='Book 2')))
    fake.routes = {sports.ESPN % 'soccer/uefa.europa': dict(events=[fixture])}
    cfg = dict(markets_db=path/'feed.db', leagues=['soccer/uefa.europa'])
    assert sports.tick(cfg, NOW, clock=lambda: 0) == 'games=1'
    assert api.sports(feed, NOW)['edges_status']['state'] == 'known'
    add_market(feed, 'm', 'Will Plzeň win on 2026-11-26?', yes=.32, no=.69)
    edge = sports.edges(feed, NOW+5)[0]
    assert edge['bookmaker_n'] == 2 and edge['gross_gap_pp'] > edge['cost_hurdle_pp']
    assert 'line_age_unknown' in edge['low_trust_reasons'] and not edge['qualifies']
    assert edge['pm_move_pp'] is None

    sports.tick(cfg, NOW+340, clock=lambda: 0)
    add_market(feed, 'later', 'Will Plzeň win on 2026-11-26?', yes=.32, no=.69, at=NOW+340)
    edge = sports.edges(feed, NOW+345)[0]
    assert edge['line_changed_at'] == NOW and edge['qualifies'] and edge['pm_move_pp'] is None


def test_single_book_paper_override_preserves_warning_and_hard_vetoes(fake, dbs):
    feed, _, path = dbs
    fake.routes = {sports.ESPN % 'soccer/uefa.europa': dict(events=[game()])}
    cfg = dict(markets_db=path/'feed.db', leagues=['soccer/uefa.europa'])
    sports.tick(cfg, NOW-340, clock=lambda: 0)
    sports.tick(cfg, NOW, clock=lambda: 0)
    add_market(feed, 'paper', 'Will Plzeň win on 2026-11-26?', yes=.32, no=.69)
    assert not sports.edges(feed, NOW+5)[0]['qualifies']
    sports.tick(dict(cfg, min_bookmakers=1), NOW, clock=lambda: 0)
    edge = sports.edges(feed, NOW+5)[0]
    assert edge['qualifies'] and edge['low_trust_reasons'] == ['single_book']
    assert edge['low_trust'] and edge['min_bookmakers'] == 1

    sports.tick(dict(cfg, min_bookmakers=1), NOW+600, clock=lambda: 0)
    add_market(feed, 'gap', 'Will Plzeň win on 2026-11-26?', yes=.32, no=.69, at=NOW+600)
    edge = sports.edges(feed, NOW+605)[0]
    assert not edge['qualifies'] and set(edge['low_trust_reasons']) == {'line_age_unknown', 'single_book'}
