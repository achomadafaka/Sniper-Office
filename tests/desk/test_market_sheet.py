"""Feed staleness (known empty vs unknown), the chart activity rule, fuller sheets, and the market_data tool, all against fake providers."""
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from conftest import MINT, POOL  # noqa: E402
from test_desk_plugin import NOW, call, desk, seat, ticket  # noqa: E402
from test_feed import World, rig  # noqa: F401,E402

from sniper_office.desk import feed as F, market as M  # noqa: E402


def add(path, tid, verdict, reason, age_s, blob='{}'):
    ts = time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime(NOW - age_s))
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO desk_tickets(ticket_id,network_id,mint,pool,stage,verdict,reason,ts,inputs_json) VALUES(?,'solana',?,?,'hard_filter',?,?,?,?)", (tid, 'm' + tid, 'p' + tid, verdict, reason, ts, blob))


# ---- 1. staleness
def test_a_live_collector_with_no_passing_ticket_is_a_known_empty_feed_with_counts(tmp_path, monkeypatch):
    path = desk(tmp_path / 'd.db', [ticket('oldpass', 2000)])
    for i in range(3):
        add(path, 's%d' % i, 'SKIP', 'kill:mcap:mcap_safety', 30 + i)
    add(path, 'l', 'SKIP', 'kill:liquidity:liq_min', 10)
    t = seat(tmp_path, monkeypatch, cfg=dict(desk_db=path, feed_window_s=1000))
    error, doc = call(t, 'feed_list')
    assert not error and doc['state'] == 'known' and doc['items'] == []
    assert doc['collector'] == dict(heartbeat_age_s=10, window_s=1000, pass_n=0, skip_n=4, top_skip_reasons=[dict(reason='kill:mcap:mcap_safety', n=3), dict(reason='kill:liquidity:liq_min', n=1)])


def test_a_stale_collector_stays_unknown_even_when_the_feed_seen_table_is_present(tmp_path, monkeypatch):
    path = desk(tmp_path / 'd.db', [ticket('a', 5000)])
    add(path, 's', 'SKIP', 'kill:x', 4000)
    error, doc = call(seat(tmp_path, monkeypatch, cfg=dict(desk_db=path)), 'feed_list')
    assert error and doc['reason'] == 'desk_stale' and doc['newest_age_s'] == 4000


def test_feed_seen_is_a_heartbeat_when_tickets_are_older_but_the_collector_is_still_discovering(tmp_path, monkeypatch):
    path = desk(tmp_path / 'd.db', [ticket('a', 5000)])
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE feed_seen(mint TEXT PRIMARY KEY, first_seen REAL, source TEXT, claim_json TEXT, ticketed INTEGER)')
        c.execute("INSERT INTO feed_seen VALUES('m',?,'s','{}',0)", (NOW - 60,))
    error, doc = call(seat(tmp_path, monkeypatch, cfg=dict(desk_db=path)), 'feed_list')
    assert not error and doc['state'] == 'known' and doc['collector']['heartbeat_age_s'] == 60


# ---- 2. chart activity
def test_zero_recent_trades_is_not_a_chart_pass(rig):
    world, make, t, _ = rig
    world.m5 = dict(buys=0, sells=0)
    feed = make()
    feed.cycle()
    ((_, _, _, verdict, reason, _, blob),) = __import__('test_feed').rows(feed.cfg['desk_db'])
    chart = json.loads(blob)['evidence']['checks']['chart']
    assert (verdict, reason) == ('SKIP', 'unknown:chart:chart_prints_lt_2') and chart['status'] == 'unknown' and chart['prints_m5'] == 0


def test_one_print_is_still_below_sniper_chart_rule_and_two_pass(rig):
    world, make, t, _ = rig
    world.m5 = dict(buys=1, sells=0)
    f1 = make()
    f1.cycle()
    assert __import__('test_feed').rows(f1.cfg['desk_db'])[0][4] == 'unknown:chart:chart_prints_lt_2'
    world2 = World()
    world2.m5 = dict(buys=1, sells=1)
    f2 = F.Feed(dict(f1.cfg, desk_db=str(_ / 'two.db')), fetch=world2, clock=lambda: t[0], sleep=lambda s: None)
    f2.cycle()
    assert __import__('test_feed').rows(f2.cfg['desk_db'])[0][3] == 'PASS'


