"""The services plug-in: every service against fakes (no network, no real host files), and the tools through a real seat harness."""
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request

import pytest

from sniper_office import host_guard, plugins as P, seat_events as E, seat_tools as T, work as W
from sniper_office.services import common, list_claims as LC, narrative as NR, wallet_analyzer as WA, xcap as XC, host_watchdog as HW
from sniper_office.services.creator import _abi as A

NOW = 1_800_000_000.
ADDR = '0x' + 'ab' * 20


def write_json(path, doc):
    path.write_text(json.dumps(doc))
    return str(path)


def clock(tmp_path):
    return write_json(tmp_path / 'clock.json', dict(offset_s=0., at=NOW))


# ---- abi ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------
def test_keccak_matches_known_vectors_and_the_padding_edges():
    assert A.keccak(b'').hex() == 'c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470'
    assert A.keccak(text='transfer(address,uint256)')[:4].hex() == 'a9059cbb'
    assert A.keccak(text='abc').hex() == '4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45'
    assert len({A.keccak(b'a' * n) for n in (135, 136, 137)}) == 3        # rate-boundary lengths all differ and run


def test_abi_roundtrips_and_matches_eth_abi_when_it_is_installed():
    ad = '0x' + 'cd' * 20
    cases = [(['uint256', 'address', 'string'], (7, ad, 'héllo')), (['(address,address,uint256)'], ((ad, ad, 9),)), (['address', 'address', 'uint256', 'string', 'string', 'uint256'], (ad, ad, 1, 'n', 's' * 40, 2)),
             (['(address,address,uint256,uint256,bytes)'], ((ad, ad, 1, 2, b'\x01' * 70),)), (['bytes', '(string,uint256)', 'uint256'], (b'ab', ('s', 3), 9))]
    for types, values in cases:
        raw = A.encode(types, values)
        assert A.decode(types, raw) == values
    try:
        import eth_abi
    except ImportError:
        return
    for types, values in cases:
        assert A.encode(types, values) == eth_abi.encode(types, values)


def test_abi_refuses_short_and_dirty_input():
    with pytest.raises(A.DecodingError):
        A.decode(['uint256', 'uint256'], bytes(32))
    with pytest.raises(A.DecodingError):
        A.decode(['address'], b'\x01' + bytes(11) + bytes(20))
    with pytest.raises(A.DecodingError):
        A.decode(['string'], (32).to_bytes(32, 'big') + (99).to_bytes(32, 'big'))
    with pytest.raises(A.DecodingError):
        A.decode(['bytes'], (4000).to_bytes(32, 'big'))


# ---- host watchdog -----------------------------------------------------------------------------------------------------------------------------------------------------------------
def tick(cfg, guard, now, cpu=0.01, io=0.0, mem=8 * 1024 ** 3, **kw):
    return HW.step(cfg, guard, now, psi=lambda k: cpu if k == 'cpu' else io, mem=lambda: mem, **kw)


def test_watchdog_pauses_on_pressure_resumes_only_after_900_s_healthy_and_the_guard_reader_agrees(tmp_path):
    cfg = dict(guard_file=str(tmp_path / 'g.json'))
    g = HW.Guard(cfg)
    assert tick(cfg, g, NOW)['state'] == 'ready' and host_guard.external_guard(cfg['guard_file'], now=NOW) is None
    doc = tick(cfg, g, NOW + 1, cpu=0.5)
    assert doc['state'] == 'paused' and doc['reason'] == 'cpu_pressure' and host_guard.external_guard(cfg['guard_file'], now=NOW + 1) == 'cpu_pressure'
    assert tick(cfg, g, NOW + 2)['state'] == 'paused'                       # healthy again, but not for long enough
    assert tick(cfg, g, NOW + 800)['state'] == 'paused'
    assert tick(cfg, g, NOW + 903)['state'] == 'ready'
    assert host_guard.external_guard(cfg['guard_file'], now=NOW + 960) == 'guard_stale'      # a watchdog that stops writing reads as not ready


def test_watchdog_telemetry_loss_and_low_memory_pause_and_telemetry_pause_never_lifts_itself(tmp_path):
    cfg = dict(guard_file=str(tmp_path / 'g.json'))
    g = HW.Guard(cfg)
    assert tick(cfg, g, NOW, mem=1024)['reason'] == 'memory_available'
    g2, cfg2 = HW.Guard(cfg), dict(cfg, guard_file=str(tmp_path / 'g2.json'))

    def bad(kind):
        raise OSError('no psi')
    assert HW.step(cfg2, g2, NOW, psi=bad, mem=lambda: 1)['reason'] == 'telemetry_unknown'
    for t in range(1, 3000, 100):
        doc = tick(cfg2, g2, NOW + t)
    assert doc['state'] == 'paused' and doc['reason'] == 'telemetry_unknown' and doc['latched'] is True


