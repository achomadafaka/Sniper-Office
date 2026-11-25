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


def _sourced_server(tmp_path, monkeypatch):
    """A floor with a sources.json: one bulk query on the ledger, one bound by name, one on the office store, one that tries to write."""
    office, quota = demo.build(tmp_path)
    ledger = tmp_path / 'ledger.db'
    with sqlite3.connect(ledger) as conn:
        conn.execute('CREATE TABLE marks(order_id TEXT,at REAL,price REAL)')
        conn.executemany('INSERT INTO marks VALUES(?,?,?)', [('a', 1., 2.), ('a', 2., 3.), ('b', 1., 9.)])
    floor = tmp_path / 'floor';floor.mkdir()
    (floor / 'floor.html').write_text('<main></main>')
    (floor / 'sources.json').write_text(json.dumps(dict(queries=dict(
        every_mark=dict(db='ledger', sql='SELECT order_id,at,price FROM marks ORDER BY at'),
        newer=dict(db='ledger', sql=['SELECT nope FROM nowhere', 'SELECT at,price FROM marks WHERE order_id=? AND at>=? ORDER BY at'], params=dict(order='text', after='real')),
        roster=dict(db='office', sql='SELECT count(*) AS n FROM office_v2_roster'),
        missing=dict(db='ledger', sql='SELECT * FROM no_such_table'),
        late=dict(db='sibling', file='collector.db', sql='SELECT count(*) AS n FROM t'), absent=dict(db='sibling', file='never.db', sql='SELECT 1 AS one')))))
    with sqlite3.connect(tmp_path / 'collector.db') as conn:        # a collector's database next to the office store, found by file name
        conn.execute('CREATE TABLE t(a)');conn.execute('INSERT INTO t VALUES(1)')
    monkeypatch.setattr(U.time, 'time', lambda: demo.NOW)
    cfg = dict(office_db=str(office), quota_db=str(quota), clock_file=None, floor_dir=str(floor), host='127.0.0.1', port=0, token=None, verbose=False, panels={}, ledgers=[str(ledger)])
    httpd = U.ThreadingHTTPServer(('127.0.0.1', 0), None);cfg['port'] = httpd.server_address[1];httpd.RequestHandlerClass = U.make_handler(cfg)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return cfg, httpd


def test_the_floors_own_queries_ride_with_the_page_read_and_run_by_name_with_bound_values(tmp_path, monkeypatch):
    cfg, httpd = _sourced_server(tmp_path, monkeypatch)
    try:
        doc = json.loads(call(cfg, 'GET', '/api/office/newsroom')[2])['sources']
        assert [r['price'] for r in doc['every_mark']['rows']] == [2., 9., 3.] and doc['every_mark']['rows'][0]['_source'] == 'ledger'
        assert doc['roster']['rows'][0]['n'] == len(demo.SEATS)
        assert doc['late']['rows'][0]['n'] == 1 and doc['absent']['state'] == 'unknown' and not (tmp_path / 'never.db').exists()      # a collector not running yet is unknown, and reading it creates nothing
        assert doc['missing']['state'] == 'unknown' and doc['missing']['rows'] == [] and 'newer' not in doc      # a query with parameters is not run for the page; one no database can answer says unknown
        status, _, raw = call(cfg, 'GET', '/api/office/query?name=newer&order=a&after=1.5')
        assert status == 200 and [r['price'] for r in json.loads(raw)['rows']] == [3.]      # the variant the database accepts is the one that runs
        for bad in ('name=nope', 'name=every_mark', 'name=newer&order=a', 'name=newer&order=a&after=x', 'name=newer&order=a&after=nan', 'name=newer&order=&after=1'):
            assert call(cfg, 'GET', '/api/office/query?' + bad)[0] == 400, bad
    finally:
        httpd.shutdown();httpd.server_close()


