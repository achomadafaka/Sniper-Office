"""Shared pieces of the markets plug-in: one https fetch (no redirects, size cap, host rules), sqlite helpers, both schemas, and the host-process loop.
Paper only: nothing here holds or reads a venue key, signs, or places an order. Every stored tick carries its source and our fetch time; an unreadable value is stored as NULL, never filled."""
import argparse
import contextlib
import ipaddress
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

MAX_BODY = 2 * 1024 * 1024
UA = 'research-reader/1.0'     # some public hosts answer 403 to urllib's default
API_HOSTS = frozenset({'gamma-api.polymarket.com', 'clob.polymarket.com', 'api.hyperliquid.xyz', 'fapi.binance.com', 'query1.finance.yahoo.com',
                       'scanner.tradingview.com', 'site.api.espn.com'})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kw):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def public_url(url, allow=None):
    """True for https to a named public host (no credentials, no IP literal, no dotless name); `allow` narrows it to a host set."""
    try:
        p = urllib.parse.urlsplit(url)
        host = p.hostname
        if p.scheme != 'https' or not host or p.username or p.password or '.' not in host:
            return False
        with contextlib.suppress(ValueError):
            ipaddress.ip_address(host)
            return False
        return allow is None or host in allow
    except ValueError:
        return False


def http(url, body=None, headers=None, timeout=15.0, allow=None):
    """GET, or POST of a JSON body. Bytes, capped. Raises on a refused url, an HTTP error or an oversize body."""
    if not public_url(url, allow):
        raise ValueError('url_refused')
    data = json.dumps(body).encode() if body is not None else None
    head = {'User-Agent': UA, 'Accept': '*/*', **({'Content-Type': 'application/json'} if data is not None else {}), **(headers or {})}
    with _OPENER.open(urllib.request.Request(url, data=data, headers=head), timeout=timeout) as r:
        raw = r.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY:
        raise ValueError('body_too_large')
    return raw


FETCH = http      # tests replace this: FETCH(url, body=None) -> bytes


def get_json(url, body=None):
    return json.loads(FETCH(url, body))


