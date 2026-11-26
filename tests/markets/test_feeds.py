"""Feeds against fakes: parse, store with source and fetch time, never invent; one bad host never poisons the rest."""
import json

import pytest

from conftest import COND, NO, NOW, YES, put_market
from sniper_office.markets import common as C, intel_hl as IH, news as NW, perps as PP, polymarket as PM, sports as SP, stocks as ST

GAMMA = 'https://gamma-api.polymarket.com/markets'


def gamma_market(**kw):
    return dict(conditionId=COND, question='Will the Fed cut rates in March?', slug='fed', endDate='2027-06-01T00:00:00Z', clobTokenIds=json.dumps([YES, NO]), outcomes='["Yes","No"]', outcomePrices='["0.5","0.5"]',
                bestBid=0.48, bestAsk=0.50, volume24hr=9000.0, liquidityNum=5000.0, closed=False, active=True, acceptingOrders=True, description='rules', **kw)


def book(token, bid, ask):
    return dict(asset_id=token, bids=[dict(price=str(bid - .01), size='10'), dict(price=str(bid), size='100')], asks=[dict(price=str(ask + .01), size='10'), dict(price=str(ask), size='80')])


def test_parse_market_and_book():
    cfg = {**PM.DEFAULTS}
    row = PM.parse_market(gamma_market(), cfg, NOW)
    assert row['yes_token'] == YES and row['no_token'] == NO and row['fee_source'] == 'config_default' and row['fee_rate'] == 0.05 and row['resolved_yes'] is None
    assert PM.parse_market(gamma_market(feeSchedule=dict(rate=0.07, exponent=1)), cfg, NOW)['fee_source'] == 'gamma_feeSchedule'
    assert PM.parse_market(dict(gamma_market(), outcomes='["A","B"]'), cfg, NOW) is None                    # not Yes/No
    assert PM.parse_market(dict(gamma_market(), clobTokenIds='["x"]'), cfg, NOW) is None
    done = PM.parse_market(dict(gamma_market(), closed=True, outcomePrices='["1","0"]'), cfg, NOW)
    assert done['resolved_yes'] == 1.0 and done['accepting'] == 0
    assert PM.parse_market(dict(gamma_market(), closed=True, outcomePrices='["0.62","0.38"]'), cfg, NOW)['resolved_yes'] is None      # not resolved
    assert PM.parse_book(book(YES, .48, .50), YES) == (0.48, 100.0, 0.50, 80.0)
    assert PM.parse_book(book('other', .48, .50), YES) is None                                              # wrong token
    assert PM.parse_book(book(YES, .52, .50), YES) is None                                                  # crossed
    assert PM.parse_book(dict(asset_id=YES, bids=[], asks=[dict(price='0.5', size='5')]), YES) is None       # one-sided: never a zero


def test_polymarket_tick_stores_markets_books_and_held_tokens(fake, dbs):
    feed, led, tmp = dbs
    C.connect(tmp / 'ledger.db', C.LEDGER_SCHEMA).execute("INSERT INTO bk_positions(pos_id,book_id,lane,instrument,side,qty,entry_price,entry_at,leverage,margin,notional,status,meta_json) VALUES('p','b','polymarket','heldtok','YES',1,.5,1,1,.5,.5,'open',?)", (json.dumps(dict(condition_id='0xheld')),))
    fake.routes = {GAMMA + '?active': [gamma_market()], GAMMA + '?condition_ids': [], GAMMA + '?closed=true&condition_ids': [dict(gamma_market(), conditionId='0xheld', clobTokenIds='["heldtok","heldno"]', closed=True, outcomePrices='["0","1"]')],
                   'https://clob.polymarket.com/books': [book(YES, .48, .50), book(NO, .50, .52), book('heldtok', .01, .03), book('heldno', .97, .99)]}
    cfg = dict(markets_db=str(tmp / 'feed.db'), ledger_db=str(tmp / 'ledger.db'), gap_s=0)
    detail = PM.tick(cfg, NOW, sleep=lambda s: None)
    assert 'markets=2' in detail and 'books_ok=4' in detail
    q = feed.execute('SELECT * FROM mk_pm_quotes WHERE token_id=?', (YES,)).fetchone()
    assert (q['bid'], q['ask'], q['source'], q['fetched_at']) == (0.48, 0.50, 'clob:book', NOW)
    held = feed.execute("SELECT closed,resolved_yes FROM mk_pm_markets WHERE condition_id='0xheld'").fetchone()
    assert (held['closed'], held['resolved_yes']) == (1, 0.0)
    assert feed.execute("SELECT room FROM mk_pm_markets WHERE condition_id=?", (COND,)).fetchone()[0] == 'perps'      # routed by the story router's rules


