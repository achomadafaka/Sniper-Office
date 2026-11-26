import io
import json

from sniper_office import hermes_child as H


def test_relay_maps_hermes_stream_json(capsys):
    lines = [json.dumps(x) + '\n' for x in (dict(type='system', subtype='init', session_id=''), dict(type='text', text='checked the books'),
             dict(type='result', session_id='local-abc', exit_code=0, tokens=dict(input=10, output=2, cache_read=4, cache_write=0)))]
    session, usage, failed, stalled = H.relay(dict(session_id=None), io.StringIO(''.join(lines)))
    assert (session, failed, stalled) == ('local-abc', False, False)
    assert usage == dict(input_tokens=10, output_tokens=2, cache_read_input_tokens=4, cache_creation_input_tokens=0)
    out = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    assert [e['kind'] for e in out] == ['thought', 'session'] and out[0]['text'] == 'checked the books'


def test_relay_marks_a_failed_result():
    _, _, failed, _ = H.relay(dict(session_id='s'), io.StringIO(json.dumps(dict(type='result', session_id='s', exit_code=1)) + '\n'))
    assert failed


def test_write_home_has_fake_token_mcp_bridge_and_soul(tmp_path):
    H.write_home(dict(account_id='acct', model='gpt-x', system='You coordinate.'), home=str(tmp_path))
    auth = json.loads((tmp_path / 'auth.json').read_text())
    tok = auth['credential_pool']['openai-codex'][0]['access_token']
    assert tok.endswith('.c2ln') and auth['credential_pool']['openai-codex'][0]['refresh_token'] == ''      # unsigned shape only: worthless outside the proxy
    cfg = json.loads((tmp_path / 'config.yaml').read_text())
    assert cfg['model'] == dict(default='gpt-x', provider='openai-codex') and cfg['mcp_servers']['office']['args'][0] == '-c'
    assert (tmp_path / 'SOUL.md').read_text() == 'You coordinate.' and oct((tmp_path / 'auth.json').stat().st_mode)[-3:] == '600'


def test_hermes_session_absent_without_a_hermes_home(monkeypatch, tmp_path):
    from sniper_office import seat_retention as R
    import subprocess
    calls = []
    def fake(*argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1 if argv[0] == 'test' else 0, stdout='')
    monkeypatch.setattr(R, '_as_office', fake)
    assert R.session_exists(str(tmp_path), 'old-codex-thread', family='hermes') is False and [c[0] for c in calls] == ['test']
