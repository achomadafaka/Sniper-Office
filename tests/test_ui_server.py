"""The UI server over real HTTP: what it serves, what it refuses, and that a write needs every check to pass."""
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
RESEARCHER = demo.SEATS[1][0]


@pytest.fixture(params=[TOKEN, None], ids=['with-token', 'read-only'])
def server(tmp_path, request, monkeypatch):
    office, quota = demo.build(tmp_path)
    monkeypatch.setattr(U.time, 'time', lambda: demo.NOW)
    cfg = dict(office_db=str(office), quota_db=str(quota), clock_file=None, host='127.0.0.1', port=0, token=request.param, verbose=False)
    httpd = U.ThreadingHTTPServer(('127.0.0.1', 0), None);cfg['port'] = httpd.server_address[1];httpd.RequestHandlerClass = U.make_handler(cfg)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield cfg
    httpd.shutdown();httpd.server_close()


def call(cfg, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection('127.0.0.1', cfg['port'], timeout=5)
    conn.request(method, path, body=body, headers=dict(headers or {}))
    r = conn.getresponse();raw = r.read();conn.close()
    return r.status, dict(r.getheaders()), raw


def good(cfg, **over):
    h = {'Origin': 'http://127.0.0.1:%d' % cfg['port'], 'X-Office-Token': TOKEN, 'Content-Type': 'application/json'};h.update(over)
    return {k: v for k, v in h.items() if v is not None}


def test_pages_and_reads_are_served_with_locked_down_headers(server):
    for path, kind in (('/', 'text/html'), ('/app.js', 'text/javascript'), ('/app.css', 'text/css')):
        status, headers, raw = call(server, 'GET', path)
        assert status == 200 and headers['Content-Type'].startswith(kind) and raw
        assert headers['Content-Security-Policy'].startswith("default-src 'self'") and headers['X-Content-Type-Options'] == 'nosniff' and headers['Cache-Control'] == 'no-store'
    status, _, raw = call(server, 'GET', '/api/overview');doc = json.loads(raw)
    assert status == 200 and doc['state'] == 'known' and len(doc['seats']) == 4 and doc['writable'] is (server['token'] is not None)
    assert json.loads(call(server, 'GET', '/api/seat?actor=' + RESEARCHER)[2])['seat']['status'] == 'parked'
    assert len(json.loads(call(server, 'GET', '/api/mail')[2])['items']) == 3
    for path in ('/api/mail', '/api/seat?actor=' + RESEARCHER):          # every view carries the flag, not only the first one loaded
        assert json.loads(call(server, 'GET', path)[2])['writable'] is (server['token'] is not None)
    assert call(server, 'GET', '/favicon.ico')[0] == 200
    assert call(server, 'GET', '/api/seat?actor=' + RESEARCHER + '&before=x')[0] == 400 and call(server, 'GET', '/api/nope')[0] == 404
    assert call(server, 'GET', '/../../etc/passwd')[0] == 404 and call(server, 'GET', '/ui_static/app.js')[0] == 404


def test_the_page_never_builds_markup_from_data_and_loads_nothing_from_elsewhere(server):
    script = call(server, 'GET', '/app.js')[2].decode();page = call(server, 'GET', '/')[2].decode()
    for banned in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'eval(', 'new Function'):
        assert banned not in script
    assert 'http://' not in page and 'https://' not in page and '//' not in page.replace('</', '').replace('/>', '').replace('/app', '')
    assert demo.HOSTILE in json.loads(call(server, 'GET', '/api/mail')[2])['items'][1]['text']      # delivered as data, exactly as written


def test_a_request_under_another_host_name_is_refused(server):
    for method, path in (('GET', '/api/overview'), ('GET', '/'), ('POST', '/api/mail')):
        assert call(server, method, path, headers={'Host': 'evil.example:%d' % server['port']})[0] == 421


def test_a_write_needs_token_origin_json_and_a_small_body(server):
    body = json.dumps(dict(to=RESEARCHER, client_id='client-0001', text='Use the 2025 filing.'))
    if server['token'] is None:
        assert call(server, 'POST', '/api/mail', body, good(server))[0] == 403
        assert len(V.mail(server['office_db'], now=demo.NOW)['items']) == 3
        return
    for headers, status in ((good(server, **{'X-Office-Token': 'wrong'}), 401), (good(server, **{'X-Office-Token': None}), 401), (good(server, Origin='http://evil.example'), 403),
                            (good(server, Origin=None), 403), (good(server, **{'Content-Type': 'text/plain'}), 415)):
        assert call(server, 'POST', '/api/mail', body, headers)[0] == status
    assert call(server, 'POST', '/api/mail', 'x' * 9000, good(server))[0] == 413
    for bad in ('[]', 'not json', json.dumps(dict(to=RESEARCHER, text='no client id')), json.dumps(dict(to=RESEARCHER, client_id='client-0001', text='t', sender='head'))):
        assert call(server, 'POST', '/api/mail', bad, good(server))[0] == 400
    assert call(server, 'POST', '/api/other', body, good(server))[0] == 400
    assert len(V.mail(server['office_db'], now=demo.NOW)['items']) == 3          # nothing above wrote anything
    status, _, raw = call(server, 'POST', '/api/mail', body, good(server))
    assert status == 200 and json.loads(raw)['state'] == 'sent'
    assert json.loads(call(server, 'POST', '/api/mail', body, good(server))[2])['state'] == 'duplicate'
    newest = V.mail(server['office_db'], now=demo.NOW)['items'][0]
    assert (newest['sender'], newest['to']) == ('operator', RESEARCHER)
    status, _, raw = call(server, 'POST', '/api/mail', json.dumps(dict(to='nobody', client_id='client-0002', text='t')), good(server))
    assert status == 409 and json.loads(raw) == dict(state='refused', reason='unknown_actor')
    status, _, raw = call(server, 'POST', '/api/clear-park', json.dumps(dict(actor=RESEARCHER, reason='usage reading fixed')), good(server))
    assert status == 200 and json.loads(raw)['state'] == 'cleared'


def test_token_file_must_be_private_and_long_and_remote_listening_needs_one(tmp_path):
    f = tmp_path / 'token';f.write_text('x' * 40);f.chmod(0o644)
    with pytest.raises(SystemExit):U.read_token(f)
    f.chmod(0o600);assert U.read_token(f) == 'x' * 40 and U.read_token(None) is None
    f.write_text('short')
    with pytest.raises(SystemExit):U.read_token(f)
    with pytest.raises(SystemExit, match='refusing to listen'):U.main(['--office-db', str(tmp_path / 'o.db'), '--host', '0.0.0.0'])