def num(value):
    """A finite float or None."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float('inf') else None


FEED_SCHEMA = '''
CREATE TABLE IF NOT EXISTS mk_pm_markets(condition_id TEXT PRIMARY KEY,question TEXT,slug TEXT,room TEXT,end_ts REAL,yes_token TEXT,no_token TEXT,fee_rate REAL,fee_exp REAL,fee_source TEXT,
  volume24h REAL,liquidity REAL,closed INTEGER,accepting INTEGER,resolved_yes REAL,rules TEXT,source TEXT NOT NULL,fetched_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS mk_pm_quotes(id INTEGER PRIMARY KEY AUTOINCREMENT,token_id TEXT NOT NULL,condition_id TEXT,bid REAL,bid_size REAL,ask REAL,ask_size REAL,source TEXT NOT NULL,fetched_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS mk_pm_quotes_token ON mk_pm_quotes(token_id,fetched_at);
CREATE TABLE IF NOT EXISTS mk_pm_trackers(tracking_id TEXT PRIMARY KEY,handle TEXT,title TEXT,start_ts REAL,end_ts REAL,active INTEGER,total REAL,pace REAL,hourly_json TEXT,market_link TEXT,source TEXT NOT NULL,fetched_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS mk_perp_ticks(id INTEGER PRIMARY KEY AUTOINCREMENT,venue TEXT NOT NULL,coin TEXT NOT NULL,mark REAL,mid REAL,oracle REAL,funding REAL,funding_h REAL,oi REAL,day_vol REAL,prev_day REAL,
  max_leverage REAL,source TEXT NOT NULL,fetched_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS mk_perp_ticks_coin ON mk_perp_ticks(venue,coin,fetched_at);
CREATE TABLE IF NOT EXISTS mk_perp_candles(coin TEXT NOT NULL,interval TEXT NOT NULL,start_ts INTEGER NOT NULL,status TEXT NOT NULL,open REAL,high REAL,low REAL,close REAL,volume REAL,
  requested_at REAL,source TEXT NOT NULL,fetched_at REAL,PRIMARY KEY(coin,interval,start_ts));
CREATE TABLE IF NOT EXISTS mk_stock_quotes(id INTEGER PRIMARY KEY AUTOINCREMENT,symbol TEXT NOT NULL,price REAL,quote_ts REAL,prev_close REAL,day_high REAL,day_low REAL,volume REAL,
  regular_start REAL,regular_end REAL,source TEXT NOT NULL,fetched_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS mk_stock_quotes_sym ON mk_stock_quotes(symbol,fetched_at);
CREATE TABLE IF NOT EXISTS mk_stock_screen(id INTEGER PRIMARY KEY AUTOINCREMENT,symbol TEXT NOT NULL,data_json TEXT NOT NULL,source TEXT NOT NULL,fetched_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS mk_news(news_id TEXT PRIMARY KEY,source TEXT NOT NULL,url TEXT,title TEXT NOT NULL,summary TEXT,published REAL,fetched_at REAL NOT NULL,title_key TEXT,dup_of TEXT);
CREATE INDEX IF NOT EXISTS mk_news_time ON mk_news(fetched_at);
CREATE TABLE IF NOT EXISTS mk_news_polls(source TEXT NOT NULL,url TEXT NOT NULL,fetched_at REAL NOT NULL,status TEXT NOT NULL,items_seen INTEGER,items_new INTEGER,error TEXT);
CREATE TABLE IF NOT EXISTS mk_routes(id INTEGER PRIMARY KEY AUTOINCREMENT,news_id TEXT NOT NULL,room TEXT NOT NULL,score REAL NOT NULL,reason TEXT,classifier TEXT NOT NULL,markets_json TEXT,
  meme_name TEXT,meme_ticker TEXT,routed_at REAL NOT NULL,UNIQUE(news_id,room,classifier));
CREATE INDEX IF NOT EXISTS mk_routes_room ON mk_routes(room,routed_at);
CREATE TABLE IF NOT EXISTS mk_story_places(news_id TEXT NOT NULL,code TEXT NOT NULL,name TEXT,lat REAL,lon REAL,classifier TEXT NOT NULL,at REAL NOT NULL,PRIMARY KEY(news_id,code,classifier));
CREATE TABLE IF NOT EXISTS mk_sports_games(game_id TEXT PRIMARY KEY,league TEXT,start_ts REAL,home TEXT,away TEXT,state TEXT,home_imp REAL,away_imp REAL,fair_home REAL,odds_provider TEXT,
  home_score REAL,away_score REAL,source TEXT NOT NULL,fetched_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS mk_feed_status(feed TEXT PRIMARY KEY,at REAL NOT NULL,ok INTEGER NOT NULL,detail TEXT);
'''

LEDGER_SCHEMA = '''
CREATE TABLE IF NOT EXISTS bk_books(book_id TEXT PRIMARY KEY,lane TEXT NOT NULL,actor_id TEXT,role TEXT,start_cash REAL NOT NULL,cash REAL NOT NULL,created_at REAL NOT NULL,halted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS bk_positions(pos_id TEXT PRIMARY KEY,book_id TEXT NOT NULL,lane TEXT NOT NULL,instrument TEXT NOT NULL,label TEXT,side TEXT NOT NULL,qty REAL NOT NULL,entry_price REAL NOT NULL,
  entry_at REAL NOT NULL,leverage REAL NOT NULL,margin REAL NOT NULL,notional REAL NOT NULL,liq_price REAL,tp_price REAL,sl_price REAL,thesis TEXT,disproof TEXT,exit_plan TEXT,strategy_id TEXT,
  strategy_reason TEXT,news_id TEXT,entry_fee REAL NOT NULL DEFAULT 0,funding_paid REAL NOT NULL DEFAULT 0,last_funding_at REAL,status TEXT NOT NULL,exit_price REAL,exit_at REAL,exit_reason TEXT,
  exit_fee REAL,realized REAL,mark REAL,mark_at REAL,meta_json TEXT,proof_json TEXT);
CREATE INDEX IF NOT EXISTS bk_positions_book ON bk_positions(book_id,status);
CREATE TABLE IF NOT EXISTS bk_events(id INTEGER PRIMARY KEY AUTOINCREMENT,book_id TEXT,pos_id TEXT,at REAL NOT NULL,kind TEXT NOT NULL,detail_json TEXT);
CREATE TABLE IF NOT EXISTS bk_strategies(strategy_id TEXT PRIMARY KEY,lane TEXT NOT NULL,owner_role TEXT NOT NULL,hypothesis TEXT,entry_rule TEXT,exit_rule TEXT,kill_criterion TEXT,
  expected_triggers_per_day REAL,status TEXT NOT NULL,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS bk_debates(id INTEGER PRIMARY KEY AUTOINCREMENT,news_id TEXT NOT NULL,role TEXT NOT NULL,stance TEXT,text TEXT NOT NULL,at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS bk_calls(call_id TEXT PRIMARY KEY,news_id TEXT,role TEXT NOT NULL,condition_id TEXT NOT NULL,question TEXT,token_id TEXT NOT NULL,side TEXT NOT NULL,entry_ask REAL,
  entry_fee REAL,control_token TEXT,control_ask REAL,control_fee REAL,expected_move_cents REAL,horizon TEXT,confidence INTEGER,mechanism TEXT,reason TEXT,at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS bk_explorer(id INTEGER PRIMARY KEY AUTOINCREMENT,role TEXT NOT NULL,at REAL NOT NULL,topic TEXT,why_this TEXT,notebook TEXT);
CREATE TABLE IF NOT EXISTS bk_leads(id INTEGER PRIMARY KEY AUTOINCREMENT,note_id INTEGER NOT NULL,condition_id TEXT NOT NULL,token_id TEXT,side TEXT,price_at REAL,note TEXT,at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS bk_lessons(id INTEGER PRIMARY KEY AUTOINCREMENT,role TEXT NOT NULL,by_role TEXT NOT NULL,at REAL NOT NULL,pos_id TEXT,lesson TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bk_skills(id INTEGER PRIMARY KEY AUTOINCREMENT,skill TEXT NOT NULL,by_role TEXT NOT NULL,at REAL NOT NULL,steps TEXT NOT NULL,why TEXT);
CREATE TABLE IF NOT EXISTS bk_reviews(id INTEGER PRIMARY KEY AUTOINCREMENT,by_role TEXT NOT NULL,at REAL NOT NULL,review TEXT NOT NULL,priorities TEXT);
CREATE TABLE IF NOT EXISTS bk_video_rules(rule_id TEXT PRIMARY KEY,role TEXT NOT NULL,source_url TEXT,title TEXT,rule_text TEXT NOT NULL,instrument TEXT,status TEXT NOT NULL,backtest_json TEXT,at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS bk_operator_chat(id INTEGER PRIMARY KEY AUTOINCREMENT,at REAL NOT NULL,sender TEXT NOT NULL,to_role TEXT,text TEXT NOT NULL);
'''


def connect(path, schema):
    path = os.fspath(path)
    if path != ':memory:':
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    if path != ':memory:':
        conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=10000')
    conn.executescript(schema)
    return conn


def ro(path):
    conn = sqlite3.connect('file:%s?mode=ro' % urllib.parse.quote(os.path.abspath(path)), uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


@contextlib.contextmanager
def txn(conn):
    conn.execute('BEGIN IMMEDIATE')
    try:
        yield conn
        conn.execute('COMMIT')
    except BaseException:
        conn.execute('ROLLBACK')
        raise


def status(feed, name, ok, detail, now):
    feed.execute('INSERT OR REPLACE INTO mk_feed_status VALUES(?,?,?,?)', (name, now, 1 if ok else 0, str(detail)[:300]))


def main_loop(name, tick, argv=None, default_interval=60.0):
    """The shared host-process entry: `--config file.json [--once]`. tick(cfg, now) -> detail string; an exception in a tick is logged and the loop goes on."""
    ap = argparse.ArgumentParser(prog=name)
    ap.add_argument('--config', required=True)
    ap.add_argument('--once', action='store_true')
    args = ap.parse_args(argv)
    with open(args.config) as f:
        cfg = json.load(f)
    code = 0
    while True:
        try:
            print('%s %s' % (name, tick(cfg, time.time())), flush=True)
        except Exception as exc:         # a feed outage is a gap, never a stop; the status row says so
            code = 1
            print('%s tick_failed %s: %s' % (name, type(exc).__name__, str(exc)[:200]), file=sys.stderr, flush=True)
        if args.once:
            return code
        time.sleep(float(cfg.get('interval_s', default_interval)))