# ---- 3. sheets
def full_ticket(tmp_path, market=True):
    """A ticket carrying a decision-time market snapshot, as the collector now writes it."""
    pair = dict(dexId='pumpswap', pairAddress=POOL, priceUsd='0.00008', priceChange=dict(h1=20.), volume=dict(h1=1000.), txns=dict(h1=dict(buys=5, sells=4)), liquidity=dict(usd=9000., base=1e6, quote=40.),
                fdv=1, marketCap=1, pairCreatedAt=(NOW - 600) * 1000, baseToken=dict(address=MINT), quoteToken=dict(address='q'))
    ev = dict(checks=dict(cluster=dict(status='unknown', reason='not_collected_here')), **(dict(market=dict(F.snapshot(pair, NOW), source='dexscreener:pairs', fetched_at=NOW)) if market else {}))
    path = desk(tmp_path / 'd.db', [])
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO desk_tickets(ticket_id,network_id,mint,pool,stage,verdict,reason,ts,inputs_json) VALUES('tk','solana',?,?,'hard_filter','PASS','pass',?,?)",
                  (MINT, POOL, time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime(NOW - 30)), json.dumps(dict(evidence=ev))))
    return path


def live_seat(tmp_path, monkeypatch, market, now=NOW, **cfg):
    market.down = False
    monkeypatch.setattr(time, 'time', lambda: now)
    return seat(tmp_path, monkeypatch, cfg=dict(desk_db=full_ticket(tmp_path), **cfg))


def test_sheet_is_known_with_decision_time_facts_kept_apart_from_the_fresh_pool_read(tmp_path, monkeypatch, market):
    market.price = '0.00012'
    error, doc = call(live_seat(tmp_path, monkeypatch, market), 'sheet_read', ticket_id='tk')
    assert not error and doc['state'] == 'known' and doc['missing'] == []
    dec, now = doc['sheet']['decision_time'], doc['sheet']['now_read']
    assert dec['run_up_pct']['h1'] == 20. and dec['volume_usd']['h1'] == 1000. and dec['txns']['h1'] == dict(buys=5., sells=4.) and dec['reserves']['quote'] == 40. and dec['liquidity_usd'] == 9000.
    assert dec['pair_age_s_at_ticket'] == 570 and dec['price_usd'] == 0.00008
    assert now['state'] == 'known' and now['fetched_at'] == NOW and now['snapshot']['price_usd'] == 0.00012 and now['price_change_since_ticket_pct'] == 50. and 'not a fill price' in now['note']
    assert doc['sheet']['trades_60s'] == dict(doc['sheet']['trades_60s'], state='known', window_s=60, trades_n=2, buys_n=1, sells_n=1) and doc['sheet']['trades_60s']['newest_trade_age_s'] == 5
    assert doc['sheet']['cluster']['status'] == 'unknown' and doc['sheet']['dump_group'] == dict(status='unknown', reason='not_collected_here')


def test_sheet_stays_partial_and_names_each_missing_part(tmp_path, monkeypatch, market):
    t = live_seat(tmp_path, monkeypatch, market)
    market.status['api.dexscreener.com'] = 429
    market.trades_ago, market.trade_cap_full = [5, 20], True   # the capped list starts inside the window: coverage unproven
    doc = call(t, 'sheet_read', ticket_id='tk')[1]
    assert doc['state'] == 'partial' and doc['reason'] == 'sheet_incomplete'
    assert {m['part']: m['reason'] for m in doc['missing']} == dict(now_read='rate_limited', trades_60s='list_does_not_reach_window_start')
    assert doc['sheet']['now_read']['state'] == 'unknown' and doc['sheet']['trades_60s']['state'] == 'unknown'
    off = seat(tmp_path, monkeypatch, cfg=dict(t.cfg, sheet_live=False))
    assert [m['part'] for m in call(off, 'sheet_read', ticket_id='tk')[1]['missing']] == ['now_read', 'trades_60s']


