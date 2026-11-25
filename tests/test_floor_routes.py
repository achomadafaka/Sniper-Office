"""The seat floor's routes: a project's floor directory served by exact allowlist, and the one read the page makes."""
import http.client
import importlib.util
import json
from pathlib import Path
import threading

import pytest

import sqlite3

from sniper_office import ui as U, views as V

_spec = importlib.util.spec_from_file_location('ui_demo_data', Path(__file__).resolve().parents[1] / 'examples/ui_demo_data.py')
demo = importlib.util.module_from_spec(_spec);_spec.loader.exec_module(demo)
TOKEN = 't' * 32


@pytest.fixture
def server(tmp_path, monkeypatch):
    office, quota = demo.build(tmp_path)
    floor = tmp_path / 'floor';(floor / 'static' / 'vendor').mkdir(parents=True)
    (floor / 'floor.html').write_text('<main id="floor"></main>');(floor / 'static' / 'a.js').write_text('1');(floor / 'static' / 'w.json').write_text('{}');(floor / 'rooms.json').write_text(json.dumps(dict(rooms=dict(decision=['head'], research=['researcher'], ops=[]), fallback='research')));(floor / 'static' / 'vendor' / 'f.woff2').write_bytes(b'font')
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
    assert headers['Content-Security-Policy'] == U.HEADERS['Content-Security-Policy'] and 'style-src' not in headers['Content-Security-Policy']     # one strict policy: no inline script, no inline style
    status, headers, raw = call(server, 'GET', '/operator')
    assert status == 200 and b'id="view"' in raw
    assert call(server, 'GET', '/app.js')[0] == 200
    assert call(server, 'GET', '/static/a.js')[:1] == (200,) and call(server, 'GET', '/static/vendor/f.woff2')[1]['Content-Type'] == 'font/woff2'
    assert call(server, 'GET', '/static/w.json')[1]['Content-Type'] == 'application/json'
    for refused in ('/static/notes.txt', '/static/link.js', '/floor.html', '/static/../../secret.js', '/static/%2e%2e/secret.js', '/%2e%2e/%2e%2e/etc/passwd', '/static/'):
        assert call(server, 'GET', refused)[0] == 404, refused


def test_a_floor_directory_needs_a_floor_page(tmp_path):
    with pytest.raises(SystemExit):
        U.floor_assets(tmp_path)


def test_the_page_read_has_seats_rooms_and_a_feed_and_says_unknown_for_what_it_lacks(server):
    status, _, raw = call(server, 'GET', '/api/office/newsroom');doc = json.loads(raw)
    assert status == 200 and set(doc['seats']) == {s[1] for s in demo.SEATS}
    assert set(doc['rooms']) == {'decision', 'research', 'ops'} and doc['rooms']['decision'] == ['head'] and sorted(doc['rooms']['research']) == ['archivist', 'researcher', 'reviewer']     # unlisted roles take the fallback room
    assert all(r['status'] in ('working', 'sleeping', 'parked', None) for r in doc['seats'].values())
    assert doc['activity'] and all({'kind', 'room', 'item_id', 'at', 'title', 'seat'} <= set(e) and e['kind'] in ('mail', 'request', 'wake', 'tool') for e in doc['activity'])
    assert [e['at'] for e in doc['activity']] == sorted((e['at'] for e in doc['activity']), reverse=True)
    assert doc['book']['state'] == 'unknown' and doc['book']['reason'] == 'no_ledger_given' and 'situation' not in doc      # nothing to put there: said, not zeroed
    assert call(server, 'GET', '/api/office/feed')[0] == 404 and call(server, 'GET', '/api/office/explore')[0] == 404
    assert call(server, 'POST', '/api/office/dm', '{}', {'Content-Type': 'application/json'})[0] in (400, 403, 404)


def test_a_sealed_seat_is_withheld_from_the_page(server):
    with sqlite3.connect(server['office_db']) as conn:
        conn.execute('UPDATE office_v2_roster SET sealed=1 WHERE actor_id=?', (demo.SEATS[0][0],))
    doc = json.loads(call(server, 'GET', '/api/office/newsroom')[2])
    assert demo.SEATS[0][1] not in doc['seats'] and len(doc['seats']) == len(demo.SEATS) - 1


def test_the_paper_book_reads_a_ledger_and_is_unknown_when_it_cannot(tmp_path):
    ledger = tmp_path / 'one-ledger' / 'ledger.db';ledger.parent.mkdir()
    with sqlite3.connect(ledger) as conn:
        conn.execute('CREATE TABLE orders(order_id TEXT,state TEXT,spend_usd REAL,exit_usd REAL,costs_usd REAL,closed_at REAL,exit_reason TEXT)')
        conn.executemany('INSERT INTO orders VALUES(?,?,?,?,?,?,?)', [('a', 'closed', 10., 14., 1., 5., 'tp'), ('b', 'closed', 10., 8., 0., 6., 'stop'), ('c', 'open', 10., None, None, None, None), ('d', 'closed', 10., None, None, 7., 'censored')])
    out = V.book([str(ledger)], now=100.)
    assert out['state'] == 'known' and out['books'] == [dict(name='one-ledger', open=1, closed=3, net_usd=1., wins=1)]       # an unpriced close counts as closed, not as money
    assert [o['exit_reason'] for o in out['last_closed']] == ['censored', 'stop']
    assert V.book([str(tmp_path / 'missing.db')], now=100.)['reason'] == 'ledger_unreadable' and V.book([], now=100.)['state'] == 'unknown'


def test_a_bad_rooms_table_is_refused_and_a_missing_one_gives_seats_no_room(tmp_path):
    (tmp_path / 'rooms.json').write_text(json.dumps(dict(rooms=dict(a=['x']), fallback='nowhere')))
    with pytest.raises(SystemExit):
        U.floor_layout(tmp_path)
    (tmp_path / 'rooms.json').unlink()
    assert U.floor_layout(tmp_path) == V.NO_ROOMS
