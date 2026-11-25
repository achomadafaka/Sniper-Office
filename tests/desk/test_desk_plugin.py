"""The desk plug-in: registration, and a known and an unknown path for each tool group."""
import json
import os
import sqlite3
import stat
import sys
import time

import pytest

from sniper_office import plugins as P, seat_events as E, seat_tools as T, work as W
from sniper_office.desk import chain_data as C, wallet_network as N

NOW = 1_800_000_000.
SOL = '11111111111111111111111111111111'
GROUPS = ['paper', 'chain', 'network', 'brain']


def write_json(path, doc):
    path.write_text(json.dumps(doc))
    return str(path)


def ledger(path, orders=()):
    conn = sqlite3.connect(path)
    conn.executescript('CREATE TABLE orders(order_id,book_id,epoch_id,mint,pool,state,reason,requested_at,filled_at,closed_at,spend_usd,exit_usd,costs_usd,exit_reason,exit_fill_kind,exit_requested_at);'
                       'CREATE TABLE marks(id INTEGER PRIMARY KEY,order_id,at,lamports);CREATE TABLE cashflows(order_id,usd,at);CREATE TABLE capital_flows(book_id,usd,at);')
    conn.executemany('INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', orders)
    conn.execute("INSERT INTO capital_flows VALUES('ep',1000.,0)")
    conn.commit();conn.close()
    return str(path)


def desk(path, rows):
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE desk_tickets(id INTEGER PRIMARY KEY,ticket_id,network_id,mint,pool,stage,verdict,reason,ts,inputs_json)')
    conn.executemany("INSERT INTO desk_tickets(ticket_id,network_id,mint,pool,stage,verdict,reason,ts,inputs_json) VALUES(?,?,?,?,'hard_filter','PASS','pass',?,?)", rows)
    conn.commit();conn.close()
    return str(path)


def ticket(tid, age_s, top10=12.5):
    ts = time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime(NOW - age_s))
    return (tid, 'solana', 'mint_' + tid, 'pool_' + tid, ts, json.dumps(dict(evidence=dict(checks=dict(holders=dict(top10_pct=top10), cluster=dict(status='pass', reason='cluster_clear'))))))


@pytest.fixture
def env(tmp_path):
    return tmp_path


def seat(tmp_path, monkeypatch, role='judge', cfg=None, groups=GROUPS, pager=None):
    host = P.load(dict(plugins=['sniper_office.desk']))
    host.pager = pager
    monkeypatch.setattr(T.SeatTools, '_stop_units', lambda self: True)
    db = tmp_path / 'o.db'
    conn = sqlite3.connect(db, isolation_level=None);W.ensure_schema(conn);E.ensure_schema(conn)
    conn.execute('INSERT OR REPLACE INTO office_v2_roster VALUES(?,?,?,?,?,0,0)', ('ag_' + role, 'ep', role, 1, 1));conn.close()
    cfg = dict(cfg or {})
    cfg.setdefault('clock_file', write_json(tmp_path / 'clock.json', dict(offset_s=0., at=NOW)))
    return T.SeatTools(actor='ag_' + role, epoch='ep', generation=1, incarnation=1, workspace=tmp_path, path=str(tmp_path / 's.sock'), db_path=str(db), clock=lambda: NOW,
                       office_uid=os.getuid(), plugins=host, groups=groups, cfg=cfg)


def call(t, name, **args):
    out = t.call(name, args)
    text = out['content'][0]['text']
    try:
        return out['isError'], json.loads(text)
    except ValueError:
        return out['isError'], text


def test_registration_names_every_ported_tool_in_its_group_and_a_seat_without_the_group_cannot_name_it(tmp_path, monkeypatch):
    host = P.load(dict(plugins=['sniper_office.desk']))
    expected = {'results_view', 'book_view', 'sheet_read', 'feed_list', 'feed_filter_set', 'candidate_register', 'paper_order', 'paper_exit', 'book_halt', 'network_read', 'network_expand',
                'chain_wallet_history', 'chain_transaction', 'chain_funding_trace', 'chain_token_holders', 'brain_read', 'brain_note'}
    assert set(host.tools) == expected
    assert {t.group for t in host.tools.values()} == set(GROUPS)
    assert not any('order' in n and host.tools[n].group != 'paper' for n in host.tools)
    plain = seat(tmp_path, monkeypatch, groups=())
    assert expected.isdisjoint(plain._names()) and plain.call('book_view', {})['content'][0]['text'] == 'unknown_tool'
    assert expected <= seat(tmp_path, monkeypatch)._names()