def test_a_query_file_that_could_write_or_is_malformed_is_refused(tmp_path):
    for bad in (dict(queries=dict(x=dict(db='ledger', sql='DELETE FROM marks'))), dict(queries=dict(x=dict(db='elsewhere', sql='SELECT 1'))), dict(queries=dict(x=dict(db='sibling', file='../up.db', sql='SELECT 1'))), dict(queries=dict(x=dict(db='sibling', sql='SELECT 1'))), dict(queries=dict(x=dict(db='office', sql='SELECT ?', params=dict(a='blob')))), [], dict(queries=[])):
        with pytest.raises(ValueError):
            V.source_queries(bad)
    ledger = tmp_path / 'l.db'
    with sqlite3.connect(ledger) as conn:
        conn.execute('CREATE TABLE marks(a)')
    assert V._run([str(ledger)], dict(sql='SELECT 1; DELETE FROM marks')) is None             # a second statement is not run: execute takes one
    assert V._run([str(ledger)], dict(sql='INSERT INTO marks VALUES(1)')) is None             # and the read-only connection and authorizer refuse a write that got past validation
    with sqlite3.connect(ledger) as conn:
        assert conn.execute('SELECT count(*) FROM marks').fetchone()[0] == 0


def test_a_seat_may_sit_in_several_rooms_and_the_chat_panel_lists_requests_and_operator_mail(server):
    layout = dict(rooms=dict(decision=['head'], research=['head', 'researcher']), fallback=None)
    doc = V.newsroom(server['office_db'], server['quota_db'], now=demo.NOW, layout=layout)
    assert 'head' in doc['rooms']['decision'] and 'head' in doc['rooms']['research'] and 'archivist' not in sum(doc['rooms'].values(), [])
    chat = json.loads(call(server, 'GET', '/api/office/operator-chat')[2])
    assert chat['status'] == 'ok' and chat['asks'] and all(a['mark'] == 'i' and a['seat'] in {s[1] for s in demo.SEATS} for a in chat['asks'])
    assert all(m['seats'][0] in {s[1] for s in demo.SEATS} for m in chat['messages'])


# ---- the floor folder's own server side: named databases and routes.py

ROUTES = '''
def databases(cfg):
    return {'archive': None, 'spare': None, **(cfg.get('databases') or {})}      # a registered name may have no path

def routes(cfg, dbs):
    def hello(query, now):
        return {'hello': query.get('who', ['nobody'])[0], 'now': now, 'archive': dbs.get('archive')}
    def act(body, now):
        if not isinstance(body['n'], int):
            raise ValueError('n')
        return {'state': 'done', 'n': body['n'] + 1}
    return {'/api/office/hello': hello}, {'/api/office/act': ({'n'}, act)}
'''


def routes_server(tmp_path, monkeypatch, with_routes=True, token=TOKEN, archive=None):
    office, quota = demo.build(tmp_path)
    floor = tmp_path / 'floor';floor.mkdir()
    (floor / 'floor.html').write_text('<main id="floor"></main>')
    if with_routes:
        (floor / 'routes.py').write_text(ROUTES)
        (floor / 'sources.json').write_text(json.dumps(dict(queries=dict(archived=dict(db='archive', sql='SELECT 1 AS one'), missing=dict(db='spare', sql='SELECT 1 AS one')))))
    monkeypatch.setattr(U.time, 'time', lambda: demo.NOW)
    cfg = dict(office_db=str(office), quota_db=str(quota), clock_file=None, floor_dir=str(floor), host='127.0.0.1', port=0, token=token, verbose=False, panels={}, databases=dict(archive=archive) if archive else {})
    httpd = U.ThreadingHTTPServer(('127.0.0.1', 0), None);cfg['port'] = httpd.server_address[1];httpd.RequestHandlerClass = U.make_handler(cfg)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return cfg, httpd


def test_the_floor_folder_may_add_read_routes_and_named_databases(tmp_path, monkeypatch):
    archive = tmp_path / 'archive.db'
    with sqlite3.connect(archive) as conn:
        conn.execute('CREATE TABLE t(x)')
    cfg, httpd = routes_server(tmp_path, monkeypatch, archive=str(archive))
    try:
        status, _, raw = call(cfg, 'GET', '/api/office/hello?who=you')
        assert status == 200 and json.loads(raw) == dict(hello='you', now=demo.NOW, archive=str(archive))
        sources = json.loads(call(cfg, 'GET', '/api/office/newsroom')[2])['sources']
        assert sources['archived'] == dict(state='known', rows=[dict(one=1, _source='archive')])       # a named database is read at the path it was given
        assert sources['missing'] == dict(state='unknown', rows=[], reason='no_database')                # a name with no path is unknown, never an empty success
    finally:
        httpd.shutdown();httpd.server_close()


