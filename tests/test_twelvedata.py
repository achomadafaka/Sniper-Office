"""Twelve Data tools: the key stays on the host, a missing key answers unknown, and results are bounded data."""
import http.server
import json
import threading

import pytest

from sniper_office import plugins as P

KEY = 'k3y-SECRET-12345'
SEEN = []


class Fake(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        SEEN.append(self.path)
        body = json.dumps(dict(status='error', code=401, message='bad apikey ' + KEY) if 'bad' in self.path else
                          dict(symbol='AAPL', close='1.5', values=[dict(datetime='d', close=str(i)) for i in range(3000)] if 'time_series' in self.path else None))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a):
        pass


@pytest.fixture
def host(tmp_path):
    srv = http.server.HTTPServer(('127.0.0.1', 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    env = tmp_path / 'td.env'
    env.write_text('OTHER=nope\nTWELVE_DATA_API_KEY="%s"\n' % KEY)
    SEEN.clear()
    h = P.load(dict(plugins=['sniper_office.twelvedata'], twelvedata=dict(env_file=str(env), base_url='http://127.0.0.1:%d' % srv.server_port)))
    yield h, env
    srv.shutdown()


def text(res):
    return res['content'][0]['text']


def test_quote_and_time_series_are_grouped_bounded_and_never_show_the_key(host):
    h, _ = host
    assert {n: t.group for n, t in h.tools.items()} == dict(twelvedata_quote='twelvedata', twelvedata_time_series='twelvedata')
    ok = h.tools['twelvedata_quote'].handler(None, symbol='AAPL')
    assert json.loads(text(ok))['state'] == 'ok' and 'apikey=' + KEY in SEEN[0]
    big = h.tools['twelvedata_time_series'].handler(None, symbol='AAPL', interval='1day', outputsize=5000)
    assert len(text(big)) <= 16384 and 'outputsize=200' in SEEN[-1]
    bad = h.tools['twelvedata_quote'].handler(None, symbol='bad')
    assert json.loads(text(bad))['state'] == 'error'
    assert all(KEY not in text(r) for r in (ok, big, bad))


def test_missing_key_or_file_answers_unknown_and_makes_no_request(host):
    h, env = host
    for body in ('OTHER=1\n', None):
        env.write_text(body) if body else env.unlink()
        res = h.tools['twelvedata_quote'].handler(None, symbol='AAPL')
        assert res['isError'] and json.loads(text(res)) == dict(state='unknown', reason='key_missing')
    assert SEEN == []


def test_bad_arguments_are_refused_before_any_request(host):
    h, _ = host
    for kw in (dict(symbol='A&apikey=x'), dict(symbol=''), dict(symbol='AAPL', interval='3days')):
        t = h.tools['twelvedata_time_series' if 'interval' in kw else 'twelvedata_quote']
        assert json.loads(text(t.handler(None, **kw)))['state'] == 'refused'
    assert SEEN == []