def test_polymarket_tick_pages_gamma_and_dedups(fake, dbs):
    _, _, tmp = dbs
    fake.routes = {GAMMA + '?active': [gamma_market()], 'https://clob.polymarket.com/books': [book(YES, .48, .50), book(NO, .50, .52)]}
    detail = PM.tick(dict(markets_db=str(tmp / 'feed.db'), ledger_db=str(tmp / 'ledger.db'), gap_s=0, pages=3), NOW, sleep=lambda s: None)
    offsets = [u.split('offset=')[1] for u, _ in fake.calls if u.startswith(GAMMA + '?active')]
    assert offsets == ['0', '100', '200'] and 'markets=1' in detail


def test_polymarket_tick_adds_tagged_events_and_tracker_counts(fake, dbs):
    feed, _, tmp = dbs
    hot = dict(gamma_market(), conditionId='0xtemp', question='Will the highest temperature in Seoul be 8°C on November 26?', clobTokenIds='["ty","tn"]', bestAsk=0.30, bestBid=0.28, volume24hr=600.0, endDate='2099-01-01T00:00:00Z')
    dead = dict(hot, conditionId='0xdead', bestAsk=0.001, bestBid=0.0)                   # a bucket outside 3-97 cents: orders refuse it, so the feed skips it
    fake.routes = {GAMMA + '?active': [gamma_market()], 'https://gamma-api.polymarket.com/events?tag_slug=daily-temperature': [dict(title='Seoul', markets=[hot, dead])],
                   'https://xtracker.polymarket.com/api/users/elonmusk/trackings': dict(data=[dict(id='w1', title='Elon Musk # tweets', startDate='2026-11-20T17:00:00Z', endDate='2099-01-01T00:00:00Z', isActive=True, marketLink='https://polymarket.com/event/x')]),
                   'https://xtracker.polymarket.com/api/trackings/w1': dict(data=dict(stats=dict(total=234, pace=273, daily=[dict(date='2026-11-20T17:00:00Z', count=5)]))),
                   'https://clob.polymarket.com/books': [book(YES, .48, .50), book('ty', .28, .30)]}
    detail = PM.tick(dict(markets_db=str(tmp / 'feed.db'), ledger_db=str(tmp / 'ledger.db'), gap_s=0, event_tags=['daily-temperature'], trackers=['elonmusk']), NOW, sleep=lambda s: None)
    got = {r[0] for r in feed.execute('SELECT condition_id FROM mk_pm_markets')}
    assert 'markets=2' in detail and got == {COND, '0xtemp'}
    t = feed.execute('SELECT * FROM mk_pm_trackers').fetchone()
    assert (t['tracking_id'], t['active'], t['total'], t['pace'], json.loads(t['hourly_json'])) == ('w1', 1, 234, 273, [['2026-11-20T17:00:00Z', 5]])


def test_polymarket_tick_gamma_down_raises_and_records(fake, dbs):
    feed, _, tmp = dbs
    fake.routes = {GAMMA: OSError('down')}
    with pytest.raises(OSError):
        PM.tick(dict(markets_db=str(tmp / 'feed.db')), NOW, sleep=lambda s: None)
    r = feed.execute("SELECT ok,detail FROM mk_feed_status WHERE feed='polymarket'").fetchone()
    assert r['ok'] == 0 and 'down' in r['detail'] and feed.execute('SELECT COUNT(*) FROM mk_pm_quotes').fetchone()[0] == 0