def test_watchdog_heartbeat_needs_two_breaches_and_kill_fires_once_per_new_pause_only_when_configured(tmp_path):
    beat = tmp_path / 'hb.json'
    cfg = dict(guard_file=str(tmp_path / 'g.json'), heartbeat_files=[str(beat)], kill_file=str(tmp_path / 'kill'))
    kills, g = [], HW.Guard(cfg)
    beat.write_text(json.dumps(dict(ts=NOW)))
    assert tick(cfg, g, NOW + 1, kill=kills.append)['state'] == 'ready'
    assert tick(cfg, g, NOW + 100, kill=kills.append)['state'] == 'ready' and not kills       # one stale reading is a debounce, not a pause
    assert tick(cfg, g, NOW + 101, kill=kills.append)['reason'] == 'heartbeat'
    tick(cfg, g, NOW + 102, kill=kills.append)
    assert kills == [cfg['kill_file']]
    cfg_off = dict(cfg, guard_file=str(tmp_path / 'g3.json'))
    cfg_off.pop('kill_file')
    g3, k3 = HW.Guard(cfg_off), []
    tick(cfg_off, g3, NOW + 1, cpu=0.9, kill=k3.append)
    assert k3 == []


def test_watchdog_pauses_when_the_seat_user_nears_its_task_cap_and_unreadable_counts_are_unknown(tmp_path):
    # The seat user's slice hit TasksMax 6830 times (kernel "fork rejected by pids controller"): threads and browsers failed to start
    # mid-wake. Near the cap the guard pauses new wakes instead; it lifts by itself once the count falls.
    cg = tmp_path / 'user-995.slice'
    cg.mkdir()
    (cg / 'pids.max').write_text('2048\n')
    (cg / 'pids.current').write_text('200\n')
    cfg = dict(guard_file=str(tmp_path / 'g.json'), pids_cgroup=str(cg))
    g = HW.Guard(cfg)
    doc = tick(cfg, g, NOW)
    assert doc['state'] == 'ready' and doc['sample']['pids'] == [200, 2048]
    (cg / 'pids.current').write_text('1700\n')
    doc = tick(cfg, g, NOW + 1)
    assert doc['reason'] == 'pids_pressure' and doc['latched'] is False
    (cg / 'pids.current').write_text('300\n')
    assert tick(cfg, g, NOW + 2)['state'] == 'paused' and tick(cfg, g, NOW + 903)['state'] == 'ready'
    (cg / 'pids.max').write_text('max\n')
    assert tick(cfg, g, NOW + 904)['state'] == 'ready'                       # no cap: nothing to run out of
    (cg / 'pids.current').unlink()
    assert tick(cfg, HW.Guard(cfg), NOW + 905)['reason'] == 'telemetry_unknown'


def test_real_proc_sample_reads_this_host():
    s = HW.sample({}, NOW)
    assert not s['telemetry_error'] and 0 <= s['cpu'] <= 1 and s['mem'] > 0


# ---- list claims -------------------------------------------------------------------------------------------------------------------------------------------------------------------
def fomo_row(**kw):
    row = dict(feed='fomo', raw_json=json.dumps(dict(item=dict(cells=['AB\n', '$1.2M', '▲12.5%', '$340K', '1,234\n5,000 total']))), host_received_at=1000., clock_offset_s=0., observed_at=1000.)
    return dict(row, **kw)


def test_list_claims_known_values_and_every_failure_is_unknown_with_a_reason():
    ok = LC.list_claims_for_row(fomo_row(), as_of=1100.)
    assert ok['state'] == 'known' and (ok['vol_24h_usd'], ok['change_24h_pct'], ok['holders'], ok['holders_total'], ok['mcap_claim_usd']) == (340000., 12.5, 1234, 5000, 1200000.) and ok['label'] == 'provider_claim'
    for row, as_of, reason in [(fomo_row(), 1400., 'stale'), (fomo_row(), 900., 'future_source'), (fomo_row(clock_offset_s=None), 1100., 'clock_unknown'), (fomo_row(observed_at=5.), 1100., 'source_mismatch'),
                               (fomo_row(feed='other'), 1100., 'unsupported_source'), (fomo_row(raw_json='{}'), 1100., 'parse_failed'), (None, 1100., 'no_source'), (fomo_row(raw_json='not json'), 1100., 'source_unreadable')]:
        assert LC.list_claims_for_row(row, as_of=as_of)['reason'] == reason, reason
    assert LC.list_claims_for_row(fomo_row(), as_of=None)['reason'] == 'clock_unknown'


