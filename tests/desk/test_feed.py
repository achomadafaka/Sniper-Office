"""The desk feed collector: one ticket per pool, unknown checks recorded as unknown, kills and unknowns refuse, a fresh ticket reaches feed_list end to end, retention bounds the db."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_desk_plugin import NOW, call, desk, seat  # noqa: E402

from sniper_office.desk import feed as F  # noqa: E402

MINT, POOL, HOLDER = 'MintAAAA' + '1' * 34, 'PooBBBB' + '2' * 35, 'HodCCCC' + '3' * 35
RPC = 'https://rpc.example.test/?api-key=SECRETKEY'
SECRET = 'SECRETKEY'


class World:
    """Fake providers by url. Knobs are plain attributes; every call is counted per kind."""
    def __init__(self):
        self.liq_sol, self.mcap, self.m5, self.mint_auth, self.freeze_auth, self.hook = 40., 200_000., dict(buys=10, sells=8), None, None, None
        self.created_ms, self.loss_back, self.down, self.calls, self.status = (NOW - 120) * 1000, 99_000_000, False, [], {}
        self.whale = 30

    def pair(self):
        return dict(chainId='solana', pairAddress=POOL, dexId='pumpswap', baseToken=dict(address=MINT), quoteToken=dict(address=F.WSOL), priceUsd='0.0001', marketCap=self.mcap, fdv=self.mcap,
                    pairCreatedAt=self.created_ms, liquidity=dict(usd=self.liq_sol * 150, base=1e6, quote=self.liq_sol), txns=dict(m5=self.m5))

    def __call__(self, url, body=None):
        kind = 'rpc' if url.startswith('https://rpc.') else url.split('/')[2]
        self.calls.append((kind, body['method'] if body else url))
        if self.down or self.status.get(kind):
            return self.status.get(kind, 500), None, 30.
        if 'token-profiles' in url:
            return 200, [dict(chainId='solana', tokenAddress=MINT, description='hello'), dict(chainId='ethereum', tokenAddress='0xabc')], None
        if 'token-boosts' in url:
            return 200, [], None
        if 'new_pools' in url:
            return 200, dict(data=[dict(attributes=dict(address=POOL, pool_created_at='x', name='A / SOL'), relationships=dict(base_token=dict(data=dict(id='solana_' + MINT))))]), None
        if '/latest/dex/tokens/' in url:
            return 200, dict(pairs=[self.pair()]), None
        if 'quote' in url:
            if 'outputMint=' + MINT in url:
                return 200, dict(outAmount='5000000', priceImpactPct='0.1'), None
            return 200, dict(outAmount=str(self.loss_back), priceImpactPct='0.2'), None
        if kind == 'rpc':
            m = body['method']
            if m == 'getAccountInfo':
                return 200, dict(result=dict(context=dict(slot=9), value=dict(owner=F.TOKEN_2022 if self.hook else 'Tokenkeg', data=dict(parsed=dict(info=dict(
                    mintAuthority=self.mint_auth, freezeAuthority=self.freeze_auth, supply='1000000', decimals=6,
                    extensions=[dict(extension='transferHook', state=dict(programId=self.hook))] if self.hook else [])))))), None
            if m == 'getTokenLargestAccounts':
                return 200, dict(result=dict(value=[dict(address='vault', amount='500000'), dict(address='h1', amount=str(self.whale * 10000))])), None
            if m == 'getMultipleAccounts':
                own = lambda o: dict(data=dict(parsed=dict(info=dict(owner=o))))
                return 200, dict(result=dict(value=[own(POOL), own(HOLDER)])), None
        return 404, None, None


@pytest.fixture
def rig(tmp_path):
    env = tmp_path / 'research.env'
    env.write_text('OTHER=1\nMEMECOIN_RESEARCH_HELIUS_RPC_URL="%s"\n' % RPC)
    world, t = World(), [NOW]

    def make(**cfg):
        c = dict(desk_db=str(tmp_path / 'feed.db'), research_env_file=str(env), min_interval_s=dict(dexscreener=0, geckoterminal=0, jupiter=0, rpc=0))
        c.update(cfg)
        return F.Feed(c, fetch=world, clock=lambda: t[0], sleep=lambda s: None)
    return world, make, t, tmp_path


def rows(path):
    with sqlite3.connect(path) as c:
        return c.execute('SELECT ticket_id,mint,pool,verdict,reason,ts,inputs_json FROM desk_tickets').fetchall()


def test_a_passing_pool_gets_one_ticket_with_sourced_checks_and_feed_list_sees_it_end_to_end(rig, monkeypatch):
    world, make, t, tmp = rig
    feed = make()
    out = feed.cycle()
    assert out['written'] == 1 and out['passed'] == 1 and out['discovered'] == 1   # the ethereum profile is not a candidate
    (tid, mint, pool, verdict, reason, ts, blob), = rows(feed.cfg['desk_db'])
    assert (mint, pool, verdict, reason) == (MINT, POOL, 'PASS', 'pass')
    checks = json.loads(blob)['evidence']['checks']
    for name, row in checks.items():
        assert row['status'] in ('pass', 'kill', 'unknown') and row['source'] and row['fetched_at'] == NOW, name
    assert checks['holders']['top10_pct'] == 30. and checks['holders']['exclusions_verified'] is True and checks['sell_probe']['round_trip_loss_pct'] == 1.
    assert checks['cluster']['status'] == 'unknown' and checks['market_data']['provider_claim']['pairAddress'] == POOL
    assert SECRET not in blob and F.rpc_url_from(feed.cfg['research_env_file']) == RPC
    seat_tools = seat(tmp, monkeypatch, cfg=dict(desk_db=feed.cfg['desk_db']))
    error, doc = call(seat_tools, 'feed_list')
    assert not error and [i['ticket_id'] for i in doc['items']] == [tid] and doc['items'][0]['facts']['top10_pct'] == 30.
    assert call(seat_tools, 'sheet_read', ticket_id=tid)[1]['evidence']['checks']['token_safety']['authority_mint'] == 'closed'
    order = call(seat(tmp, monkeypatch, role='judge', cfg=dict(desk_db=feed.cfg['desk_db'])), 'paper_order', ticket_id=tid, reason='r', thesis='t', disproof='d', exit_plan='e')[1]
    assert order['state'] == 'queued'


def test_the_same_pool_is_ticketed_once_across_cycles_and_restarts(rig):
    world, make, t, _ = rig
    make().cycle()
    n = len(world.calls)
    t[0] += 60
    out = make().cycle()
    assert out['written'] == 0 and len(rows(make().cfg['desk_db'])) == 1
    assert not [c for c in world.calls[n:] if c[0] == 'rpc' or 'quote' in c[1]]   # no second evidence fetch for a ticketed mint


def test_without_the_rpc_the_authority_and_holder_checks_are_recorded_unknown_and_the_ticket_is_skip_never_pass(rig):
    world, make, t, _ = rig
    feed = make(research_env_file=None)
    assert feed.cycle()['passed'] == 0
    (_, _, _, verdict, reason, _, blob), = rows(feed.cfg['desk_db'])
    checks = json.loads(blob)['evidence']['checks']
    assert (verdict, reason) == ('SKIP', 'unknown:token_safety:rpc_unconfigured')
    assert checks['token_safety']['status'] == 'unknown' and checks['holders']['status'] == 'unknown' and 'authority_mint' not in checks['token_safety']
    assert not [c for c in world.calls if c[0] == 'rpc']


@pytest.mark.parametrize('knob,value,reason', [
    ('liq_sol', 3., 'kill:liquidity:liq_min'), ('mcap', 1000., 'kill:mcap:mcap_safety'), ('mcap', 9e8, 'kill:mcap:mcap_safety'), ('mint_auth', 'someone', 'kill:token_safety:mint_authority_open'),
    ('freeze_auth', 'someone', 'kill:token_safety:freeze_authority_open'), ('hook', 'HookProgram', 'kill:token_safety:transfer_hook'), ('loss_back', 80_000_000, 'kill:sell_probe:round_trip_loss_pct'),
    ('m5', dict(buys=25, sells=0), 'kill:flow:buys_without_sells'), ('created_ms', 1000., 'kill:chart:pair_too_old')])
def test_each_kill_rule_refuses_and_the_default_world_passes(rig, knob, value, reason):
    world, make, t, _ = rig
    setattr(world, knob, value)
    feed = make()
    feed.cycle()
    assert [r[3:5] for r in rows(feed.cfg['desk_db'])] == [('SKIP', reason)]


def test_unknown_mandatory_data_is_skip_not_pass(rig):
    world, make, t, _ = rig
    world.m5 = None
    world.loss_back = 0       # the route cannot sell
    feed = make()
    feed.cycle()
    assert [r[3:5] for r in rows(feed.cfg['desk_db'])] == [('SKIP', 'unknown:sell_probe:no_route_sell')]


def test_a_429_backs_off_and_an_outage_defers_then_writes_unknown_after_the_retry_window(rig):
    world, make, t, _ = rig
    feed = make(retry_unknown_s=300)
    world.status['api.dexscreener.com'] = 429
    out = feed.cycle()
    assert out['written'] == 0 and out['sources']['dexscreener:token-profiles'] == 'rate_limited' and feed.net.stop_until['dexscreener'] == NOW + 30
    n = len(world.calls)
    assert feed.cycle()['written'] == 0 and not [c for c in world.calls[n:] if c[0] == 'api.dexscreener.com']
    t[0] += 60
    world.status.clear()
    world.down = True
    assert feed.cycle()['written'] == 0 and rows(feed.cfg['desk_db']) == []   # inside the window: deferred, nothing written
    t[0] += 400
    assert feed.cycle()['written'] == 1
    assert [r[3:5] for r in rows(feed.cfg['desk_db'])] == [('SKIP', 'unknown:market_data:no_pair_or_provider_unavailable')]


def test_the_net_refuses_other_hosts_plain_http_and_non_read_only_rpc_methods(rig):
    world, make, t, _ = rig
    feed = make()
    assert feed.net.get('https://evil.example.test/x') == (None, 'host_not_allowed')
    assert feed.net.get('https://rpc.example.test.evil.test/x') == (None, 'host_not_allowed')
    with pytest.raises(ValueError):
        feed.net.rpc('sendTransaction', [])
    assert http_refuses_plain()
    assert not world.calls


def http_refuses_plain():
    return F.http_fetch('http://api.dexscreener.com/x') == (None, None, None)


def test_retention_drops_tickets_older_than_the_window_and_caps_the_row_count(rig):
    world, make, t, _ = rig
    feed = make(retention_days=1, max_rows=2)
    iso = lambda age: F.datetime.fromtimestamp(NOW - age, F.timezone.utc).isoformat()
    put = lambda i, age: feed.write(dict(ticket_id='t%d' % i, mint='m%d' % i, pool='p', verdict='PASS', reason='pass', ts=iso(age), inputs_json='{}'))
    put(0, 3 * 86400), put(1, 10)
    assert feed.retain(NOW) == 1 and [r[0] for r in rows(feed.cfg['desk_db'])] == ['t1']
    put(2, 5), put(3, 5)
    assert feed.retain(NOW) == 1 and [r[0] for r in rows(feed.cfg['desk_db'])] == ['t2', 't3']      # the cap keeps the newest rows


@pytest.mark.parametrize('m5,status,reason', [(dict(buys=None, sells=5), 'unknown', 'm5_txns_missing'), (dict(buys=5), 'unknown', 'm5_txns_missing'), (dict(buys=0, sells=0), 'unknown', 'chart_prints_lt_2'),
                                              (dict(buys=0, sells=3), 'pass', 'age_and_activity_present')])
def test_a_missing_m5_count_is_an_unknown_chart_never_zero_prints(m5, status, reason):
    cfg = dict(F.DEFAULTS, min_prints_m5=2)
    pair = dict(pairCreatedAt=(NOW - 600) * 1000, txns=dict(m5=m5), liquidity=dict(usd=1.), priceUsd='1', marketCap=1e5)
    chart = F.market_checks(pair, NOW, cfg, NOW)['chart']
    assert (chart['status'], chart['reason']) == (status, reason)


def test_pool_origin_kills_creator_seeded_meteora_and_labels_the_rest():
    from sniper_office.desk.feed import pool_origin
    held = lambda top: dict(status='pass', top10_pct=top)
    assert pool_origin(dict(dexId='meteora'), 'Abc', held(95.0), 1)['status'] == 'kill'
    assert pool_origin(dict(dexId='meteora'), 'Abc', held(40.0), 1)['reason'] == 'creator_pool'
    assert pool_origin(dict(dexId='meteora'), 'Abc', dict(status='unknown'), 1)['status'] == 'pass'      # unknown holders never kill here
    assert pool_origin(dict(dexId='pumpswap'), 'Xpump', held(99.0), 1)['reason'] == 'launchpad_migrated'
    assert pool_origin(dict(dexId='pumpswap'), 'Xbonk', held(5.0), 1)['reason'] == 'creator_pool'