HL_ANSWER = [dict(universe=[dict(name='BTC', maxLeverage=40), dict(name='ETH', maxLeverage=25), dict(name='OLD', maxLeverage=3, isDelisted=True), dict(name='ZZZ', maxLeverage=5)]),
             [dict(funding='0.0000125', openInterest='100', prevDayPx='99', dayNtlVlm='5e9', premium='0', oraclePx='100.5', markPx='100.4', midPx='100.45'),
              dict(funding='-0.00001', openInterest='50', prevDayPx='10', dayNtlVlm='1e9', oraclePx='10', markPx='10', midPx=None),
              dict(funding='0', openInterest='1', prevDayPx='1', dayNtlVlm='1', oraclePx='1', markPx='1', midPx='1'), dict(funding='0', openInterest='1', prevDayPx='1', dayNtlVlm='5', oraclePx='1', markPx='1', midPx='1')]]


def test_perps_parse_hl_and_tick(fake, dbs):
    feed, _, tmp = dbs
    rows = PP.parse_hl(HL_ANSWER, NOW, 2, ['ZZZ'])
    assert {r['coin'] for r in rows} == {'BTC', 'ETH', 'ZZZ'}                                               # top 2 by volume plus the pinned one, delisted skipped
    btc = next(r for r in rows if r['coin'] == 'BTC')
    assert (btc['mid'], btc['funding'], btc['funding_h'], btc['max_leverage'], btc['fetched_at']) == (100.45, 1.25e-5, 1.0, 40.0, NOW)
    assert next(r for r in rows if r['coin'] == 'ETH')['mid'] is None                                       # unknown stays NULL
    fake.routes = {'https://api.hyperliquid.xyz/info': HL_ANSWER}
    assert PP.tick(dict(markets_db=str(tmp / 'feed.db'), top_n=2, coins=['ZZZ']), NOW) == 'ticks=3'
    assert feed.execute('SELECT COUNT(*) FROM mk_perp_ticks').fetchone()[0] == 3


def test_perps_binance_venue(fake, dbs):
    feed, _, tmp = dbs
    base = 'https://fapi.binance.com/fapi/v1/'
    fake.routes = {base + 'ticker/24hr': [dict(symbol='BTCUSDT', quoteVolume='9e9'), dict(symbol='BTCBUSD', quoteVolume='1')], base + 'premiumIndex': [dict(symbol='BTCUSDT', markPrice='100', indexPrice='99.9', lastFundingRate='0.0001')],
                   base + 'ticker/bookTicker': [dict(symbol='BTCUSDT', bidPrice='99.9', askPrice='100.1')]}
    PP.tick(dict(markets_db=str(tmp / 'feed.db'), venues=['binance'], coins=['BTC']), NOW)
    r = feed.execute('SELECT * FROM mk_perp_ticks').fetchone()
    assert (r['venue'], r['coin'], r['mid'], r['funding_h'], r['max_leverage']) == ('binance', 'BTCUSDT', 100.0, 8.0, None)


def test_perps_tick_all_venues_down_raises(fake, dbs):
    _, _, tmp = dbs
    fake.routes = {'https://api.hyperliquid.xyz/info': OSError('x')}
    with pytest.raises(RuntimeError):
        PP.tick(dict(markets_db=str(tmp / 'feed.db')), NOW)


def candle(coin, start, close='101.5'):
    return [dict(s=coin, i='1h', t=start * 1000, T=(start + 3600) * 1000 - 1, o='100', h='102', l='99', c=close, v='5')]