def test_list_claims_a_bad_cell_is_null_and_named_not_dropped():
    row = fomo_row(raw_json=json.dumps(dict(item=dict(cells=['AB', '$1.2M', 'n/a', '$340K', '12\n5 total']))))
    got = LC.list_claims_for_row(row, as_of=1100.)
    assert got['state'] == 'known' and got['change_24h_pct'] is None and got['field_unknown'] == {'change_24h_pct': 'parse_cell'} and got['parse_ok'] is False
    with pytest.raises(ValueError):
        LC.numeric('-5')                       # a negative USD claim is not a number


# ---- x capture ---------------------------------------------------------------------------------------------------------------------------------------------------------------------
MINT = '7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU'


def article(body='', author='alice', pid='123', extra=''):
    return ('<article data-testid="tweet"><a href="/%s/status/%s"><time datetime="2026-11-25T10:00:00.000Z">t</time></a>%s<div data-testid="tweetText">%s</div>%s</article>' % (author, pid, extra, body, ''))


def test_x_parse_direct_card_extracts_the_mint_with_its_chain_and_a_ticker_as_ambiguous_evidence():
    html = article('Buy $PEPE ca: %s on solana <a href="https://pump.fun/coin/%s">l</a><script>steal()</script>' % (MINT, MINT))
    cards, diags = XC.parse_article(XC.sanitize(html))
    assert diags == () and len(cards) == 1 and cards[0]['relationship'] == 'direct' and 'steal' not in cards[0]['full_text']
    ms = XC.normalize(cards[0])['mentions']
    assert {(m['network_id'], m['mint'], m['extraction_method'], m['eligible']) for m in ms if m['mint']} == {('solana', MINT, 'launchpad_url', True), ('solana', MINT, 'contract_text', True)}
    assert [m for m in ms if m['ticker'] == 'PEPE'][0]['eligible'] is False


def test_x_parse_repost_quote_reply_and_nested_text_is_not_flattened():
    quote = ('<div role="link"><a href="https://x.com/bob/status/999"><time datetime="2026-11-25T09:00:00Z">t</time></a><div data-testid="tweetText">quoted words</div></div>')
    cards, diags = XC.parse_article(XC.sanitize(article('my take', extra=quote)))
    assert [c['relationship'] for c in cards] == ['direct', 'quote'] and cards[0]['full_text'] == 'my take' and cards[1]['author_handle'] == 'bob' and cards[1]['actor_handle'] == 'alice'
    repost = '<a href="https://x.com/carol"><span data-testid="socialContext">Carol reposted</span></a>'
    html = '<article data-testid="tweet">' + repost + article('orig').split('<article data-testid="tweet">')[1]
    cards, _ = XC.parse_article(XC.sanitize(html))
    assert cards[0]['relationship'] == 'repost' and cards[0]['actor_handle'] == 'carol' and cards[0]['author_handle'] == 'alice'
    reply = article('re', extra='<span>Replying to @bob</span>')
    assert XC.parse_article(XC.sanitize(reply))[1] == ('missing_referenced_post_id',)
    cards, diags = XC.parse_article(XC.sanitize(article('x', extra='<div data-testid="placementTracking"></div>')))
    assert cards == () and diags == ('promoted_card',)


def test_x_sanitize_refuses_secret_material_and_non_cards():
    with pytest.raises(ValueError, match='secret_material'):
        XC.sanitize(article('x').replace('<article ', '<article cookie="a=b" '))
    with pytest.raises(ValueError, match='secret_material'):
        XC.sanitize(article('x', extra='<div aria-label="Bearer abcdef"></div>'))
    with pytest.raises(ValueError, match='not_tweet_article'):
        XC.sanitize('<div>nothing</div>')


def test_x_ingest_is_idempotent_keeps_unknown_clock_rows_out_of_reads_and_refuses_bad_payloads(tmp_path):
    conn = XC.open_db(tmp_path / 'x.db')
    payload = dict(run_id='r1', status='ok', cards=[article('hi ca: %s on solana' % MINT), '<div>junk</div>'])
    doc = XC.ingest(conn, payload, observed_at=NOW, received_at=NOW)
    assert doc['state'] == 'stored' and doc['posts_new'] == 1 and 'not_tweet_article' in doc['diagnostics']
    assert XC.ingest(conn, payload, observed_at=NOW, received_at=NOW)['state'] == 'duplicate'
    XC.ingest(conn, dict(run_id='r2', status='ok', cards=[article('again ca: %s on solana' % MINT, pid='124')]), observed_at=None, received_at=NOW)
    assert conn.execute("SELECT COUNT(*) FROM x_posts WHERE clock_domain='legacy_unknown'").fetchone()[0] == 1
    rows = XC.mentions_of(tmp_path / 'x.db', mint=MINT)
    assert [r['post_id'] for r in rows] == ['123']                                   # the unknown-clock capture is excluded
    assert XC.ingest(conn, dict(run_id='r3', status='logged_out', cards=[article('x', pid='5')]), observed_at=NOW, received_at=NOW)['posts_new'] == 0
    for bad in (dict(run_id='bad id!', status='ok'), dict(run_id='r9', status='weird'), dict(run_id='r9', status='ok', cards=['x'] * 51), dict(run_id='r9', status='ok', cards=['x' * (XC.MAX_CARD_BYTES + 1)]), []):
        with pytest.raises(ValueError):
            XC.ingest(conn, bad, observed_at=NOW, received_at=NOW)


