"""The floor's newer rooms (markets, the J7 copy lane, services, token names, the two token-gated writes): floor/routes.py and the page's scripts, against made-up data (floor_fixture.py).
They live here because the repository's word guard keeps this plug-in's domain words out of the generic tests."""
import http.client
import importlib.util
import json
from pathlib import Path
import re
import sqlite3
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import floor_fixture as FX                                              # noqa: E402
from sniper_office import ui as U, views as V                          # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
FLOOR = ROOT / 'floor'


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


demo = _load(ROOT / 'examples/ui_demo_data.py', 'ui_demo_data_markets')
R = _load(FLOOR / 'routes.py', 'floor_routes_test')
TOKEN = 't' * 32


def call(cfg, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection('127.0.0.1', cfg['port'], timeout=5)
    conn.request(method, path, body=body, headers=dict(headers or {}))
    r = conn.getresponse();raw = r.read();conn.close()
    return r.status, dict(r.getheaders()), raw


# ---- the newer rooms: markets, the J7 lane, services, token names, the two write actions


THIRTEEN = ('head', 'judge', 'judge_gpt', 'scout', 'researcher', 'forensics', 'narrative', 'copy_desk', 'newsdesk', 'pm_trader', 'perps_trader', 'stocks_trader', 'coach')


@pytest.fixture
def fx(tmp_path, monkeypatch):
    office, quota = demo.build(tmp_path)
    FX.seed(tmp_path, demo.NOW, office_db=office)
    floor = tmp_path / 'floor';floor.mkdir()
    (floor / 'floor.html').write_text('<main id="floor"></main>');(floor / 'sources.json').write_text((FLOOR / 'sources.json').read_text());(floor / 'routes.py').write_text((FLOOR / 'routes.py').read_text())
    monkeypatch.setattr(U.time, 'time', lambda: demo.NOW)
    cfg = dict(office_db=str(office), quota_db=str(quota), clock_file=None, floor_dir=str(floor), host='127.0.0.1', port=0, token=TOKEN, verbose=False, panels={}, ledgers=[str(tmp_path / 'ledger.db')],
               guard_file=str(tmp_path / 'guard.json'), creator_dir=None, names_db=str(tmp_path / 'names.db'), names_lookup=False)
    httpd = U.ThreadingHTTPServer(('127.0.0.1', 0), None);cfg['port'] = httpd.server_address[1];httpd.RequestHandlerClass = U.make_handler(cfg)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    cfg['dir'] = tmp_path
    yield cfg
    httpd.shutdown();httpd.server_close()


def get(cfg, path):
    status, _, raw = call(cfg, 'GET', path)
    return status, json.loads(raw)


def test_markets_route_carries_every_room_and_says_unknown_when_the_database_is_missing(fx):
    status, doc = get(fx, '/api/office/markets')
    assert status == 200 and doc['state'] == 'known' and set(doc['rooms']) == {'newsroom', 'situation', 'strategies', 'paper_labs', 'sports', 'explorer', 'memes', 'coach', 'lab', 'control'}
    assert all(r.get('state') != 'unknown' for r in doc['rooms'].values() if isinstance(r, dict)), {k: v for k, v in doc['rooms'].items() if isinstance(v, dict) and v.get('state') == 'unknown'}
    wire = doc['rooms']['newsroom']['wire']
    assert {w['news_id'] for w in wire} == {'n0', 'n1', 'n2', 'n3'} and doc['rooms']['situation']['stories'] and doc['rooms']['sports']['games'][0]['home'] == 'Celtics'
    assert {b['lane'] for b in doc['rooms']['paper_labs']['books']} == {'polymarket', 'perps', 'stocks'} and doc['rooms']['coach']['lessons'] and doc['rooms']['lab']['rules']
    (fx['dir'] / 'markets.db').unlink()
    status, doc = get(fx, '/api/office/markets')
    assert status == 200 and doc['state'] == 'unknown' and doc['reason'] == 'markets_db_missing' and doc['rooms'] == {}      # said, not zeroed


def test_a_hostile_headline_travels_as_data_and_the_page_scripts_never_build_markup(fx):
    doc = get(fx, '/api/office/markets')[1]
    assert any(FX.HOSTILE in w['title'] for w in doc['rooms']['newsroom']['wire'])     # carried verbatim: the page renders it with textContent
    scripts = {p.name: p.read_text() for p in (FLOOR / 'static').glob('office-*.js') if p.name in ('office-live.js', 'office-names.js', 'office-adapter.js')}
    assert set(scripts) == {'office-live.js', 'office-names.js', 'office-adapter.js'}
    bad = re.compile(r'innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function|srcdoc')
    assert not [n for n, t in scripts.items() if bad.search(t)]
    assert bad.search('node.innerHTML = feed.title') and bad.search('x.insertAdjacentHTML("beforeend", y)')      # the guard bites
    assert '<script>' not in (FLOOR / 'floor.html').read_text().replace('<script defer src=', '')      # no inline script under the strict policy


def test_live_route_gives_pnl_rows_tp_sl_signals_and_only_new_ticks_after_since(fx):
    status, doc = get(fx, '/api/office/live')
    assert status == 200 and doc['state'] == 'known' and doc['books'][0]['book_id'] == 'office-v2-copy-1' and doc['books'][0]['total_usd'] == 12.5
    pos = doc['positions'][0]
    assert pos['order_id'] == 'o1' and pos['sl_pct'] == 30. and pos['tp_pct'] == 100. and pos['entry_price_usd'] == .001 and pos['unrealized_usd'] == 12.5
    assert [s['signal_id'] for s in doc['signals']] == ['s3', 's2', 's1'] and doc['signals'][0]['side'] == 'sell'
    ticks = doc['ticks']['solana:' + FX.MINT]
    assert len(ticks) == 300 and ticks == sorted(ticks) and doc['ticks']['solana:' + FX.MINT2] == []       # a coin with no ticks is empty, not invented
    assert doc['resolution']['poll_gap_median_s'] == 1.0 and doc['resolution']['price_gap_median_s'] == 3.0
    since = ticks[-5][0]
    newer = get(fx, '/api/office/live?since=%s' % since)[1]['ticks']['solana:' + FX.MINT]
    assert [t[0] for t in newer] == [t[0] for t in ticks if t[0] > since] and len(newer) == 4


def test_live_route_is_unknown_without_the_tables(tmp_path):
    office, _ = demo.build(tmp_path)
    doc = R.live(str(office), str(tmp_path / 'absent.db'), (), now=demo.NOW)
    assert doc['state'] == 'unknown' and doc['books'] is None and doc['positions'] is None and doc['signals'] is None and doc['ticks'] == {}


def test_guard_states_and_the_unit_reader(tmp_path):
    path = tmp_path / 'g.json'
    assert R.guard_state(None, now=100.)['state'] == 'unknown' and R.guard_state(path, now=100.)['state'] == 'unreadable'
    path.write_text(json.dumps(dict(state='ready', reason=None, at=95.)))
    assert R.guard_state(path, now=100.)['state'] == 'ready'
    path.write_text(json.dumps(dict(state='paused', reason='cpu_psi', at=99., latched=True)))
    g = R.guard_state(path, now=100.)
    assert g['state'] == 'paused' and g['reason'] == 'cpu_psi' and g['latched'] is True
    path.write_text(json.dumps(dict(state='ready', reason=None, at=10.)))
    assert R.guard_state(path, now=100.)['state'] == 'stale'                    # an old "ready" is not ready
    path.write_text('[1]')
    assert R.guard_state(path, now=100.)['state'] == 'unreadable'
    show = ('Id=office-j7-ticks.service\nActiveState=active\nSubState=running\nLoadState=loaded\nUnitFileState=enabled\nType=simple\n\n'
            'Id=office-clock.service\nActiveState=inactive\nSubState=dead\nLoadState=loaded\nUnitFileState=static\nType=oneshot\n\n'
            'Id=office-clock.timer\nActiveState=active\nSubState=waiting\nLoadState=loaded\nUnitFileState=enabled\n\n'
            'Id=office-crew-bridge.service\nActiveState=inactive\nSubState=dead\nLoadState=loaded\nUnitFileState=disabled\nType=simple\n\n'
            'Id=office-paper-filler.service\nActiveState=failed\nSubState=failed\nLoadState=loaded\nUnitFileState=enabled\nType=simple\n\n'
            'Id=office-xcap.service\nLoadState=not-found\n')
    fake = lambda argv: show
    states = R.unit_states(runner=fake)
    assert 'office-xcap' not in states and states['office-clock.timer']['active'] == 'active' and states['office-crew-bridge']['enabled'] == 'disabled'
    shown, hidden = R.judge_units(states, 30.)
    assert shown['office-j7-ticks']['ok'] is True and shown['office-paper-filler']['ok'] is False          # an enabled unit that is not running is a problem
    assert 'office-crew-bridge' not in shown and hidden == 1                                                  # a disabled unit is not listed, only counted
    assert shown['office-clock'] == dict(active='timer active', sub='waiting', ok=True, note='clock file 30 s old')    # a timed oneshot is judged by its timer and the clock file, not by the service being inactive
    assert R.judge_units(states, 700.)[0]['office-clock']['ok'] is False and R.judge_units(states, None)[0]['office-clock']['ok'] is False      # a stale or unreadable clock file is a problem
    dead = dict(states, **{'office-clock.timer': dict(states['office-clock.timer'], active='inactive')})
    assert R.judge_units(dead, 30.)[0]['office-clock']['ok'] is False and R.judge_units(None, 1.) == (None, None)


def test_services_route_joins_guard_j7_ticks_and_crew(fx):
    with sqlite3.connect(fx['office_db']) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS crew_bridge_cursor(name TEXT PRIMARY KEY, value INTEGER NOT NULL)');conn.execute("INSERT INTO crew_bridge_cursor VALUES('mirror',203)")
    status, doc = get(fx, '/api/office/services')
    assert 'units_not_enabled' in doc and status == 200 and doc['guard']['state'] == 'ready' and doc['j7']['signals_total'] == 3 and doc['ticks']['coins_60s'] == 1 and doc['crew']['cursors'] == {'mirror': 203}
    assert doc['creator']['state'] == 'unknown'
    d = fx['dir'] / 'creator' / 'collect';d.mkdir(parents=True)
    (d / 'checkpoint.json').write_text(json.dumps(dict(status='running', calls=7, last_error=None, launches=[dict(token='0x' + 'a' * 40, tx_hash='0x' + 'b' * 64, order=[1, 2], capture='x.json')])))
    assert R.creator_state(fx['dir'] / 'creator')['launches'][0]['token'] == '0x' + 'a' * 40 and R.creator_state(fx['dir'] / 'creator')['calls'] == 7


def test_the_floors_new_queries_read_the_service_databases_and_say_unknown_for_the_missing_ones(fx):
    src = json.loads(call(fx, 'GET', '/api/office/newsroom')[2])['sources']
    assert [r['guess_id'] for r in src['narrative_guesses']['rows']] == ['g1'] and src['narrative_matches']['rows'][0]['name'] == 'Biaoqing' and src['narrative_matches']['rows'][0]['match_kind'] == 'fuzzy'
    assert src['x_mentions']['rows'][0]['ticker'] == 'PLARA' and FX.HOSTILE in src['x_mentions']['rows'][0]['text'] and src['wallet_findings']['rows'][0]['finding_type'] == 'no_edge'
    (fx['dir'] / 'x.db').unlink()
    src = json.loads(call(fx, 'GET', '/api/office/newsroom')[2])['sources']
    assert src['x_mentions']['state'] == 'unknown' and src['x_mentions']['reason'] == 'unreadable' and src['x_mentions']['rows'] == []


def test_token_names_use_local_data_first_then_one_cached_lookup_and_clean_what_providers_say(tmp_path):
    ids = R.parse_ids('solana:%s,bad,solana:short,SOLANA:%s,bsc:0x%s,solana:%s' % (FX.MINT, FX.MINT, 'a' * 40, FX.MINT))
    assert ids == [('solana', FX.MINT), ('bsc', '0x' + 'a' * 40)]
    assert R.clean_name('Ka‮pi\x00 \n Pla   Ra' + 'x' * 80) == 'Kapi Pla Ra' + 'x' * 29 and R.clean_name(5) is None
    FX.seed(tmp_path, demo.NOW, office_db=tmp_path / 'o.db')
    calls = []
    doc = [dict(baseToken=dict(address=FX.MINT, name='Kapi Pla Ra', symbol='PlaRa')), dict(baseToken=dict(address='other', name='Wrong', symbol='W'))]
    names = R.TokenNames(str(tmp_path / 'n.db'), R.local_names(str(tmp_path / 'j7.db'), str(tmp_path / 'narrative.db')), fetch=lambda url: calls.append(url) or json.dumps(doc).encode(), start=False)
    got = names.lookup([('solana', FX.MINT), ('solana', FX.MINT2)], 100.)
    assert got['solana:' + FX.MINT] == dict(name=None, symbol='PlaRa', source='local')                 # a J7 symbol is known at once, the name is not yet
    assert got['solana:' + FX.MINT2]['name'] == 'Biaoqing' and got['solana:' + FX.MINT2]['symbol'] == 'BIAO'      # the narrative launch table has both
    assert not calls and names.queue.qsize() == 1                                                         # only the coin without a name is queued, and nothing was fetched on the request
    names.step(names.queue.get_nowait(), 101.)
    assert calls == ['https://api.dexscreener.com/tokens/v1/solana/' + FX.MINT]
    assert names.lookup([('solana', FX.MINT)], 102.)['solana:' + FX.MINT] == dict(name='Kapi Pla Ra', symbol='PlaRa', source='dexscreener')      # the pair whose base token is another address is ignored
    names.lookup([('solana', FX.MINT)], 103.);assert names.queue.qsize() == 0                              # cached: not queued again
    miss = R.TokenNames(str(tmp_path / 'm.db'), None, fetch=lambda url: b'[]', start=False)
    miss.lookup([('solana', FX.MINT)], 100.);miss.step(miss.queue.get_nowait(), 100.)
    miss.lookup([('solana', FX.MINT)], 200.);assert miss.queue.qsize() == 0                                # a miss is remembered for NAMES_RETRY_S
    miss.lookup([('solana', FX.MINT)], 100. + R.NAMES_RETRY_S + 1);assert miss.queue.qsize() == 1


def test_the_name_lookup_goes_only_to_dexscreener_over_https():
    from sniper_office.markets import common as MC
    for url in ('http://api.dexscreener.com/x', 'https://evil.example/tokens', 'https://127.0.0.1/tokens', 'https://user:p@api.dexscreener.com/x'):
        with pytest.raises(ValueError):
            MC.http(url, allow={'api.dexscreener.com'})
    assert R.NAMES_URL.startswith('https://api.dexscreener.com/')


def test_token_names_route_returns_known_names_only(fx):
    status, doc = get(fx, '/api/office/token-names?ids=solana:%s,solana:%s,junk' % (FX.MINT, FX.MINT2))
    assert status == 200 and doc['names']['solana:' + FX.MINT]['symbol'] == 'PlaRa' and doc['names']['solana:' + FX.MINT2]['name'] == 'Biaoqing' and doc['lookup'] is True
    assert get(fx, '/api/office/token-names?ids=')[1]['names'] == {}


def test_mail_activity_names_both_ends_so_the_page_can_move_it_between_desks(fx):
    acts = json.loads(call(fx, 'GET', '/api/office/newsroom')[2])['activity']
    mail = [a for a in acts if a['kind'] == 'mail']
    assert mail and all('from' in a and 'to' in a for a in mail) and {(a['from'], a['to']) for a in mail} >= {('head', 'researcher')}


def post(cfg, path, body, token=TOKEN):
    return call(cfg, 'POST', path, json.dumps(body), {'Content-Type': 'application/json', 'Origin': 'http://127.0.0.1:%d' % cfg['port'], 'X-Office-Token': token, 'Content-Length': str(len(json.dumps(body)))})


def test_halting_a_book_and_the_operator_note_need_the_token_and_exact_keys(fx):
    mled = str(fx['dir'] / 'markets_ledger.db')
    body = dict(book_id='pm-pm_trader', halted=True, reason='test')
    assert post(fx, '/api/markets/halt', body, token='x' * 32)[0] == 401
    assert post(fx, '/api/markets/halt', dict(body, extra=1))[0] == 400 and post(fx, '/api/markets/halt', dict(body, halted='yes'))[0] == 400
    status, _, raw = post(fx, '/api/markets/halt', body)
    assert status == 200 and json.loads(raw) == dict(state='known', book_id='pm-pm_trader', halted=True)
    with sqlite3.connect(mled) as c:
        assert c.execute("SELECT halted FROM bk_books WHERE book_id='pm-pm_trader'").fetchone() == (1,) and c.execute("SELECT count(*) FROM bk_events WHERE kind='halt'").fetchone() == (1,)
    assert get(fx, '/api/office/markets')[1]['rooms']['control']['books'][0]['halted'] in (True, False) and any(b['halted'] for b in get(fx, '/api/office/markets')[1]['rooms']['control']['books'])
    unknown = post(fx, '/api/markets/halt', dict(body, book_id='nope'))
    assert unknown[0] == 409 and json.loads(unknown[2])['reason'] == 'book_unknown'
    blank = post(fx, '/api/markets/say', dict(to_role='pm_trader', text='  '))
    assert blank[0] == 409 and json.loads(blank[2])['reason'] == 'text_required'
    assert json.loads(post(fx, '/api/markets/say', dict(to_role='pm_trader', text='tighten'))[2]) == dict(state='recorded') and post(fx, '/api/markets/say', dict(text='x'))[0] == 400


def test_halt_is_refused_when_the_page_is_read_only(tmp_path, monkeypatch):
    office, quota = demo.build(tmp_path)
    cfg = dict(office_db=str(office), quota_db=str(quota), clock_file=None, floor_dir=None, host='127.0.0.1', port=0, token=None, verbose=False, panels={})
    httpd = U.ThreadingHTTPServer(('127.0.0.1', 0), None);cfg['port'] = httpd.server_address[1];httpd.RequestHandlerClass = U.make_handler(cfg)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        assert call(cfg, 'POST', '/api/markets/halt', '{}', {'Content-Type': 'application/json'})[0] == 403
        assert call(cfg, 'GET', '/api/office/live')[0] == 404 and call(cfg, 'GET', '/api/office/markets')[0] == 404       # a page with no floor folder has no extra routes
    finally:
        httpd.shutdown();httpd.server_close()


def test_rooms_json_places_every_seat_in_a_room_and_none_of_the_dropped_ones():
    doc = json.loads((FLOOR / 'rooms.json').read_text())
    placed = {role for members in doc['rooms'].values() for role in members}
    assert set(THIRTEEN) <= placed and not placed & {'explorer', 'meme_scout', 'pm_critic'}
    assert set(doc['rooms']) >= {'newsroom', 'debate', 'trading', 'explorer', 'memes', 'coach', 'control', 'situation', 'sports', 'strategies', 'paper-labs', 'lab'}
    assert {'copy_desk', 'perps_trader', 'stocks_trader', 'pm_trader'} <= set(doc['rooms']['trading']) and {'newsdesk', 'narrative'} <= set(doc['rooms']['newsroom']) and doc['rooms']['coach'] == ['coach']
    assert not {r for r in THIRTEEN if r not in placed}                    # nothing falls back to the Research pile
    assert V.source_queries(json.loads((FLOOR / 'sources.json').read_text()), tuple(R.DEFAULT_DBS))      # the floor's queries validate


def test_every_floor_asset_is_served_and_the_page_lists_the_new_scripts():
    html = (FLOOR / 'floor.html').read_text()
    assets = U.floor_assets(FLOOR)
    for name in ('office-names.js', 'office-live.js', 'office-live.css'):
        assert '/static/' + name in html and '/static/' + name in assets
    assert html.index('office-names.js') < html.index('office-adapter.js') and html.index('office-rooms-office.js') < html.index('office-live.js')


def test_a_sealed_seat_does_not_reach_the_rooms_even_when_rooms_json_names_it(tmp_path):
    office, quota = demo.build(tmp_path)
    layout = dict(rooms=dict(trading=['head', 'researcher']), fallback=None)
    with sqlite3.connect(office) as conn:
        conn.execute('UPDATE office_v2_roster SET sealed=1 WHERE role=?', ('researcher',))
    doc = V.newsroom(str(office), str(quota), now=demo.NOW, layout=layout)
    assert doc['rooms']['trading'] == ['head'] and 'researcher' not in doc['seats']