def test_an_old_ticket_without_the_market_snapshot_is_partial_even_when_the_live_reads_work(tmp_path, monkeypatch, market):
    market.down = False
    monkeypatch.setattr(time, 'time', lambda: NOW)
    t = seat(tmp_path, monkeypatch, cfg=dict(desk_db=full_ticket(tmp_path, market=False)))
    doc = call(t, 'sheet_read', ticket_id='tk')[1]
    assert doc['state'] == 'partial' and [m['part'] for m in doc['missing']] == ['decision_time_market'] and doc['sheet']['now_read']['state'] == 'known'


def test_the_collector_stores_the_market_snapshot_with_the_ticket(rig):
    world, make, t, _ = rig
    feed = make()
    feed.cycle()
    m = json.loads(__import__('test_feed').rows(feed.cfg['desk_db'])[0][6])['evidence']['market']
    assert m['run_up_pct']['h1'] is None and m['txns']['m5'] == dict(buys=10., sells=8.) and m['reserves']['quote'] == 40. and m['liquidity_usd'] == 6000. and m['pair_age_s'] == 120
    assert m['source'] == 'dexscreener:pairs' and m['fetched_at'] == NOW


def test_trades_window_counts_the_last_60s_and_a_quiet_pool_is_known_zero(market, monkeypatch):
    market.down = False
    monkeypatch.setattr(time, 'time', lambda: NOW)
    market.trades_ago = [80, 90]
    r = M.trades_window(POOL, NOW)
    assert r['state'] == 'known' and r['trades_n'] == 0 and r['listed_n'] == 2
    market.trades_ago = []
    assert M.trades_window(POOL, NOW)['state'] == 'known'


# ---- 4. market_data
def test_market_data_is_a_labelled_provider_claim_and_refuses_bad_input(tmp_path, monkeypatch, market):
    t = live_seat(tmp_path, monkeypatch, market)
    error, doc = call(t, 'market_data', pool=POOL)
    assert not error and doc['state'] == 'known' and doc['source'] == 'dexscreener:pairs' and doc['fetched_at'] == NOW and 'not a fill price' in doc['note'] and doc['provider_claim']['run_up_pct']['h1'] == 20.
    assert call(t, 'market_data', mint=MINT)[1]['provider_claim']['pair'] == POOL and any('/tokens/' + MINT in c for c in market.calls)
    assert call(t, 'market_data', mint='other' + MINT[5:], pool=POOL)[1]['reason'] == 'mint_pool_mismatch'
    assert call(t, 'market_data')[1]['reason'] == 'mint_or_pool_required'
    assert call(t, 'market_data', pool='../../etc/passwd')[1]['reason'] == 'bad_address'
    market.status['api.dexscreener.com'] = 429
    n = len(market.calls)
    assert call(t, 'market_data', pool=POOL)[1]['reason'] == 'rate_limited' and call(t, 'market_data', pool=POOL)[1]['reason'] == 'rate_limited' and len(market.calls) == n + 1   # backoff: no second call


def test_market_data_is_a_separate_group_and_the_transport_allowlist_holds(tmp_path, monkeypatch, market):
    plain = seat(tmp_path, monkeypatch, groups=['paper'])
    assert 'market_data' not in plain._names()
    assert M.net().get('https://evil.example.test/x') == (None, 'host_not_allowed')
    assert F.http_fetch('http://api.dexscreener.com/x') == (None, None, None)


# ---- 5. the Office data-sources example
def test_the_office_data_sources_example_loads_and_names_only_real_columns_and_no_raw_blobs():
    from sniper_office import seat_db as S
    from sniper_office.desk import filler as FL
    spec = S.load_spec(str(Path(__file__).parents[2] / 'config' / 'office_data_sources.example.json'))
    conn = sqlite3.connect(':memory:')
    conn.executescript(FL.SCHEMA + F.SCHEMA)
    for name, t in spec['tables'].items():
        if t['source'] in ('desk', 'ledger'):
            have = {r[1] for r in conn.execute('PRAGMA table_info(%s)' % t.get('source_table', name))}
            assert have and set(t['columns'] + [t['time_column']]) <= have, name
        assert not {'inputs_json', 'policy_json', 'fill_proof', 'exit_proof', 'output_json'} & set(t['columns']), name
    assert {'desk_tickets', 'orders', 'marks', 'request_outcomes', 'history_desk_tickets', 'explore_orders', 'explore_gpt_orders'} <= set(spec['tables'])