def test_candle_recorder_ok_pending_missed_and_validation(fake, dbs):
    feed, _, tmp = dbs
    start = (int(NOW) // 3600 - 1) * 3600
    t_ok = start + 3600 + 20
    cfg = dict(markets_db=str(tmp / 'feed.db'), coins=['BTC'])
    assert IH.valid_bar(candle('BTC', start), 'BTC', start) == [100.0, 102.0, 99.0, 101.5, 5.0]
    assert IH.valid_bar(candle('ETH', start), 'BTC', start) is None and IH.valid_bar(candle('BTC', start + 3600), 'BTC', start) is None and IH.valid_bar(candle('BTC', start, close='x'), 'BTC', start) is None
    assert IH.valid_bar(candle('BTC', start) * 2, 'BTC', start) is None
    assert 'nothing_due' in IH.tick(cfg, start + 3600 + 5)                                                  # before the 15 s delay
    fake.routes = {'https://api.hyperliquid.xyz/info': [[]]}
    fake.routes = {'https://api.hyperliquid.xyz/info': []}                                                  # venue answers no bar yet
    assert 'pending' in IH.tick(cfg, t_ok)
    fake.routes = {'https://api.hyperliquid.xyz/info': candle('BTC', start)}
    assert IH.tick(cfg, t_ok + 1) .endswith('BTC ok')
    assert 'nothing_due' in IH.tick(cfg, t_ok + 2)                                                          # recorded once
    c = feed.execute('SELECT * FROM mk_perp_candles').fetchone()
    assert (c['status'], c['close'], c['start_ts'], c['source']) == ('ok', 101.5, start, 'hyperliquid:candleSnapshot')
    feed.execute('DELETE FROM mk_perp_candles')
    fake.routes = {'https://api.hyperliquid.xyz/info': OSError('x')}
    assert IH.tick(cfg, start + 3600 + 1000).endswith('BTC missed')
    m = feed.execute('SELECT * FROM mk_perp_candles').fetchone()
    assert m['status'] == 'missed' and m['close'] is None                                                   # a gap is recorded as a gap


YAHOO = 'https://query1.finance.yahoo.com/v8/finance/chart/'


def yahoo(sym='NVDA', price=101.0, t=NOW - 5):
    return dict(chart=dict(result=[dict(meta=dict(symbol=sym, regularMarketPrice=price, regularMarketTime=t, chartPreviousClose=99.0, regularMarketDayHigh=102.0, regularMarketDayLow=98.0, regularMarketVolume=1e6,
                                                  currentTradingPeriod=dict(regular=dict(start=NOW - 3600, end=NOW + 3600))))]))


def test_stocks_parse_and_tick(fake, dbs):
    feed, _, tmp = dbs
    q = ST.parse_quote(yahoo(), 'NVDA', NOW)
    assert (q['price'], q['quote_ts'], q['regular_end'], q['source']) == (101.0, NOW - 5, NOW + 3600, 'yahoo:chart')
    assert ST.parse_quote(yahoo(sym='AMD'), 'NVDA', NOW) is None and ST.parse_quote(yahoo(price=0), 'NVDA', NOW) is None and ST.parse_quote({}, 'NVDA', NOW) is None
    fake.routes = {YAHOO + 'NVDA': yahoo(), YAHOO + 'SPY': OSError('429'),
                   'https://scanner.tradingview.com/america/scan': dict(data=[dict(d=['NVDA', 101.0, 1.2, 0.1, 0.0, 55, 0.3, 2.1, 100, 3, 99, 98, 102, 98, 'Tech']), dict(d=['ZZZZ', 20.0, 5, 0, 0, 70, 0.5, 4.0, 19, 1, 18, 17, 21, 19, 'Fin'])])}
    fake.routes[YAHOO + 'ZZZZ'] = yahoo('ZZZZ', 20.0)
    detail = ST.tick(dict(markets_db=str(tmp / 'feed.db'), symbols=['NVDA', 'SPY']), NOW, sleep=lambda s: None)
    assert detail.startswith('quotes=2 of 3') and feed.execute('SELECT COUNT(*) FROM mk_stock_screen').fetchone()[0] == 2     # SPY's failure is a gap, the screener adds ZZZZ
    assert json.loads(feed.execute("SELECT data_json FROM mk_stock_screen WHERE symbol='NVDA'").fetchone()[0])['relative_volume_10d_calc'] == 2.1


RSS = b'''<?xml version="1.0"?><rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>
<item><title>Bitcoin ETF sees record inflows as BTC surges</title><link>https://ex.example/a</link><guid>g1</guid><pubDate>Tue, 01 Dec 2026 10:00:00 GMT</pubDate><description>&lt;p&gt;Crypto &amp;amp; more&lt;/p&gt;</description></item>
<item><title>Viral hippo escapes zoo, internet renames it</title><link>https://ex.example/b</link><guid>g2</guid></item>
<item><title></title><link>https://ex.example/empty</link></item></channel></rss>'''
ATOM = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Fed signals rate cut at next FOMC meeting</title><link rel="alternate" href="https://ex.example/c"/><id>tag:c</id><updated>2026-12-01T10:00:00Z</updated><summary>Inflation cooling</summary></entry></feed>'''


def test_rss_atom_parse_and_hostile_input():
    items = NW.parse(RSS)
    assert [i[1] for i in items] == ['Bitcoin ETF sees record inflows as BTC surges', 'Viral hippo escapes zoo, internet renames it'] and items[0][3] == 'Crypto & more' and items[0][4] is not None
    assert items[1][4] is None                                                                              # no publisher stamp: NULL, never our clock
    a = NW.parse(ATOM)
    assert a[0][2] == 'https://ex.example/c' and a[0][4] == pytest.approx(1796119200.0)
    assert NW.parse(b'not xml') == [] and NW.parse(b'<!DOCTYPE x [<!ENTITY a "b">]><rss><channel><item><title>&a;</title></item></channel></rss>') == []


def test_any_dtd_or_entity_rejects_the_feed_in_any_encoding_and_position():
    body = '<rss><channel><item><title>Bitcoin up</title></item></channel></rss>'
    assert len(NW.parse(body.encode())) == 1
    ent = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE r [<!ENTITY a "b">]><rss><channel><item><title>&a;</title></item></channel></rss>'
    assert NW.parse(ent.encode('utf-16')) == []                                                   # UTF-16: a byte search for b'<!ENTITY' sees nothing
    late = '<?xml version="1.0"?><!-- ' + 'x' * 3000 + ' --><!DOCTYPE rss><rss><channel><item><title>Bitcoin up</title></item></channel></rss>'
    assert NW.parse(late.encode()) == []                                                          # DOCTYPE after 2 KB
    inner = '<rss><channel><item><title>t</title></item></channel></rss>'
    assert NW.parse(('<!DOCTYPE rss [<!ENTITY % p "x">]>' + inner).encode()) == []
    assert NW.parse(b'\xff\xfe garbage') == []


def test_news_tick_ingests_routes_dedups_and_polls_on_schedule(fake, dbs):
    feed, _, tmp = dbs
    put_market(feed)
    for i in range(12):      # a market list of realistic size, so a shared word is rare
        feed.execute("INSERT INTO mk_pm_markets(condition_id,question,yes_token,no_token,closed,accepting,source,fetched_at) VALUES(?,?,?,?,0,1,'t',?)", ('0xf%d' % i, 'Will team%d win the title%d?' % (i, i), 'y%d' % i, 'n%d' % i, NOW))
    feeds = [['coindesk', 'https://ex.example/rss1', 60], ['bbc_world', 'https://ex.example/rss2', 60], ['broken', 'https://ex.example/rss3', 60], ['badurl', 'http://plain.example/rss', 60]]
    fake.routes = {'https://ex.example/rss1': RSS, 'https://ex.example/rss2': ATOM + b'', 'https://ex.example/rss3': OSError('boom')}
    cfg = dict(markets_db=str(tmp / 'feed.db'), feeds=feeds)
    assert NW.tick(cfg, NOW) == 'feeds_ok=2 feeds_error=2 new_stories=3'
    assert feed.execute('SELECT COUNT(*) FROM mk_news').fetchone()[0] == 3
    assert {r['room'] for r in feed.execute("SELECT room FROM mk_routes r JOIN mk_news n USING(news_id) WHERE n.title LIKE 'Bitcoin%'")} >= {'crypto'}
    assert {r['room'] for r in feed.execute("SELECT room FROM mk_routes r JOIN mk_news n USING(news_id) WHERE n.title LIKE 'Fed%'")} >= {'perps', 'polymarket'}     # macro, and overlaps the open Fed market
    hippo = feed.execute("SELECT * FROM mk_routes WHERE room='memes'").fetchone()
    assert hippo['meme_name'] and hippo['meme_ticker']
    errs = [r['error'] for r in feed.execute("SELECT error FROM mk_news_polls WHERE status='error'")]
    assert any('boom' in e for e in errs) and any('url_refused' in e for e in errs)
    n_calls = len(fake.calls)
    assert NW.tick(cfg, NOW + 10) == 'feeds_ok=0 feeds_error=0 new_stories=0' and len(fake.calls) == n_calls          # inside poll_s: no fetch
    fake.routes['https://ex.example/rss2'] = RSS      # the same headline from a second source is stored once as a duplicate and routed once
    fake.routes['https://ex.example/rss1'] = ATOM
    NW.tick(cfg, NOW + 100)
    dup = feed.execute('SELECT dup_of FROM mk_news WHERE source=? AND title LIKE ?', ('bbc_world', 'Bitcoin%')).fetchone()
    assert dup['dup_of'] and feed.execute('SELECT COUNT(*) FROM mk_routes WHERE news_id IN (SELECT news_id FROM mk_news WHERE dup_of IS NOT NULL)').fetchone()[0] == 0


def test_sports_moneyline_devig_and_edges(fake, dbs):
    feed, _, tmp = dbs
    assert SP.ml('-150') == pytest.approx(0.6) and SP.ml('+130') == pytest.approx(100 / 230) and SP.ml('EVEN') == 0.5 and SP.ml('x') is None
    espn = dict(events=[dict(id='1', date='2026-12-01T00:00Z', status=dict(type=dict(state='pre')), competitions=[dict(competitors=[dict(homeAway='home', team=dict(displayName='Boston Celtics'), score='0'),
            dict(homeAway='away', team=dict(displayName='Miami Heat'), score='0')], odds=[dict(provider=dict(name='Book'), homeTeamOdds=dict(moneyLine='-200'), awayTeamOdds=dict(moneyLine='+170'))])]),
            dict(id='2', competitions=[])])
    fake.routes = {'https://site.api.espn.com/apis/site/v2/sports/': espn}
    assert SP.tick(dict(markets_db=str(tmp / 'feed.db'), leagues=['basketball/nba']), NOW) == 'games=1'
    g = feed.execute('SELECT * FROM mk_sports_games').fetchone()
    assert g['fair_home'] == pytest.approx((2 / 3) / ((2 / 3) + 100 / 270)) and g['odds_provider'] == 'Book'
    feed.execute("INSERT INTO mk_pm_markets(condition_id,question,yes_token,no_token,closed,accepting,source,fetched_at) VALUES('c9','Will the Celtics beat the Heat?','y9','n9',0,1,'t',?)", (NOW,))
    feed.execute("INSERT INTO mk_pm_quotes(token_id,bid,ask,source,fetched_at) VALUES('y9',0.55,0.58,'clob:book',?)", (NOW,))
    e = SP.edges(feed, NOW + 10)
    assert len(e) == 1 and e[0]['gap_pp'] == pytest.approx((g['fair_home'] - 0.58) * 100, abs=0.01) and SP.edges(feed, NOW + 5000) == []


def test_http_refuses_unsafe_urls():
    for bad in ('http://x.example/a', 'https://127.0.0.1/a', 'https://localhost/a', 'https://user:pw@x.example/a', 'ftp://x.example'):
        assert not C.public_url(bad)
    assert C.public_url('https://x.example/a') and not C.public_url('https://x.example/a', C.API_HOSTS) and C.public_url('https://api.hyperliquid.xyz/info', C.API_HOSTS)
    with pytest.raises(ValueError):
        C.http('https://127.0.0.1/a')


def test_main_loop_once_runs_one_tick_and_reports_failure(tmp_path, capsys):
    cfg = tmp_path / 'c.json'
    cfg.write_text('{}')
    assert C.main_loop('t', lambda c, now: 'fine', ['--config', str(cfg), '--once']) == 0
    assert C.main_loop('t', lambda c, now: 1 / 0, ['--config', str(cfg), '--once']) == 1 and 'tick_failed ZeroDivisionError' in capsys.readouterr().err