def test_x_receiver_over_http_needs_the_token_and_caps_the_body(tmp_path):
    import http.server
    cfg = dict(x_db=str(tmp_path / 'x.db'), clock_file=write_json(tmp_path / 'c.json', dict(offset_s=0., at=time.time())))
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), XC.make_handler(cfg, 'secret-token-0123456789'))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = 'http://127.0.0.1:%d/capture' % server.server_address[1]

    def post(body, token=None, origin=None):
        req = urllib.request.Request(url, data=body, method='POST', headers={'X-Office-Token': token} if token else {})
        if origin:
            req.add_header('Origin', origin)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read()), r.headers
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read()), exc.headers
    try:
        good = json.dumps(dict(run_id='h1', status='ok', cards=[article('x ca: %s on solana' % MINT)])).encode()
        assert post(good)[0] == 403 and post(good, 'wrong-token-0123456789')[0] == 403
        status, doc, headers = post(good, 'secret-token-0123456789', 'https://x.com')
        assert status == 200 and doc['posts_new'] == 1 and doc['clock_domain'] == 'observed_real_utc' and headers['Access-Control-Allow-Origin'] == 'https://x.com'
        assert post(good, 'secret-token-0123456789', 'https://evil.example')[2].get('Access-Control-Allow-Origin') is None
        import http.client
        conn = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=5)
        conn.putrequest('POST', '/capture')
        conn.putheader('X-Office-Token', 'secret-token-0123456789')
        conn.putheader('Content-Length', str(XC.MAX_BODY + 1))
        conn.endheaders()
        assert conn.getresponse().status == 413
        assert post(b'not json', 'secret-token-0123456789')[0] == 400
    finally:
        server.shutdown()
    with pytest.raises(ValueError):
        XC.serve(dict(cfg, env_file=write_json(tmp_path / 'e', {}), token_key='K'))


# ---- wallet analyzer ---------------------------------------------------------------------------------------------------------------------------------------------------------------
def closed(n, win=0.7, pnl_each=1.0, ts0=NOW - 86400):
    return [dict(conditionId='0xc%03d' % i, outcome='Yes', totalBought=10, avgPrice=0.5, realizedPnl=(pnl_each if (i % 10) < win * 10 else -pnl_each * 0.4) + (i % 3) * 0.1, timestamp=int(ts0 + i)) for i in range(n)]


def fake_fetch(closed_rows, activity=(), positions=()):
    def fetch(url):
        for name, rows in (('closed-positions', closed_rows), ('positions', positions), ('activity', activity)):
            if '/' + name + '?' in url:
                off = int(url.split('offset=')[1].split('&')[0])
                return 200, list(rows)[off:off + WA.PAGE]
        return 200, []
    return fetch


def test_held_wallet_with_a_clear_record_is_edge_detected_and_findings_are_deterministic(tmp_path):
    conn = WA.open_db(tmp_path / 'w.db')
    data = WA.fetch_wallet(ADDR, fake_fetch(closed(60)))
    report = WA.analyze(data, now=NOW)
    assert report['mode'] == 'held_only' and report['overall_verdict'] == 'edge_detected' and report['primary_edge_type'] == 'held' and report['flip_rate'] == 0.
    run = WA.store(conn, ADDR, 'polygon', data, report, now=NOW)
    again = WA.store(conn, ADDR, 'polygon', data, report, now=NOW + 5)
    assert run == again and conn.execute('SELECT COUNT(*) FROM wallet_findings').fetchone()[0] == 1 and conn.execute('SELECT COUNT(*) FROM wallet_analysis_results').fetchone()[0] == 1
    f = WA.findings_of(ADDR, run, 'lifetime', report)
    assert f == WA.findings_of(ADDR, run, 'lifetime', report) and f[0]['finding_type'] == 'supported_hypothesis' and f[0]['severity'] == 'insight'


