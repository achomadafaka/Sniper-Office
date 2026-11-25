"""Made-up data for the floor's newer rooms: the markets feed and ledger, the J7 lane (signals, 1 s ticks, live PnL rows, an open order) and the services' small databases.
Used by tests/test_floor_routes.py; `python3 tests/floor_fixture.py DIR` seeds a folder (add --tick to keep writing one tick a second) for a hand-run page."""
import json
from pathlib import Path
import sqlite3
import sys
import time

from sniper_office.markets import common as MC
from sniper_office.services import narrative as NA, wallet_analyzer as WA, xcap as XC

MINT = 'EKAYmcq1Y1Kt2bWPJ5xLX3SpQ3d5jy2zYJB6VcnKS4nL'
MINT2 = 'As3XtYSstuVdrVyb6sJZZ77Tu8JvjTBg6S6hgPv4pump'
HOSTILE = '<img src=x onerror=alert(1)>'
J7_DDL = '''CREATE TABLE j7_signals(signal_id TEXT PRIMARY KEY, kind TEXT NOT NULL, trader_id TEXT NOT NULL, handle TEXT, chain TEXT NOT NULL, network_id TEXT, token TEXT NOT NULL, symbol TEXT,
  side TEXT NOT NULL, usd REAL, price_usd REAL, token_qty REAL, native_amount REAL, mcap_usd REAL, holding_usd REAL, pnl_usd REAL, pnl_pct REAL, text TEXT, tx_ref TEXT, trade_at REAL, j7_at REAL,
  recv_at REAL NOT NULL, account INTEGER, ticket_id TEXT, watch_until REAL, outcome_json TEXT);
CREATE TABLE ticks(id INTEGER PRIMARY KEY, at REAL NOT NULL, chain TEXT NOT NULL, token TEXT NOT NULL, pair TEXT, price_usd REAL NOT NULL, liquidity_usd REAL, source TEXT NOT NULL, changed INTEGER NOT NULL);'''
LIVE_DDL = '''CREATE TABLE desk_live_pnl(book_id TEXT NOT NULL, order_id TEXT NOT NULL, at REAL NOT NULL, chain TEXT, mint TEXT, state TEXT, price_usd REAL, tick_age_s REAL, qty REAL, cost_usd REAL,
  value_usd REAL, unrealized_usd REAL, unrealized_pct REAL, realized_usd REAL, equity_usd REAL, initial_usd REAL, total_usd REAL, open_n INTEGER, unpriced_n INTEGER, PRIMARY KEY(book_id, order_id))'''


def price(i):
    return 0.001 * (1 + 0.02 * ((i * 7) % 11 - 5) / 5 + i * 0.0001)


def add_tick(j7, at, i, token=MINT, chain='solana'):
    j7.execute('INSERT INTO ticks(at,chain,token,pair,price_usd,liquidity_usd,source,changed) VALUES(?,?,?,?,?,?,?,?)', (at, chain, token, 'pair1', price(i), 9000., 'onchain', int(i % 3 == 0)))