def test_unconfigured_seat_gets_unknown_with_a_reason_from_every_group(tmp_path, monkeypatch):
    t = seat(tmp_path, monkeypatch)
    for name, args, reason in [('feed_list', {}, 'desk_config_unknown'), ('sheet_read', dict(ticket_id='x'), 'desk_config_unknown'), ('results_view', {}, 'ledger_config_unknown'),
                               ('book_view', {}, 'ledger_config_unknown'), ('chain_transaction', dict(signature='s'), 'chain_config_unknown'), ('chain_token_holders', dict(mint=SOL), 'chain_config_unknown'),
                               ('network_read', dict(seed=SOL), 'network_config_unknown'), ('network_expand', dict(seed=SOL), 'chain_config_unknown'),
                               ('brain_read', dict(query='x'), 'brain_config_unknown'), ('brain_note', dict(text='x'), 'brain_config_unknown')]:
        error, doc = call(t, name, **args)
        assert error and doc == dict(state='unknown', reason=reason), name


def test_a_seat_not_in_the_roster_is_refused_and_a_stale_clock_is_unknown(tmp_path, monkeypatch):
    t = seat(tmp_path, monkeypatch, cfg=dict(desk_db=desk(tmp_path / 'd.db', [ticket('a', 5)])))
    t.incarnation = 2
    assert call(t, 'feed_list')[1] == dict(state='refused', reason='seat_not_in_epoch')
    t = seat(tmp_path, monkeypatch, cfg=dict(desk_db=desk(tmp_path / 'd2.db', [ticket('a', 5)]), clock_file=write_json(tmp_path / 'old.json', dict(offset_s=0., at=NOW - 7200))))
    assert call(t, 'feed_list')[1] == dict(state='unknown', reason='clock_unknown')


def test_feed_list_shows_fresh_stored_passes_applies_the_seats_filter_and_a_stale_desk_is_unknown_not_empty(tmp_path, monkeypatch):
    path = desk(tmp_path / 'd.db', [ticket('old', 7000), ticket('hi', 20, top10=60.), ticket('lo', 10, top10=12.5)])
    t = seat(tmp_path, monkeypatch, cfg=dict(desk_db=path))
    error, doc = call(t, 'feed_list')
    assert not error and doc['state'] == 'partial' and [i['ticket_id'] for i in doc['items']] == ['lo', 'hi'] and doc['items'][0]['facts']['top10_pct'] == 12.5
    assert call(t, 'feed_filter_set', rules=[dict(path='holders.top10_pct', op='gt', value=50)], reason='whales')[1]['state'] == 'known'
    doc = call(t, 'feed_list')[1]
    assert [i['ticket_id'] for i in doc['items']] == ['lo'] and doc['hidden_count'] == 1
    assert [i['ticket_id'] for i in call(t, 'feed_list', include_hidden=True)[1]['items']] == ['lo', 'hi']
    assert call(t, 'feed_filter_set', rules=[dict(path='x', op='bad', value=1)], reason='r')[1]['reason'] == 'rules_invalid'
    stale = seat(tmp_path, monkeypatch, cfg=dict(desk_db=desk(tmp_path / 'd2.db', [ticket('a', 5000)])))
    error, doc = call(stale, 'feed_list')
    assert error and doc['state'] == 'unknown' and doc['reason'] == 'desk_stale'
    broken = seat(tmp_path, monkeypatch, cfg=dict(desk_db=str(tmp_path / 'missing.db')))
    assert call(broken, 'feed_list')[1] == dict(state='unknown', reason='desk_unreadable')


def test_sheet_read_is_partial_stored_evidence_and_an_unknown_ticket_is_refused(tmp_path, monkeypatch):
    t = seat(tmp_path, monkeypatch, cfg=dict(desk_db=desk(tmp_path / 'd.db', [ticket('a', 5)])))
    error, doc = call(t, 'sheet_read', ticket_id='a')
    assert not error and doc['state'] == 'partial' and doc['reason'] == 'full_sheet_not_built_here' and doc['evidence']['checks']['holders']['top10_pct'] == 12.5
    assert call(t, 'sheet_read', ticket_id='nope')[1] == dict(state='refused', reason='ticket_unknown')
    assert call(t, 'sheet_read', ticket_id='')[1] == 'bad_arguments'


ORDERS = [('o1', 'ep', 'ep', 'm1', 'p1', 'closed', None, NOW - 900, NOW - 880, NOW - 100, 25., 30., 1., 'tp', 'modelled', None),
          ('o2', 'ep', 'ep', 'm2', 'p2', 'open', None, NOW - 300, NOW - 280, None, 25., None, None, None, None, None)]