def test_negative_pnl_never_reads_as_an_edge_small_samples_are_underpowered_and_thin_wallets_are_data_insufficient():
    losing = WA.analyze(WA.fetch_wallet(ADDR, fake_fetch(closed(60, win=0.2))), now=NOW)
    assert losing['overall_verdict'] == 'no_edge'
    rows = [dict(conditionId='0xw%d' % i, outcome='Yes', totalBought=2, avgPrice=0.5, realizedPnl=0.5 + (i % 3) * 0.01, timestamp=int(NOW - 100 + i)) for i in range(59)]
    rows.append(dict(conditionId='0xbig', outcome='Yes', totalBought=2000, avgPrice=0.5, realizedPnl=-100., timestamp=int(NOW - 50)))
    lopsided = WA.analyze(WA.fetch_wallet(ADDR, fake_fetch(rows)), now=NOW)
    assert lopsided['branches'][0]['roi_lo95'] > 0 and lopsided['branches'][0]['net_pnl_usd'] < 0 and lopsided['overall_verdict'] == 'no_edge'      # winning ROIs, losing dollars: never an edge
    small = WA.analyze(WA.fetch_wallet(ADDR, fake_fetch(closed(25))), now=NOW)
    assert small['overall_verdict'] == 'underpowered'
    thin = WA.analyze(WA.fetch_wallet(ADDR, fake_fetch(closed(5))), now=NOW)
    assert (thin['overall_verdict'], thin['reason']) == ('data_insufficient', 'insufficient_sample')
    old = WA.analyze(WA.fetch_wallet(ADDR, fake_fetch(closed(60, ts0=NOW - 200 * 86400))), now=NOW)
    assert (old['overall_verdict'], old['reason']) == ('data_insufficient', 'dormant')


def activity_for(n_positions, flip_every=1, price_up=0.1):
    rows = []
    for i in range(n_positions):
        cid = '0xc%03d' % i
        rows.append(dict(conditionId=cid, outcome='Yes', side='BUY', size=10, price=0.4, timestamp=int(NOW - 5000 + i), asset='a%d' % i))
        if i % flip_every == 0:
            rows.append(dict(conditionId=cid, outcome='Yes', side='SELL', size=10, price=0.4 + price_up + (i % 5) * 0.01, timestamp=int(NOW - 4000 + i), asset='a%d' % i))
    return rows


def test_fifo_flips_match_partial_sells_and_the_flipper_branch_and_tag_grain_are_written(tmp_path):
    flips, sold = WA.fifo_flips([dict(conditionId='c', outcome='Y', side='BUY', size=10, price=0.5, timestamp=1), dict(conditionId='c', outcome='Y', side='BUY', size=10, price=0.25, timestamp=2),
                                 dict(conditionId='c', outcome='Y', side='SELL', size=15, price=0.5, timestamp=3)])
    assert sold == 1 and [(round(f['size'], 6), round(f['roi'], 6)) for f in flips] == [(10, 0.0), (5, 1.0)]
    rows = closed(60)
    data = WA.fetch_wallet(ADDR, fake_fetch(rows, activity_for(60)))
    tags = {'0xc%03d' % i: ['sports'] if i % 2 else ['politics'] for i in range(60)}
    report = WA.analyze(data, now=NOW, tags=tags)
    assert report['mode'] == 'flipper_only' and report['flip_rate'] == 1.0 and report['branches'][0]['branch'] == 'flipper' and report['branches'][0]['verdict'] == 'edge_detected'
    assert {g['tag'] for g in report['tag_grain']} == {'sports', 'politics'}
    conn = WA.open_db(tmp_path / 'w.db')
    WA.store(conn, ADDR, 'polygon', data, report, now=NOW)
    assert conn.execute('SELECT COUNT(*), SUM(n_flips) FROM wallet_flipper_tag_grain').fetchone() == (2, 60)


def test_mixed_mode_runs_both_branches_and_a_truncated_flipper_is_degraded_to_held():
    rows = closed(60)
    mixed = WA.analyze(WA.fetch_wallet(ADDR, fake_fetch(rows, activity_for(60, flip_every=2))), now=NOW)
    assert mixed['mode'] == 'mixed' and [b['branch'] for b in mixed['branches']] == ['held', 'flipper']
    big = activity_for(1700)
    assert len(big) >= WA.ACTIVITY_CAP
    capped = WA.analyze(WA.fetch_wallet(ADDR, fake_fetch(rows, big)), now=NOW)
    assert capped['activity_cap_hit'] and capped['mode'] == 'held_only_degraded' and capped['flags'] == ['FLIPPER_ANALYSIS_TRUNCATED'] and [b['branch'] for b in capped['branches']] == ['held']


def test_new_data_supersedes_the_old_run_and_an_unreachable_source_raises_instead_of_inventing_an_empty_wallet(tmp_path):
    conn = WA.open_db(tmp_path / 'w.db')
    WA.run(conn, ADDR, now=NOW, fetch=fake_fetch(closed(60)), with_tags=False)
    second = WA.run(conn, ADDR, now=NOW + 60, fetch=fake_fetch(closed(70)), with_tags=False)
    assert conn.execute('SELECT run_id FROM wallet_analysis_results WHERE superseded_by IS NULL').fetchall() == [(second['run_id'],)]
    assert conn.execute('SELECT COUNT(*) FROM wallet_findings WHERE superseded_by IS NULL AND run_id=?', (second['run_id'],)).fetchone()[0] == 1
    with pytest.raises(ConnectionError):
        WA.run(conn, '0x' + 'cd' * 20, now=NOW, fetch=lambda url: (None, None), with_tags=False)
    with pytest.raises(ValueError, match='chain_unsupported'):
        WA.run(conn, ADDR, now=NOW, fetch=fake_fetch([]), chain='solana')
    with pytest.raises(ValueError, match='bad_address'):
        WA.run(conn, 'nope', now=NOW, fetch=fake_fetch([]))


