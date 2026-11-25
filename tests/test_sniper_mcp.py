"""The sniper MCP bridge: a stdlib stdio client that exposes only allowlisted read/analyze/replay tools, bounded and marked as data."""
import json
import sys
import textwrap

from sniper_office import plugins as P, sniper_mcp as M

FAKE = textwrap.dedent('''
    import json, sys, time
    TOOLS = [("get_runtime_health", "Health."), ("analyze_historical_outcomes", "Outcomes."), ("run_replay_simulation", "Replay."),
             ("create_paper_strategy", "WRITE."), ("activate_paper_strategy", "WRITE."), ("brand_new_tool", "Unknown.")]
    for line in sys.stdin:
        m = json.loads(line)
        if "id" not in m: continue
        meth, p = m["method"], m.get("params", {})
        if meth == "initialize": r = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "fake", "version": "1"}}
        elif meth == "tools/list":
            r = {"tools": [{"name": n, "description": d, "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}, "port": {"type": "integer"}}, "required": []}} for n, d in TOOLS]}
        elif meth == "tools/call":
            a = p["arguments"]
            if a.get("limit") == 99: time.sleep(30)
            if a.get("limit") == 7:
                import socket
                try: socket.create_connection(("127.0.0.1", int(a["port"])), 2); net = "reachable"
                except OSError: net = "cut"
                r = {"content": [{"type": "text", "text": net}], "isError": False}
                print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True); continue
            text = "x" * 100000 if a.get("limit") == 5 else json.dumps({"tool": p["name"], "args": a})
            r = {"content": [{"type": "text", "text": text}], "isError": False}
        else: r = {}
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
''')


def setup(tmp_path, timeout_s=10):
    (tmp_path / 'fake.py').write_text(FAKE)
    (tmp_path / 'bad.py').write_text("open(%r,'w').write('ran')" % str(tmp_path / 'marker'))
    mcp = {'mcpServers': {'sniper': {'command': sys.executable, 'args': ['fake.py']}, 'supabase': {'command': sys.executable, 'args': ['bad.py']}}}
    (tmp_path / '.mcp.json').write_text(json.dumps(mcp))
    return P.load(dict(plugins=['sniper_office.sniper_mcp'], sniper_mcp=dict(mcp_json=str(tmp_path / '.mcp.json'), timeout_s=timeout_s)))


def test_only_allowlisted_tools_are_exposed_and_supabase_never_runs(tmp_path):
    host = setup(tmp_path)
    assert set(host.tools) == {'sniper_get_runtime_health', 'sniper_analyze_historical_outcomes', 'sniper_run_replay_simulation'}
    assert all(t.group == 'sniper_mcp' and 'read-only' in t.description.lower() for t in host.tools.values())
    assert not (tmp_path / 'marker').exists()


def test_call_is_bounded_marked_as_data_and_drops_undeclared_arguments(tmp_path):
    host = setup(tmp_path)
    h = host.tools['sniper_get_runtime_health'].handler
    doc = json.loads(h(None, limit=2, evil='x')['content'][0]['text'])
    assert doc['untrusted_data'] is True and doc['source'] == 'sniper_mcp' and 'evil' not in doc['data']
    big = h(None, limit=5)
    assert len(big['content'][0]['text']) <= M.MAX_OUTPUT and json.loads(big['content'][0]['text'])['state'] == 'partial'


def test_timeout_answers_unknown(tmp_path):
    host = setup(tmp_path, timeout_s=1)
    res = host.tools['sniper_get_runtime_health'].handler(None, limit=99)
    assert res['isError'] and json.loads(res['content'][0]['text']) == dict(state='unknown', reason='timeout')


def test_missing_server_registers_nothing(tmp_path):
    host = P.load(dict(plugins=['sniper_office.sniper_mcp'], sniper_mcp=dict(mcp_json=str(tmp_path / 'none.json'))))
    assert host.tools == {}


def test_allowlist_holds_no_write_tool():
    names = {n for g in M.ALLOW.values() for n in g}
    assert names and not [n for n in names if n.startswith(('create_', 'activate_', 'set_', 'delete_', 'update_'))]
    assert set(M.ALLOW) == {'read', 'analyze', 'replay'}


def test_server_runs_without_network_and_is_not_launched_without_isolation(tmp_path, monkeypatch):
    import socket
    srv = socket.socket(); srv.bind(('127.0.0.1', 0)); srv.listen(1)
    host = setup(tmp_path)
    res = host.tools['sniper_get_runtime_health'].handler(None, limit=7, port=srv.getsockname()[1])
    assert json.loads(res['content'][0]['text'])['data'] == 'cut'
    monkeypatch.setattr(M.shutil, 'which', lambda n: None)
    res = host.tools['sniper_get_runtime_health'].handler(None, limit=1)
    assert json.loads(res['content'][0]['text']) == dict(state='unknown', reason='no_net_isolation')
    assert P.load(dict(plugins=['sniper_office.sniper_mcp'], sniper_mcp=dict(mcp_json=str(tmp_path / '.mcp.json')))).tools == {}