def seed(folder, now, office_db=None, ticks=True):
    folder = Path(folder);folder.mkdir(parents=True, exist_ok=True)
    feed, led = MC.connect(folder / 'markets.db', MC.FEED_SCHEMA), MC.connect(folder / 'markets_ledger.db', MC.LEDGER_SCHEMA)
    for i, (title, rooms, place) in enumerate((('Port strike widens in Rotterdam as talks stall', (('stocks', 62), ('polymarket', 41)), ('NL', 'Netherlands', 52.1, 5.3)),
                                               ('New dog coin Kapi Pla Ra trends on social media', (('memes', 71),), None), (HOSTILE + ' central bank holds rates', (('perps', 55), ('crypto', 48)), ('US', 'United States', 38., -97.)),
                                               ('NBA: Celtics beat Knicks in overtime', (('sports', 80),), ('US', 'United States', 38., -97.)))):
        nid = 'n%d' % i
        feed.execute('INSERT INTO mk_news(news_id,source,url,title,published,fetched_at) VALUES(?,?,?,?,?,?)', (nid, 'wire%d' % (i % 2), 'https://example.org/%d' % i, title, now - 300 - i * 120, now - 200 - i * 100))
        for room, score in rooms:
            feed.execute('INSERT INTO mk_routes(news_id,room,score,reason,classifier,meme_name,meme_ticker,routed_at) VALUES(?,?,?,?,?,?,?,?)', (nid, room, score, 'keywords', 'rule', 'Kapi Pla Ra' if room == 'memes' else None, 'PlaRa' if room == 'memes' else None, now - 190 - i * 100))
        if place:
            feed.execute('INSERT INTO mk_story_places VALUES(?,?,?,?,?,?,?)', (nid, place[0], place[1], place[2], place[3], 'rule', now - 190))
    feed.execute("INSERT INTO mk_sports_games(game_id,league,start_ts,home,away,state,home_imp,away_imp,fair_home,odds_provider,home_score,away_score,source,fetched_at) VALUES('g1','nba',?,'Celtics','Knicks','in',.6,.45,.57,'book',88,84,'espn',?)", (now - 3000, now - 30))
    feed.execute("INSERT INTO mk_pm_markets(condition_id,question,slug,room,end_ts,yes_token,no_token,fee_rate,fee_exp,closed,accepting,source,fetched_at) VALUES('c1','Will the strike end by Friday?','s','polymarket',?,'ty','tn',.05,1,0,1,'gamma',?)", (now + 86400, now - 20))
    feed.execute("INSERT INTO mk_pm_quotes(token_id,condition_id,bid,bid_size,ask,ask_size,source,fetched_at) VALUES('ty','c1',.40,500,.42,500,'clob',?)", (now - 5,))
    feed.execute("INSERT INTO mk_perp_ticks(venue,coin,mark,mid,oracle,funding,funding_h,oi,day_vol,prev_day,max_leverage,source,fetched_at) VALUES('hl','BTC',60500,60500,60500,.0001,1,1,1,60000,40,'hl',?)", (now - 3,))
    feed.execute("INSERT INTO mk_stock_quotes(symbol,price,quote_ts,prev_close,day_high,day_low,volume,regular_start,regular_end,source,fetched_at) VALUES('NVDA',130.5,?,128,131,127,1000,?,?,'yahoo',?)", (now - 5, now - 3600, now + 3600, now - 5))
    for f, ok in (('news', 1), ('polymarket', 1), ('perps', 1), ('stocks', 0), ('sports', 1), ('book_engine', 1)):
        feed.execute('INSERT INTO mk_feed_status VALUES(?,?,?,?)', (f, now - 10, ok, 'detail %s' % f))
    for book, lane, role in (('pm-pm_trader', 'polymarket', 'pm_trader'), ('perps-perps_trader', 'perps', 'perps_trader'), ('stocks-stocks_trader', 'stocks', 'stocks_trader')):
        led.execute('INSERT INTO bk_books(book_id,lane,role,start_cash,cash,created_at) VALUES(?,?,?,1000,900,?)', (book, lane, role, now - 9000))
    led.execute("INSERT INTO bk_positions VALUES('p1','pm-pm_trader','polymarket','ty','Strike ends by Friday','long',200,.40,?,1,80,80,NULL,.6,.2,'t','d','e',NULL,NULL,'n0',0,0,NULL,'open',NULL,NULL,NULL,NULL,NULL,NULL,NULL,'{\"fee_rate\":0.05,\"fee_exp\":1}','{}')", (now - 600,))
    led.execute("INSERT INTO bk_positions VALUES('p2','perps-perps_trader','perps','hl:BTC','BTC','long',.001,60000,?,5,12,60,48000,66000,57000,'t','d','e',NULL,NULL,NULL,0,0,NULL,'open',NULL,NULL,NULL,NULL,NULL,NULL,NULL,'{}','{}')", (now - 500,))
    led.execute("INSERT INTO bk_events(book_id,pos_id,at,kind,detail_json) VALUES('pm-pm_trader','p1',?,'open','{}')", (now - 600,))
    led.execute("INSERT INTO bk_debates(news_id,role,stance,text,at) VALUES('n0','pm_critic','bear',?,?)", (HOSTILE + ' weak overlap', now - 150))
    led.execute("INSERT INTO bk_explorer(role,at,topic,why_this,notebook) VALUES('explorer',?,'Port logistics','free hour','notes')", (now - 400,))
    led.execute("INSERT INTO bk_lessons(role,by_role,at,lesson) VALUES('pm_trader','coach',?,'Size down when the book is thin')", (now - 300,))
    led.execute("INSERT INTO bk_skills(skill,by_role,at,steps,why) VALUES('read-the-book','coach',?,'1. look','spreads')", (now - 300,))
    led.execute("INSERT INTO bk_video_rules VALUES('v1','coach','https://example.org/v','Gap fade','fade gaps over 1%','NQ','under_test',NULL,?)", (now - 300,))
    led.execute("INSERT INTO bk_operator_chat(at,sender,to_role,text) VALUES(?,'operator','pm_trader','tighten stops')", (now - 100,))
    for c in (feed, led):
        c.close()
    # J7 lane
    j7 = sqlite3.connect(folder / 'j7.db', isolation_level=None);j7.executescript(J7_DDL)
    for i, (sid, tok, sym, side) in enumerate((('s1', MINT, 'PlaRa', 'buy'), ('s2', MINT2, None, 'buy'), ('s3', MINT, 'PlaRa', 'sell'))):
        j7.execute('INSERT INTO j7_signals(signal_id,kind,trader_id,handle,chain,network_id,token,symbol,side,usd,price_usd,mcap_usd,recv_at,trade_at,ticket_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (sid, 'trade', 'u1', 'trader%d' % i, 'solana', 'solana', tok, sym, side, 500. + i, .001, 90000., now - 600 + i * 100, now - 601 + i * 100, 't%d' % i))
    if ticks:
        for i in range(300):
            add_tick(j7, now - 300 + i, i)
    j7.close()
    office = sqlite3.connect(office_db or folder / 'office.db', isolation_level=None);office.executescript(LIVE_DDL)
    office.execute("INSERT INTO desk_live_pnl(book_id,order_id,at,realized_usd,unrealized_usd,equity_usd,initial_usd,total_usd,open_n,unpriced_n) VALUES('office-v2-copy-1','',?,0,12.5,1012.5,1000,12.5,1,0)", (now,))
    office.execute("INSERT INTO desk_live_pnl(book_id,order_id,at,chain,mint,state,price_usd,tick_age_s,qty,cost_usd,value_usd,unrealized_usd,unrealized_pct) VALUES('office-v2-copy-1','o1',?,'solana',?,'open',.00105,.4,100000,100,112.5,12.5,12.5)", (now, MINT))
    office.close()
    ledger = sqlite3.connect(folder / 'ledger.db', isolation_level=None)
    ledger.executescript('''CREATE TABLE orders(order_id TEXT PRIMARY KEY, book_id TEXT NOT NULL, epoch_id TEXT NOT NULL, network_id TEXT, mint TEXT NOT NULL, pool TEXT, state TEXT NOT NULL, reason TEXT, requested_at REAL NOT NULL,
      filled_at REAL, closed_at REAL, spend_usd REAL, exit_usd REAL, costs_usd REAL NOT NULL DEFAULT 0, exit_reason TEXT, exit_fill_kind TEXT, exit_requested_at REAL, qty REAL, entry_price_usd REAL, peak_value_usd REAL, policy_json TEXT, fill_proof TEXT, exit_proof TEXT);''')
    ledger.execute("INSERT INTO orders(order_id,book_id,epoch_id,network_id,mint,state,requested_at,filled_at,spend_usd,costs_usd,qty,entry_price_usd,policy_json) VALUES('o1','office-v2-copy-1','e','solana',?,'open',?,?,100,1,100000,.001,?)",
                   (MINT, now - 200, now - 190, json.dumps(dict(sl_pct=30., tp_pct=100., trail_pct=20.))))
    ledger.close()
    # services
    n = sqlite3.connect(folder / 'narrative.db', isolation_level=None)
    n.executescript(NA.DDL)
    n.execute("INSERT INTO launches VALUES('solana',?,'Biaoqing','BIAO',?,'pump')", (MINT2, now - 900))
    n.execute("INSERT INTO guesses VALUES('g1','c1','news',?,?,?,'t1','dog memes are trending',70,'[\"PLARA\"]','[\"Kapi Pla Ra\"]','r1','narrative')", (now - 1000, now - 990, now + 3600))
    n.execute("INSERT INTO matches VALUES('solana',?,'news','g1','fuzzy','ok','h',?,10)", (MINT2, now - 800))
    n.close()
    x = sqlite3.connect(folder / 'x.db', isolation_level=None)
    x.executescript(XC.SCHEMA)
    x.execute("INSERT INTO x_posts VALUES('p1','https://x.example/p1','someone','$PLARA to the moon ' || ?,1,NULL,'[]',1,?,'host','r1')", (HOSTILE, now - 500))
    x.execute("INSERT INTO x_mentions VALUES('p1','solana',?,'PLARA','cashtag',0,1,?,'host','r1')", (MINT, now - 500))
    x.close()
    w = sqlite3.connect(folder / 'wallet_network.db', isolation_level=None)
    w.executescript(WA.SCHEMA)
    w.execute("INSERT INTO wallet_findings VALUES('f1','0xwallet','r1','w1','no_edge','info','h1','No edge found in the closed positions',?,NULL)", (now - 700,))
    w.close()
    (folder / 'guard.json').write_text(json.dumps(dict(state='ready', reason=None, at=now - 2, guard_policy_version='office-host-guard-v1')))


if __name__ == '__main__':
    folder, tick = sys.argv[1], '--tick' in sys.argv
    seed(folder, time.time())
    if tick:
        j7, i = sqlite3.connect(Path(folder) / 'j7.db', isolation_level=None), 300
        while True:
            add_tick(j7, time.time(), i);i += 1
            time.sleep(1)