def test_tag_lookup_is_bounded_and_unresolved_conditions_stay_untagged():
    calls = []

    def fetch(url):
        calls.append(url)
        return 200, [dict(tags=[dict(slug='Sports')], markets=[dict(conditionId='0xC001')]) for _ in range(500)] if 'active=true' in url else []
    assert WA.fetch_tags({'0xc001', '0xc002'}, fetch, max_pages=2) == {'0xc001': ['sports']}
    assert len(calls) <= 4


# ---- narrative ---------------------------------------------------------------------------------------------------------------------------------------------------------------------
def g(gid, known, **kw):
    return dict(dict(id=gid, known_at=known, expires_at=known + 7200, theme_id='frogs', tickers=['PEPE'], names=['Pepe The Frog']), **kw)


def test_matcher_ranks_exact_over_fuzzy_over_theme_and_only_counts_guesses_written_before_first_sight():
    launch = dict(name='Pepe the Frog', symbol='PEPE', first_seen=1000.)
    assert NR.match(launch, [g('a', 900.)], guesser_state='ok') == ('exact', 'a')
    assert NR.match(dict(name='Pepee', symbol='PEPEE', first_seen=1000.), [g('a', 900., tickers=['PEPES'], names=[])], guesser_state='ok') == ('fuzzy', 'a')
    assert NR.match(dict(name='Pepe', symbol='PEPE', first_seen=1000.), [g('a', 900., tickers=['PEPS'], names=[])], guesser_state='ok') == ('none', None)      # four letters: one edit is not evidence
    assert NR.match(dict(name='Zed', symbol='ZED', theme_id='frogs', first_seen=1000.), [g('a', 900.)], guesser_state='ok') == ('theme_only', 'a')
    assert NR.match(launch, [g('a', 1000.)], guesser_state='ok') == ('none', None)             # written at the same instant: not before
    assert NR.match(launch, [g('a', 1000. - 8000)], guesser_state='ok') == ('none', None)      # expired
    assert NR.match(launch, [g('late', 950.), g('early', 900.)], guesser_state='ok') == ('exact', 'early')
    assert NR.match(dict(name='Pep', symbol='PEP', first_seen=1000.), [g('a', 900., tickers=['PEP'], names=[])], guesser_state='ok') == ('exact', 'a')
    assert NR.match(launch, [g('a', 900.)], guesser_state='stale') == (None, None)             # unknown is not none


def test_guess_generations_are_one_per_slot_validated_and_immutable(tmp_path):
    conn = NR.open_db(tmp_path / 'n.db')
    good = [dict(theme_id='frogs', rationale='r', confidence=60, tickers=['$pepe'], names=['Pepe'])]
    assert NR.write_generation(conn, good, now=NOW, actor='a1')['written'] == 1
    assert NR.write_generation(conn, good, now=NOW + 10, actor='a1')['reason'] == 'slot_already_used'
    assert NR.write_generation(conn, good, now=NOW + NR.SLOT_S, actor='a1')['state'] == 'known'
    for bad in ([], good * 6, [dict(good[0], confidence=101)], [dict(good[0], tickers=[], names=[])], [dict(good[0], tickers=['x' * 41])], 'x'):
        assert NR.write_generation(conn, bad, now=NOW + 5000, actor='a1')['state'] == 'refused'
    assert conn.execute('SELECT ticker_candidates_json FROM guesses').fetchone()[0] == '["PEPE"]'
    with pytest.raises(sqlite3.DatabaseError, match='immutable'):
        conn.execute("UPDATE guesses SET theme_id='x'")
    with pytest.raises(sqlite3.DatabaseError, match='immutable'):
        conn.execute('DELETE FROM guesser_runs')


