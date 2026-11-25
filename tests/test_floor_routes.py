"""The animated floor's routes: a project's floor directory served by exact allowlist, the feed in the shape the floor reads, and the floor's DM box."""
import http.client
import importlib.util
import json
from pathlib import Path
import threading

import pytest

from sniper_office import ui as U, views as V

_spec = importlib.util.spec_from_file_location('ui_demo_data', Path(__file__).resolve().parents[1] / 'examples/ui_demo_data.py')
demo = importlib.util.module_from_spec(_spec);_spec.loader.exec_module(demo)
TOKEN = 't' * 32


@pytest.fixture
def server(tmp_path, monkeypatch):
    office, quota = demo.build(tmp_path)
    floor = tmp_path / 'floor';(floor / 'static' / 'vendor').mkdir(parents=True)
    (floor / 'floor.html').write_text('<main id="floor"></main>');(floor / 'static' / 'a.js').write_text('1');(floor / 'static' / 'vendor' / 'f.woff2').write_bytes(b'font')
    (floor / 'static' / 'notes.txt').write_text('not an allowed type');(tmp_path / 'secret.js').write_text('outside the floor')
    (floor / 'static' / 'link.js').symlink_to(tmp_path / 'secret.js')
    monkeypatch.setattr(U.time, 'time', lambda: demo.NOW)
    cfg = dict(office_db=str(office), quota_db=str(quota), clock_file=None, floor_dir=str(floor), host='127.0.0.1', port=0, token=TOKEN, verbose=False, panels={})
    httpd = U.ThreadingHTTPServer(('127.0.0.1', 0), None);cfg['port'] = httpd.server_address[1];httpd.RequestHandlerClass = U.make_handler(cfg)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield cfg
    httpd.shutdown();httpd.server_close()


def call(cfg, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection('127.0.0.1', cfg['port'], timeout=5)
    conn.request(method, path, body=body, headers=dict(headers or {}))
    r = conn.getresponse();raw = r.read();conn.close()
    return r.status, dict(r.getheaders()), raw


def test_the_floor_is_at_root_the_operator_page_at_operator_and_only_listed_files_are_served(server):
    status, headers, raw = call(server, 'GET', '/')
    assert status == 200 and b'id="floor"' in raw
    assert "style-src 'self' 'unsafe-inline'" in headers['Content-Security-Policy'] and 'script-src' not in headers['Content-Security-Policy']
    status, headers, raw = call(server, 'GET', '/operator')
    assert status == 200 and b'id="view"' in raw and 'style-src' not in headers['Content-Security-Policy']       # the operator page keeps the strict policy
    assert call(server, 'GET', '/app.js')[0] == 200
    assert call(server, 'GET', '/static/a.js')[:1] == (200,) and call(server, 'GET', '/static/vendor/f.woff2')[1]['Content-Type'] == 'font/woff2'
    assert "style-src" not in call(server, 'GET', '/static/a.js')[1]['Content-Security-Policy']
    for refused in ('/static/notes.txt', '/static/link.js', '/floor.html', '/static/../../secret.js', '/static/%2e%2e/secret.js', '/%2e%2e/%2e%2e/etc/passwd', '/static/'):
        assert call(server, 'GET', refused)[0] == 404, refused


def test_a_floor_directory_needs_a_floor_page(tmp_path):
    with pytest.raises(SystemExit):
        U.floor_assets(tmp_path)


def test_feed_has_the_shape_the_floor_reads(server):
    status, _, raw = call(server, 'GET', '/api/office/feed?limit=200');doc = json.loads(raw)
    assert status == 200 and doc['roster']['state'] == 'known' and len(doc['roster']['items']) == 4
    assert {s['actor_id'] for s in doc['seats']['items']} == {r['actor_id'] for r in doc['roster']['items']}
    assert all(s['activity']['kind'] in ('working', 'sleeping', 'parked', 'idle', 'unknown') for s in doc['seats']['items'])
    assert doc['events']['items'] and all(isinstance(e['seq'], str) and isinstance(e['refs'], dict) and 'refs_json' not in e for e in doc['events']['items'])
    assert {'items', 'state', 'cursor'} <= set(doc['events']) and set(doc) >= {'as_of', 'mail', 'tasks', 'questions'}
    newest = doc['events']['cursor']                       # a cursor at the newest event pages to nothing
    assert json.loads(call(server, 'GET', '/api/office/feed?after=' + newest)[2])['events']['items'] == []
    one = json.loads(call(server, 'GET', '/api/office/feed?actor=' + doc['roster']['items'][0]['actor_id'])[2])['events']['items']
    assert one and {e['actor_id'] for e in one} == {doc['roster']['items'][0]['actor_id']}
    for bad in ('limit=0', 'limit=201', 'limit=x', 'after=-1', 'after=1.5', 'mail_after=abc'):
        assert call(server, 'GET', '/api/office/feed?' + bad)[0] == 400, bad
    assert json.loads(call(server, 'GET', '/api/office/explore')[2])['state'] == 'unknown'      # no book here: said, not zeroed


def test_a_sealed_seat_is_withheld_from_the_feed(server):
    import sqlite3
    with sqlite3.connect(server['office_db']) as conn:
        conn.execute('UPDATE office_v2_roster SET sealed=1 WHERE actor_id=?', (demo.SEATS[0][0],))
    doc = json.loads(call(server, 'GET', '/api/office/feed')[2])
    assert doc['roster']['state'] == 'known' and doc['roster']['omitted'] == 0 and len(doc['roster']['items']) == 3     # withheld on purpose, not counted as damaged
    assert demo.SEATS[0][0] not in {r['actor_id'] for r in doc['roster']['items']} | {s['actor_id'] for s in doc['seats']['items']}


def test_the_floor_dm_box_needs_the_same_checks_and_reaches_one_seat_or_all(server):
    headers = {'Origin': 'http://127.0.0.1:%d' % server['port'], 'X-Office-Token': TOKEN, 'Content-Type': 'application/json'}
    body = lambda **kw: json.dumps(dict(dict(to_actor_id=demo.SEATS[1][0], client_id='floor-0001', text='hello'), **kw))
    assert call(server, 'POST', '/api/office/dm', body(), {k: v for k, v in headers.items() if k != 'X-Office-Token'})[0] == 401
    assert call(server, 'POST', '/api/office/dm', body(), dict(headers, Origin='http://evil.example'))[0] == 403
    assert call(server, 'POST', '/api/office/dm', json.dumps(dict(to_actor_id='x', text='t')), headers)[0] == 400
    assert call(server, 'POST', '/api/office/dm', body(text='   '), headers)[0] == 400
    before = len(V.mail(server['office_db'], now=demo.NOW)['items'])
    status, _, raw = call(server, 'POST', '/api/office/dm', body(), headers)
    assert status == 200 and json.loads(raw)['state'] == 'sent'
    assert json.loads(call(server, 'POST', '/api/office/dm', body(), headers)[2])['state'] == 'duplicate'
    assert call(server, 'POST', '/api/office/dm', body(to_actor_id='nobody', client_id='floor-0002'), headers)[0] == 400
    assert call(server, 'POST', '/api/office/dm', body(client_id='floor-0001', text='different'), headers)[0] == 409
    status, _, raw = call(server, 'POST', '/api/office/dm', body(to_actor_id='all', client_id='floor-0003'), headers)
    assert status == 200 and len(json.loads(raw)['seats']) == 4
    assert len(V.mail(server['office_db'], now=demo.NOW, limit=200)['items']) == before + 1 + 4