def test_results_and_book_view_read_the_ledger_and_stay_honest_about_equity(tmp_path, monkeypatch):
    cfg = dict(ledger_db=ledger(tmp_path / 'l.db', ORDERS), book_policy=write_json(tmp_path / 'bp.json', dict(policy=dict(max_concurrent=3, daily_loss_usd=100., rail_bounds=dict(sl_pct=[10, 40])))))
    t = seat(tmp_path, monkeypatch, cfg=cfg)
    error, doc = call(t, 'results_view')
    assert not error and [i['order_id'] for i in doc['items']] == ['o1'] and doc['items'][0]['net_usd'] == pytest.approx(4.)
    assert sqlite3.connect(t.db_path).execute('SELECT reads FROM desk_results_reads').fetchone() == (1,)
    error, doc = call(t, 'book_view')
    assert not error and [p['order_id'] for p in doc['positions']] == ['o2'] and doc['equity']['state'] == 'unknown' and doc['equity']['usd'] is None
    assert doc['daily_loss_usd'] == 100. and doc['entry_halt']['state'] == 'never_set' and len(doc['equity_snapshot_id']) == 64
    flat = seat(tmp_path, monkeypatch, cfg=dict(ledger_db=ledger(tmp_path / 'l2.db')))
    doc = call(flat, 'book_view')[1]
    assert doc['equity'] == dict(state='known', usd=1000., cash_usd=1000., unpriced_count=0) and doc['policy_state'] == 'unknown' and doc['daily_loss_halted'] is None
    assert call(seat(tmp_path, monkeypatch, role='researcher', cfg=cfg), 'results_view')[1]['reason'] == 'role_cannot_view_results'
    bad = seat(tmp_path, monkeypatch, cfg=dict(ledger_db=str(tmp_path / 'gone.db')))
    assert call(bad, 'results_view')[1] == dict(state='unknown', reason='ledger_unreadable')


def test_paper_order_checks_then_records_a_queued_request_and_never_claims_a_fill(tmp_path, monkeypatch):
    cfg = dict(desk_db=desk(tmp_path / 'd.db', [ticket('a', 5), ticket('b', 5)]), ledger_db=ledger(tmp_path / 'l.db', ORDERS), book_policy=write_json(tmp_path / 'bp.json', dict(policy=dict(rail_bounds=dict(sl_pct=[10, 40])))))
    t = seat(tmp_path, monkeypatch, cfg=cfg)
    plan = dict(reason='why', thesis='t', disproof='d', exit_plan='e')
    assert call(t, 'paper_order', ticket_id='a', reason='why')[1]['reason'] == 'plan_required'
    assert call(t, 'paper_order', ticket_id='a', sl_pct=99, **plan)[1]['reason'] == 'rail_out_of_bounds:sl_pct'
    assert call(t, 'paper_order', ticket_id='zzz', **plan)[1]['reason'] == 'ticket_unknown'
    assert call(seat(tmp_path, monkeypatch, role='scout', cfg=cfg), 'paper_order', ticket_id='a', **plan)[1]['reason'] == 'role_cannot_order'
    error, doc = call(t, 'paper_order', ticket_id='a', sl_pct=20, **plan)
    assert not error and doc['state'] == 'queued' and doc['filled'] is False and doc['order_id'] is None
    assert call(t, 'paper_order', ticket_id='a', **plan)[1]['state'] == 'duplicate'
    head = seat(tmp_path, monkeypatch, role='head', cfg=cfg)
    assert call(head, 'book_halt', action='halt', reason='stop')[1] == dict(state='halt')
    assert call(t, 'paper_order', ticket_id='b', **plan)[1]['reason'] == 'book_halted'
    assert call(t, 'book_halt', action='resume', reason='x')[1]['reason'] == 'halt_not_allowed'
    assert call(head, 'book_view')[1]['entry_halt']['state'] == 'halted'
    assert call(head, 'book_halt', action='resume', reason='ok')[1] == dict(state='resume')
    assert call(t, 'paper_order', ticket_id='b', **plan)[1]['state'] == 'queued'


def test_paper_exit_needs_an_open_order_in_this_book_and_records_one_request(tmp_path, monkeypatch):
    t = seat(tmp_path, monkeypatch, cfg=dict(ledger_db=ledger(tmp_path / 'l.db', ORDERS)))
    assert call(t, 'paper_exit', order_id='nope', reason='r')[1]['reason'] == 'order_not_in_this_book'
    assert call(t, 'paper_exit', order_id='o1', reason='r')[1]['reason'] == 'not_open'
    assert call(t, 'paper_exit', order_id='o2', reason='r')[1]['state'] == 'queued'
    assert call(t, 'paper_exit', order_id='o2', reason='again')[1]['reason'] == 'exit_already_requested'
    assert call(seat(tmp_path, monkeypatch, cfg={}), 'paper_exit', order_id='o2', reason='r')[1] == dict(state='unknown', reason='ledger_config_unknown')