def test_labeller_matches_a_launch_to_the_prior_guess_and_a_silent_seat_makes_matches_unknown(tmp_path):
    conn = NR.open_db(tmp_path / 'n.db')
    quiet = NR.label(conn, 'solana', 'MintQuiet', 'Pepe', 'PEPE', NOW - 100, 'table', NOW)
    assert quiet and conn.execute("SELECT match_kind,guesser_state FROM matches WHERE mint='MintQuiet'").fetchone() == (None, 'stale')
    NR.write_generation(conn, [dict(theme_id='frogs', rationale='', confidence=50, tickers=['PEPE'], names=[])], now=NOW, actor='a1')
    assert NR.label(conn, 'solana', 'MintA', 'Pepe', 'PEPE', NOW + 60, 'table', NOW + 60)
    assert not NR.label(conn, 'solana', 'MintA', 'Other', 'OTH', NOW + 90, 'table', NOW + 90)       # a first sighting is never moved or relabelled
    assert conn.execute("SELECT match_kind,guesser_state,guess_age_s FROM matches WHERE mint='MintA'").fetchone() == ('exact', 'ok', 60.)
    assert NR.label(conn, 'solana', 'MintLate', 'Pepe', 'PEPE', NOW + NR.STALE_AFTER_S + 100, 'table', NOW + NR.STALE_AFTER_S + 100)
    assert conn.execute("SELECT match_kind,guesser_state FROM matches WHERE mint='MintLate'").fetchone() == (None, 'stale')
    view = NR.matches_view(tmp_path / 'n.db')
    assert view['counts'] == {'unknown': 2, 'exact': 1} and 'name' not in view['rows'][0]


def test_launch_sources_table_cursor_future_rows_and_geckoterminal_fake(tmp_path):
    src = sqlite3.connect(tmp_path / 'launches.db')
    src.execute('CREATE TABLE launches(mint,name,symbol,received_at)')
    src.executemany('INSERT INTO launches VALUES(?,?,?,?)', [('M1', 'One', 'ONE', NOW - 10), ('M2', 'Two', 'TWO', NOW + 999)])
    src.commit()
    conn = NR.open_db(tmp_path / 'n.db')
    cfg = dict(launch_sources=[dict(kind='table', db=str(tmp_path / 'launches.db'))])
    assert NR.cycle(cfg, conn, NOW) == 1 and NR.cycle(cfg, conn, NOW + 1) == 0               # the future-stamped row is held back, then not re-read
    pool = dict(data=[dict(attributes=dict(name='FROG / SOL'), relationships=dict(base_token=dict(data=dict(id='solana_MintG')))), dict(attributes={}, relationships={})])
    assert NR.cycle(dict(launch_sources=[dict(kind='geckoterminal')]), conn, NOW, fetch=lambda url: (200, pool)) == 1
    assert NR.cycle(dict(launch_sources=[dict(kind='geckoterminal')]), conn, NOW, fetch=lambda url: (429, None)) == 0


# ---- the tools through a seat ------------------------------------------------------------------------------------------------------------------------------------------------------
GROUPS = ['wallets', 'creator', 'narrative']


def seat(tmp_path, monkeypatch, cfg=None, groups=GROUPS):
    host = P.load(dict(plugins=['sniper_office.services']))
    monkeypatch.setattr(T.SeatTools, '_stop_units', lambda self: True)
    db = tmp_path / 'o.db'
    conn = sqlite3.connect(db, isolation_level=None)
    W.ensure_schema(conn)
    E.ensure_schema(conn)
    conn.execute('INSERT OR REPLACE INTO office_v2_roster VALUES(?,?,?,?,?,0,0)', ('ag_x', 'ep', 'narrative', 1, 1))
    conn.close()
    cfg = dict(cfg or {})
    cfg.setdefault('clock_file', clock(tmp_path))
    return T.SeatTools(actor='ag_x', epoch='ep', generation=1, incarnation=1, workspace=tmp_path, path=str(tmp_path / 's.sock'), db_path=str(db), clock=lambda: NOW, office_uid=os.getuid(),
                       plugins=host, groups=groups, cfg=cfg)


def call(t, name, **args):
    out = t.call(name, args)
    text = out['content'][0]['text']
    return out['isError'], json.loads(text)


def test_registration_and_a_seat_without_the_groups_cannot_name_the_tools(tmp_path, monkeypatch):
    host = P.load(dict(plugins=['sniper_office.services']))
    names = {'wallet_analyze', 'wallet_findings', 'creator_status', 'creator_launches', 'narrative_guess', 'narrative_matches', 'x_mentions'}
    assert set(host.tools) == names and {t.group for t in host.tools.values()} == set(GROUPS)
    assert names.isdisjoint(seat(tmp_path, monkeypatch, groups=())._names()) and names <= seat(tmp_path, monkeypatch)._names()


def test_unconfigured_seat_gets_unknown_with_a_reason_from_every_tool(tmp_path, monkeypatch):
    t = seat(tmp_path, monkeypatch)
    for name, args, reason in [('wallet_analyze', dict(address=ADDR), 'wallet_config_unknown'), ('wallet_findings', {}, 'wallet_config_unknown'), ('creator_status', {}, 'creator_config_unknown'),
                               ('creator_launches', {}, 'creator_config_unknown'), ('narrative_guess', dict(guesses=[]), 'narrative_config_unknown'), ('narrative_matches', {}, 'narrative_config_unknown'),
                               ('x_mentions', dict(ticker='PEPE'), 'x_config_unknown')]:
        error, doc = call(t, name, **args)
        assert error and doc == dict(state='unknown', reason=reason), name