def test_a_floor_without_routes_has_none_and_a_write_route_needs_the_token_the_keys_and_sane_values(tmp_path, monkeypatch):
    cfg, httpd = routes_server(tmp_path, monkeypatch, with_routes=False)
    try:
        assert call(cfg, 'GET', '/api/office/hello')[0] == 404 and call(cfg, 'POST', '/api/office/act', '{"n":1}', {'Content-Type': 'application/json', 'Origin': 'http://127.0.0.1:%d' % cfg['port'], 'X-Office-Token': TOKEN})[0] == 400
    finally:
        httpd.shutdown();httpd.server_close()
    tmp = tmp_path / 'two';tmp.mkdir()
    cfg, httpd = routes_server(tmp, monkeypatch)
    try:
        def act(body, token=TOKEN):
            raw = json.dumps(body)
            return call(cfg, 'POST', '/api/office/act', raw, {'Content-Type': 'application/json', 'Origin': 'http://127.0.0.1:%d' % cfg['port'], 'X-Office-Token': token, 'Content-Length': str(len(raw))})
        assert act(dict(n=1), token='x' * 32)[0] == 401
        assert act(dict(n=1, extra=2))[0] == 400 and act(dict())[0] == 400 and act(dict(n='one'))[0] == 400
        status, _, raw = act(dict(n=1))
        assert status == 200 and json.loads(raw) == dict(state='done', n=2)
    finally:
        httpd.shutdown();httpd.server_close()
    tmp = tmp_path / 'three';tmp.mkdir()
    cfg, httpd = routes_server(tmp, monkeypatch, token=None)
    try:
        assert call(cfg, 'POST', '/api/office/act', '{"n":1}', {'Content-Type': 'application/json'})[0] == 403       # read-only page: no write route works
    finally:
        httpd.shutdown();httpd.server_close()


def test_only_registered_database_names_are_queryable_and_a_bad_flag_is_refused():
    doc = dict(queries=dict(a=dict(db='archive_2', sql='SELECT 1')))
    assert V.source_queries(doc, names=('archive_2',))
    with pytest.raises(ValueError):
        V.source_queries(doc)                                                  # a name the floor folder did not register is refused
    with pytest.raises(ValueError):
        V.source_queries(doc, names=('other',))
    with pytest.raises(SystemExit):
        U.main(['--office-db', 'x.db', '--db', 'Bad=/tmp/x'])
    with pytest.raises(SystemExit):
        U.main(['--office-db', 'x.db', '--db', 'noequals'])


def test_mail_activity_names_both_ends_so_the_page_can_move_it_between_desks(server):
    acts = json.loads(call(server, 'GET', '/api/office/newsroom')[2])['activity']
    mail = [a for a in acts if a['kind'] == 'mail']
    assert mail and all('from' in a and 'to' in a for a in mail) and ('head', 'researcher') in {(a['from'], a['to']) for a in mail}


def test_a_sealed_seat_does_not_reach_the_rooms_even_when_rooms_json_names_it(tmp_path):
    office, quota = demo.build(tmp_path)
    layout = dict(rooms=dict(front=['head', 'researcher']), fallback=None)
    with sqlite3.connect(office) as conn:
        conn.execute('UPDATE office_v2_roster SET sealed=1 WHERE role=?', ('researcher',))
    doc = V.newsroom(str(office), str(quota), now=demo.NOW, layout=layout)
    assert doc['rooms']['front'] == ['head'] and 'researcher' not in doc['seats']


def test_a_room_lists_every_rostered_seat_once_and_the_roster_decides_how_many(tmp_path):
    office, quota = demo.build(tmp_path)
    layout = dict(rooms=dict(front=[s[1] for s in demo.SEATS], back=['head']), fallback=None)
    doc = V.newsroom(str(office), str(quota), now=demo.NOW, layout=layout)
    assert sorted(doc['rooms']['front']) == sorted(s[1] for s in demo.SEATS) and doc['rooms']['back'] == ['head']      # a seat may sit in two rooms; none is dropped or invented
    with sqlite3.connect(office) as conn:
        conn.execute('DELETE FROM office_v2_roster WHERE role=?', ('archivist',))
    doc = V.newsroom(str(office), str(quota), now=demo.NOW, layout=layout)
    assert 'archivist' not in doc['rooms']['front'] and len(doc['rooms']['front']) == len(demo.SEATS) - 1