def test_candidate_register_keeps_the_first_registration(tmp_path, monkeypatch):
    t = seat(tmp_path, monkeypatch)
    first = call(t, 'candidate_register', network_id='solana', mint=SOL)[1]
    t.clock = lambda: NOW + 99
    again = call(t, 'candidate_register', network_id='solana', mint=SOL)[1]
    assert first['state'] == 'registered' and again['known_at'] == first['known_at'] == NOW and first['corrected_known_at'] == NOW
    assert call(t, 'candidate_register', network_id='', mint=SOL)[1]['reason'] == 'candidate_invalid'


def env_file(tmp_path, url='https://rpc.example.invalid/?k=abc'):
    path = tmp_path / 'research.env'
    path.write_text('MEMECOIN_RESEARCH_HELIUS_RPC_URL=%s\n' % url)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return str(path)


def test_chain_tools_go_through_the_configured_key_and_a_bad_key_or_missing_file_is_unknown(tmp_path, monkeypatch):
    calls = []
    def fake_rpc(url, method, params):
        calls.append((url, method))
        return 200, dict(result=dict(value=[dict(address=SOL, amount='5', uiAmount=0.0, decimals=0)]))
    monkeypatch.setattr(C, '_rpc', fake_rpc)
    real = C.ChainData
    monkeypatch.setattr(C, 'ChainData', lambda *a, **k: real(*a, **dict(k, rpc=fake_rpc)))
    t = seat(tmp_path, monkeypatch, cfg=dict(research_env_file=env_file(tmp_path)))
    error, doc = call(t, 'chain_token_holders', mint=SOL)
    assert calls and calls[0][0].startswith('https://rpc.example.invalid') and 'fetch_id' in doc, doc
    assert t.plugin_state['chain_fetch_ids'] == [doc['fetch_id']]
    assert P.load(dict(plugins=['sniper_office.desk'])).label_sources[0](t)['chain_fetch_ids'] == [doc['fetch_id']]
    assert 'rpc.example' not in json.dumps(doc)
    bad = seat(tmp_path, monkeypatch, cfg=dict(research_env_file=env_file(tmp_path, url='http://insecure.invalid'), chain_db=str(tmp_path / 'other.db')))
    error, doc = call(bad, 'chain_token_holders', mint=SOL)
    assert doc['state'] == 'unknown' and 'insecure' not in json.dumps(doc)
    gone = seat(tmp_path, monkeypatch, cfg=dict(research_env_file=str(tmp_path / 'none.env'), chain_db=str(tmp_path / 'other2.db')))
    assert call(gone, 'chain_transaction', signature='s' * 80)[1]['state'] == 'unknown'


def test_network_read_needs_its_db_and_reads_edges_with_proof_state(tmp_path, monkeypatch):
    path = tmp_path / 'net.db'
    t = seat(tmp_path, monkeypatch, cfg=dict(wallet_network_db=str(path)))
    assert call(t, 'network_read', seed=SOL)[1] == dict(state='unknown', reason='network_db_missing')
    assert not path.exists()
    net = N.WalletNetwork(path)
    assert net.add_edge(src=SOL, dst='So11111111111111111111111111111111111111112', edge_type='funded_by', actor='x')['state'] != 'refused'
    error, doc = call(t, 'network_read', seed=SOL)
    assert not error and doc['state'] in ('known', 'partial') and doc['edges'] and doc['edges'][0]['proof_state'] == 'none'   # a seat's edge is never verified


def test_brain_uses_the_office_brain_with_the_seat_role(tmp_path, monkeypatch):
    pkg = tmp_path / 'pkg' / 'brain'
    pkg.mkdir(parents=True)
    log = tmp_path / 'calls.txt'
    (pkg / '__init__.py').write_text('')
    (pkg / '__main__.py').write_text(
        'import sys\nopen(%r, "a").write(" ".join(sys.argv[1:]) + "\\n")\n'
        'op = sys.argv[1]\n'
        'print({"ask": "[unverified] 45718b61 observation: x", "episode": "episode 5392ba90-ec5e-4b72-9915-5f896cb59616 (1 part(s))", "write": "written 1"}[op])\n' % str(log))
    t = seat(tmp_path, monkeypatch, cfg=dict(brain_dir=str(tmp_path), brain_package=str(tmp_path / 'pkg'), brain_python=sys.executable, role='judge'))
    error, _ = call(t, 'brain_read', query='q')
    assert not error and t.plugin_state['brain_fact_ids'] == ['45718b61']
    error, _ = call(t, 'brain_note', text='n')
    assert not error
    calls = log.read_text().splitlines()
    assert calls[1].startswith('episode --role judge --kind note') and '--episode 5392ba90-ec5e-4b72-9915-5f896cb59616' in calls[2] and 'actor:judge' in calls[2]
    assert call(t, 'brain_note', text=' ')[1]['reason'] == 'text_invalid'
    assert call(t, 'brain_note', text='--role head')[1]['reason'] == 'text_invalid'