def test_wallet_tools_refuse_other_chains_and_bad_addresses_and_serve_the_cache(tmp_path, monkeypatch):
    t = seat(tmp_path, monkeypatch, cfg=dict(wallet_db=str(tmp_path / 'w.db')))
    monkeypatch.setattr(WA, 'FETCH', fake_fetch(closed(60)))
    assert call(t, 'wallet_analyze', address=ADDR, chain='solana')[1]['reason'] == 'chain_unsupported'
    assert call(t, 'wallet_analyze', address='0x12')[1]['reason'] == 'bad_address'
    error, doc = call(t, 'wallet_analyze', address=ADDR.upper().replace('0X', '0x'))
    assert not error and doc['overall_verdict'] == 'edge_detected' and doc['cached'] is False and doc['wallet'] == ADDR and 'screen only' in doc['limits']
    monkeypatch.setattr(WA, 'FETCH', lambda url: (None, None))
    assert call(t, 'wallet_analyze', address=ADDR)[1]['cached'] is True                             # served from the store, no network
    assert call(t, 'wallet_analyze', address=ADDR, refresh=True)[1] == dict(state='unknown', reason='source_unreachable', source='data-api.polymarket.com')
    found = call(t, 'wallet_findings', query=ADDR[:10])[1]
    assert found['state'] == 'known' and [i['finding_type'] for i in found['items']] == ['supported_hypothesis']
    assert call(t, 'wallet_findings', query='0xdead')[1]['items'] == []


def test_narrative_tools_write_once_per_slot_and_x_mentions_needs_a_coin(tmp_path, monkeypatch):
    t = seat(tmp_path, monkeypatch, cfg=dict(narrative_db=str(tmp_path / 'n.db'), x_db=str(tmp_path / 'x.db')))
    guess = [dict(theme_id='frogs', rationale='r', confidence=50, tickers=['PEPE'], names=[])]
    assert call(t, 'narrative_guess', guesses=guess)[1]['written'] == 1
    error, doc = call(t, 'narrative_guess', guesses=guess)
    assert error and doc['reason'] == 'slot_already_used'
    assert call(t, 'narrative_matches')[1]['rows'] == []
    assert call(t, 'x_mentions')[1]['reason'] == 'mint_or_ticker_required'
    XC.ingest(XC.open_db(tmp_path / 'x.db'), dict(run_id='r1', status='ok', cards=[article('buy $PEPE')]), observed_at=NOW, received_at=NOW)
    items = call(t, 'x_mentions', ticker='pepe')[1]['items']
    assert len(items) == 1 and items[0]['eligible'] == 0 and items[0]['ticker'] == 'PEPE'


def test_creator_tools_read_the_checkpoint_and_flag_a_stale_one(tmp_path, monkeypatch):
    state = tmp_path / 'collect'
    state.mkdir()
    (state / 'checkpoint.json').write_text(json.dumps(dict(status='running', calls=5, subject='0xaa', launches=[dict(token='0x1', block=3, nested=dict(a=1))], last_error=None)))
    t = seat(tmp_path, monkeypatch, cfg=dict(creator_state=dict(collect=str(state), venue=str(tmp_path / 'missing'))))
    doc = call(t, 'creator_status')[1]
    assert doc['state'] == 'known' and doc['launches'] == 1 and doc['running'] is True and doc['calls'] == 5
    os.utime(state / 'checkpoint.json', (time.time() - 3600,) * 2)
    assert call(t, 'creator_status')[1]['running'] is False
    assert call(t, 'creator_launches')[1]['items'] == [dict(token='0x1', block=3)]
    assert call(t, 'creator_status', which='venue')[1]['reason'] == 'checkpoint_unreadable'
    assert call(t, 'creator_status', which='bogus')[1]['reason'] == 'creator_config_unknown'



def test_a_latched_pause_is_not_downgraded_by_a_later_pressure_reading(tmp_path):
    cfg = dict(guard_file=str(tmp_path / 'g.json'))
    g = HW.Guard(cfg)

    def bad(kind):
        raise OSError('no psi')
    assert HW.step(cfg, g, NOW, psi=bad, mem=lambda: 1)['reason'] == 'telemetry_unknown'
    doc = tick(cfg, g, NOW + 1, cpu=0.5)                 # a reason that could lift by itself must not replace the latched one
    assert doc['state'] == 'paused' and doc['reason'] == 'telemetry_unknown' and doc['latched'] is True and doc['last_reason'] == 'cpu_pressure'
    for t in range(2, 3000, 100):
        doc = tick(cfg, g, NOW + t)
    assert doc['state'] == 'paused' and doc['reason'] == 'telemetry_unknown'
