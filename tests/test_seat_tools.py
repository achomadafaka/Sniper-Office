import json
import os
import socket
import sqlite3

import pytest

from sniper_office import seat_events as E,seat_tools as T,work as W


@pytest.fixture
def tools(tmp_path,monkeypatch):
    monkeypatch.setattr(T.SeatTools,'_stop_units',lambda self:(self.__dict__.setdefault('stops',[]).append(1),getattr(self,'stop_ok',True))[1])
    db=tmp_path/'o.db'
    conn=sqlite3.connect(db,isolation_level=None);W.ensure_schema(conn);E.ensure_schema(conn)
    conn.execute("INSERT INTO office_v2_roster VALUES('head','ep','head',1,2,0,1.)");conn.execute("INSERT INTO office_v2_roster VALUES('reviewer','ep','reviewer',1,1,0,1.)");conn.close()
    t=T.SeatTools(actor='head',epoch='ep',generation=1,incarnation=2,workspace=tmp_path,path=str(tmp_path/'h.sock'),db_path=str(db),clock=lambda:5.,office_uid=os.getuid())
    policy=tmp_path/'search.json';policy.write_text('{"daily_budget":30}')
    t.search_broker=T.S.Broker(tmp_path/'search.db',policy,clock=lambda:5.)
    t.start();yield t;t.stop()


def _rpc(t,lines):
    c=socket.socket(socket.AF_UNIX);c.settimeout(5);c.connect(t.path);f=c.makefile('rwb')
    out=[]
    for m in lines:
        f.write(json.dumps(m).encode()+b'\n');f.flush();out.append(json.loads(f.readline()))
    c.close();return out


def _call(t,name,**args):
    return _rpc(t,[dict(jsonrpc='2.0',id=1,method='tools/call',params=dict(name=name,arguments=args))])[0]['result']


def _db(t):return sqlite3.connect(t.db_path)


def test_mail_sender_comes_from_the_socket_not_the_model(tools):
    assert not _call(tools,'mail_send',to='reviewer',text='hi')['isError']
    assert _call(tools,'mail_send',to='reviewer',text='hi',from_actor='operator')['isError']
    row=_db(tools).execute('SELECT from_actor_id,from_generation FROM office_dm_events').fetchone()
    assert row==('head',1)
    assert _db(tools).execute('SELECT epoch_id,from_incarnation,to_incarnation FROM office_v2_mail_bind').fetchone()==('ep',2,1)


def test_read_mail_is_once(tools):
    conn=_db(tools);E.operator_dm(conn,to_actor_id='head',client_id='client-0001',text='hello',now=1.);conn.close()
    first=json.loads(_call(tools,'mail_read')['content'][0]['text'])
    assert [(m['sender'],m['text']) for m in first]==[('operator','hello')]
    assert json.loads(_call(tools,'mail_read')['content'][0]['text'])==[]


def test_unknown_tool_and_bad_args_are_typed(tools):
    assert _call(tools,'read_env')['content'][0]['text']=='unknown_tool'
    assert _call(tools,'sleep',until_s='x',reason='r')['isError']


def test_wrong_peer_gets_nothing(tools):
    tools.office_uid=os.getuid()+1
    c=socket.socket(socket.AF_UNIX);c.settimeout(5);c.connect(tools.path);c.sendall(b'{"id":1,"method":"tools/list"}\n')
    try:assert c.recv(10)==b''
    except ConnectionError:pass


def test_stop_ends_the_incarnation(tools):
    open_client=socket.socket(socket.AF_UNIX);open_client.settimeout(5);open_client.connect(tools.path)
    open_client.sendall(json.dumps(dict(jsonrpc='2.0',id=1,method='tools/list')).encode()+b'\n');assert open_client.recv(4096)
    path=tools.path;tools.stop()
    late=socket.socket(socket.AF_UNIX)
    with pytest.raises(OSError):late.connect(path)
    try:
        open_client.sendall(json.dumps(dict(jsonrpc='2.0',id=2,method='tools/list')).encode()+b'\n')
        assert open_client.recv(4096)==b''
    except ConnectionError:pass   # cut before the send or during the read: both are the refusal


def test_long_output_is_stored_truncated(tools):
    conn=_db(tools);E.record_event(conn,epoch_id='e',actor_id='a',generation=1,incarnation=1,at=1.,kind='tool_result',text='é'*5000)
    assert conn.execute('SELECT truncated FROM office_v2_events').fetchone()==(1,)


def test_a_closed_incarnation_refuses_calls_and_stops_units_twice(tools):
    tools.stop()
    assert tools.call('mail_read',{})['content'][0]['text']=='incarnation_closed'
    assert len(tools.stops)==2


def test_stop_waits_for_a_call_in_flight(tools):
    import threading,time
    release=threading.Event();seen=[]
    tools._mail_read=lambda:(release.wait(5),T._result('x'))[1]
    worker=threading.Thread(target=lambda:seen.append(tools.call('mail_read',{})));worker.start()
    time.sleep(.1)
    stopper=threading.Thread(target=tools.stop);stopper.start();time.sleep(.1)
    assert stopper.is_alive() and len(tools.stops)==1
    release.set();stopper.join(5);worker.join(5)
    assert len(tools.stops)==2 and seen


def test_failed_cleanup_reads_not_clean(tools):
    tools.stop_ok=False
    assert tools.stop() is False and tools.clean is False


def test_stop_timeout_is_not_clean(monkeypatch):
    def boom(*a,**k):raise T.subprocess.TimeoutExpired('x',1)
    monkeypatch.setattr(T.subprocess,'run',boom)
    assert T.stop_tool_units('head',995) is False


@pytest.mark.parametrize('fd',[1,2])
def test_shell_stops_output_before_the_producer_finishes(tools,monkeypatch,tmp_path,fd):
    import sys
    marker=tmp_path/'finished'
    code='import os,pathlib; os.write(%d,b"x"*1000000); pathlib.Path(%r).touch()'%(fd,str(marker))
    monkeypatch.setattr(T.B,'unit_args',lambda *a,**k:[sys.executable,'-c',code])
    monkeypatch.setattr(T.B,'tool_args',lambda *a,**k:[])
    out=tools._shell('ignored')
    assert out['isError'] and out['content'][0]['text']=='output_limit'
    assert not marker.exists()


@pytest.mark.parametrize('state,rc,ok',[('inactive',0,True),('failed',0,True),('active',0,False),('deactivating',0,False),('',1,False)])
def test_stop_unit_needs_proof(monkeypatch,state,rc,ok):
    monkeypatch.setattr(T.subprocess,'run',lambda argv,**k:type('R',(),{'returncode':rc if 'show' in argv else 5,'stdout':state})())
    assert T.stop_unit('u',995) is ok


@pytest.mark.parametrize('complete',[False,True])
def test_oversized_rpc_line_closes_before_dispatch(tools,complete):
    msg=json.dumps(dict(jsonrpc='2.0',id=1,method='tools/call',params=dict(name='request',arguments=dict(kind='x',detail='x'*70000,why='x')))).encode()
    client=socket.socket(socket.AF_UNIX);client.settimeout(1);client.connect(tools.path)
    try:
        try:client.sendall(msg+(b'\n' if complete else b''))
        except ConnectionError:pass
        try:assert client.recv(4096)==b''
        except ConnectionError:pass
    finally:client.close()
    assert _db(tools).execute('SELECT count(*) FROM office_v2_events').fetchone()==(0,)


def test_listener_loop_starting_after_stop_exits_cleanly(tools):
    tools.stop()
    tools._serve()


@pytest.mark.parametrize('url',['http://a.test/','file:///etc/passwd','notaurl','chrome://settings'])
def test_browse_refuses_anything_but_https(tools,url):
    assert _call(tools,'browse',url=url)['isError']


def test_browse_records_host_and_path_only_and_quotes_the_url(tools,monkeypatch):
    seen=[];monkeypatch.setattr(tools,'_shell',lambda c,timeout_s:(seen.append(c),T._result('{}'))[1])
    _call(tools,'browse',url="https://a.test/p?secret=1';id;'")
    row=_db(tools).execute("SELECT text,web_tainted FROM office_v2_events WHERE kind='browser_visit'").fetchone()
    assert row==('a.test/p',1) and "'\"'\"'" in seen[0]


def test_web_search_quotes_and_bounds_the_query(tools,monkeypatch):
    seen=[];monkeypatch.setattr(tools,'_shell',lambda c,timeout_s:(seen.append(c),T._result('{}'))[1])
    _call(tools,'web_search',query="a';id;'")
    assert "'\"'\"'" in seen[0]
    assert _call(tools,'web_search',query='x'*401)['isError'] and _call(tools,'web_search',query=' ')['isError']


def test_search_page_shapes():
    from sniper_office import seat_search as S
    page='<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.test%2Fp&rut=x">Ti<b>tle</b></a><a class="result__snippet" href="x">Sni  ppet</a>'
    assert S.parse(page)==dict(state='known',results=[dict(title='Title',url='https://a.test/p',snippet='Sni ppet')])
    assert S.parse('<div class="no-results">No results.</div>')==dict(state='known',results=[])
    assert S.parse('<html>please verify you are human</html>')['state']=='unknown'


def test_bing_page_shape_and_link_unwrap():
    import base64
    from sniper_office import seat_search as S
    wrapped=base64.urlsafe_b64encode(b'https://a.test/p').decode().rstrip('=')
    page='<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&&p=x&u=a1%s&ntb=1">Ti<b>tle</b></a></h2><p>Sni  ppet</p></li>'%wrapped
    assert S.parse_bing(page)==dict(state='known',results=[dict(title='Title',url='https://a.test/p',snippet='Sni ppet')])
    assert S.parse_bing('<html>challenge</html>')['state']=='unknown'


def test_search_falls_through_a_challenged_engine_and_says_so(monkeypatch):
    from sniper_office import seat_search as S
    pages={S.TAVILY:'not json',S.URL:'<html>Select all squares containing a duck</html>','https://www.bing.com/search?q=':'<li class="b_algo"><h2><a href="https://a.test/">T</a></h2></li>'}
    monkeypatch.setattr(S,'_fetch',lambda prefix,q:pages[prefix])
    out=S.search('q')
    assert out['state']=='known' and out['engine']=='bing' and out['engine_prior']=='unrelated_on_two_controls' and out['failed']=={'tavily':'result_shape_unknown','duckduckgo':'result_markup_absent'}
    pages['https://www.bing.com/search?q=']='<html>no</html>'
    assert S.search('q')['state']=='unknown'


def test_tavily_shape_and_empty_is_known():
    from sniper_office import seat_search as S
    assert S.parse_tavily('{"results":[{"title":"T","url":"https://a.test/","content":"C  D"},{"url":"ftp://x"}]}')==dict(state='known',results=[dict(title='T',url='https://a.test/',snippet='C D')],dropped_malformed=1)
    assert S.parse_tavily('{"results":[]}')==dict(state='known',results=[])
    assert S.parse_tavily('{"error":"limit"}')['state']=='unknown'


@pytest.mark.parametrize('body',['{"results":[{}]}','{"results":[null,{},{"url":null}]}','{"results":[{"url":"http-not-a-url"}]}','{"results":[{"url":"https://"}]}'])
def test_tavily_unusable_rows_are_not_no_hits(body):
    from sniper_office import seat_search as S
    assert S.parse_tavily(body)['state']=='unknown'


def test_tavily_mixed_rows_keep_the_good_and_count_the_bad():
    from sniper_office import seat_search as S
    out=S.parse_tavily('{"results":[{"url":"https://a.test/"},{}]}')
    assert out['state']=='known' and len(out['results'])==1 and out['dropped_malformed']==1


def test_a_failed_taint_save_fences_the_tool_and_retries(tools,monkeypatch):
    ran=[];monkeypatch.setattr(tools,'_shell',lambda c,timeout_s:(ran.append(1),T._result('{"state":"known","results":[]}'))[1])
    calls=[]
    def save():
        calls.append(1)
        if len(calls)==1:raise OSError()
    tools.on_taint=save
    assert _call(tools,'web_search',query='q')['content'][0]['text']=='taint_save_failed' and not ran and tools.tainted is False
    assert not _call(tools,'web_search',query='q')['isError'] and ran and tools.tainted is True and len(calls)==2


def test_search_broker_is_used_for_real_tool_calls(tools,monkeypatch):
    calls=[]
    monkeypatch.setattr(tools,'_shell',lambda *a,**k:(calls.append(a),T._result('{"state":"known","results":[]}'))[1])
    first=json.loads(_call(tools,'web_search',query='exact')['content'][0]['text'])
    second=json.loads(_call(tools,'web_search',query='exact')['content'][0]['text'])
    assert not first['cached'] and second['cached'] and len(calls)==1 and second['age_s']==0
    monkeypatch.setattr(tools,'_shell',lambda *a,**k:T._result('{"state":"unknown","reason":"budget_exhausted"}'))
    out=_call(tools,'web_search',query='different')
    assert out['isError'] and json.loads(out['content'][0]['text'])['reason']=='budget_exhausted'


def test_cached_search_taints_before_lookup_without_running_shell(tools,tmp_path,monkeypatch):
    tools.search_broker.search('cached',lambda:dict(state='known',results=[]))
    durable=tmp_path/'durable-taint.json'
    tools.on_taint=lambda:durable.write_text('{"tainted_by_web":true}')
    lookup=tools.search_broker.search
    def checked(query,fetch):
        assert json.loads(durable.read_text())['tainted_by_web'] and tools.tainted
        return lookup(query,fetch)
    monkeypatch.setattr(tools.search_broker,'search',checked)
    monkeypatch.setattr(tools,'_shell',lambda *a,**k:pytest.fail('cache hit ran shell'))
    out=tools._web_search('cached')
    assert not out['isError'] and json.loads(out['content'][0]['text'])['cached']
    assert tools._labels()['tainted_by_web']


def test_failed_durable_taint_fences_even_a_cache_hit(tools,monkeypatch):
    tools.search_broker.search('cached',lambda:dict(state='known',results=[]))
    def fail():raise OSError('disk unavailable')
    tools.on_taint=fail
    monkeypatch.setattr(tools.search_broker,'search',lambda *a:pytest.fail('lookup before durable taint'))
    monkeypatch.setattr(tools,'_shell',lambda *a,**k:pytest.fail('shell before durable taint'))
    with pytest.raises(ValueError,match='taint_save_failed'):tools._web_search('cached')
    assert not tools.tainted


def test_mail_reaches_a_seat_by_role_and_an_ambiguous_role_refuses(tools):
    conn=_db(tools);conn.execute("INSERT INTO office_v2_roster VALUES('ag_s','ep','researcher',1,1,0,1.)");conn.commit()
    assert not _call(tools,'mail_send',to='Researcher',text='hi')['isError']
    assert _db(tools).execute('SELECT to_actor_id FROM office_dm_events').fetchone()==('ag_s',)
    conn.execute("INSERT INTO office_v2_roster VALUES('ag_t','ep','researcher',1,1,0,1.)");conn.commit()
    assert _call(tools,'mail_send',to='researcher',text='hi')['isError']
    assert not _call(tools,'mail_send',to='ag_t',text='hi')['isError']


def test_a_seat_replies_to_the_operator_without_waking_anyone(tools):
    assert not _call(tools,'mail_send',to='Operator',text='ack')['isError']
    conn=_db(tools)
    assert conn.execute("SELECT from_actor_id,to_actor_id,text FROM office_dm_events").fetchone()==('head','operator','ack')
    assert conn.execute('SELECT count(*) FROM office_dm_pending').fetchone()==(0,)
    assert _call(tools,'mail_send',to='operator',text='x',from_actor='operator')['isError']


def test_operator_influence_carries_one_wake_and_crew_mail_never_sets_it(tools):
    saved=[];tools.on_operator_read=lambda:saved.append(1)
    conn=_db(tools);E.seat_dm(conn,from_actor='crew',from_generation=0,from_incarnation=0,to_actor_id='head',text='note',now=1.);conn.close()
    _call(tools,'mail_read')
    assert tools._labels()['operator_influenced'] is False and saved==[]
    tools.operator_prior=True
    assert tools._labels()['operator_influenced'] is True
    tools.operator_prior=False
    conn=_db(tools);E.operator_dm(conn,to_actor_id='head',client_id='client-0011',text='hi',now=1.);conn.close()
    _call(tools,'mail_read')
    assert saved==[1] and tools._labels()['operator_influenced'] is True


def test_a_stale_seat_identity_cannot_reply_to_the_operator(tools):
    conn=_db(tools)
    with pytest.raises(E.OperatorDmRefused):
        E.seat_to_operator(conn,from_actor='head',from_generation=1,from_incarnation=1,text='old',now=1.)
    assert conn.execute('SELECT count(*) FROM office_dm_events').fetchone()==(0,)


def test_the_operator_read_saves_on_the_batch_read_and_a_failed_save_keeps_the_mail(tools):
    conn=_db(tools);E.operator_dm(conn,to_actor_id='head',client_id='client-0021',text='hi',now=1.);conn.close()
    def fail():raise OSError('disk')
    tools.on_operator_read=fail
    assert _call(tools,'mail_read')['isError'] and tools.operator_mail is False
    assert _db(tools).execute('SELECT count(*) FROM office_dm_pending').fetchone()==(1,)
    saved=[];tools.on_operator_read=lambda:saved.append(_db(tools).execute('SELECT count(*) FROM office_dm_pending').fetchone()[0])
    assert 'hi' in _call(tools,'mail_read')['content'][0]['text'] and saved==[1]


def test_a_failed_page_keeps_the_claim_and_can_page_again(tools):
    def fail(t):raise ValueError('x')
    tools.page_operator=fail
    assert json.loads(_call(tools,'notify_operator',case='c',evidence='e',risks='r')['content'][0]['text'])['delivery']=='telegram_failed'
    tools.page_operator=lambda t:None
    assert json.loads(_call(tools,'notify_operator',case='d',evidence='e',risks='r')['content'][0]['text'])['paged'] is True


def test_two_threads_take_one_page_slot_and_a_crash_after_reserving_holds_it(tools):
    import threading
    pages=[];start=threading.Barrier(2)
    tools.page_operator=pages.append
    def go(case):
        start.wait();_call(tools,'notify_operator',case=case,evidence='e',risks='r')
    threads=[threading.Thread(target=go,args=(c,)) for c in 'xy']
    [t.start() for t in threads];[t.join() for t in threads]
    assert len(pages)==1
    conn=_db(tools);conn.execute("UPDATE office_v2_notifications SET state='page_reserved'");conn.commit()
    assert json.loads(_call(tools,'notify_operator',case='z',evidence='e',risks='r')['content'][0]['text'])['delivery']=='queued_in_inbox' and len(pages)==1


def test_multibyte_text_stays_inside_the_mail_limit(tools):
    tools.page_operator=lambda t:None
    assert not _call(tools,'notify_operator',case='é'*1200,evidence='é'*1200,risks='é'*1200)['isError']


def test_mail_from_a_tainted_seat_labels_the_reader(tools):
    conn=_db(tools)
    E.seat_dm(conn,from_actor='reviewer',from_generation=1,from_incarnation=1,to_actor_id='head',text='clean',now=1.,sender_tainted=False)
    conn.commit();_call(tools,'mail_read');assert tools._labels()['peer_tainted_read'] is False
    E.seat_dm(conn,from_actor='reviewer',from_generation=1,from_incarnation=1,to_actor_id='head',text='web',now=2.,sender_tainted=True)
    conn.commit();_call(tools,'mail_read')
    assert tools._labels()['peer_tainted_read'] is True and tools._labels()['tainted_by_web'] is False


def test_the_claude_harness_stays_without_native_tools():
    """Alpha 2026-10-30: the harness holds a refresh token beside any native tool, so "all tools on" never reaches it."""
    import ast
    tree=ast.parse(open('sniper_office/seat_child.py').read())
    call=next(n for n in ast.walk(tree) if isinstance(n,ast.Call) and getattr(n.func,'id',None)=='ClaudeAgentOptions')
    args={k.arg:ast.literal_eval(k.value) for k in call.keywords if k.arg in ('tools','allowed_tools','setting_sources','strict_mcp_config')}
    assert args=={'tools':[],'allowed_tools':['mcp__office__*'],'setting_sources':[],'strict_mcp_config':True}


def test_a_database_failure_inside_a_tool_is_a_typed_refusal(tools,monkeypatch):
    def boom(**kw):raise sqlite3.OperationalError('unable to open database file')
    monkeypatch.setattr(tools,'_mail_read',boom,raising=True)
    out=tools._call('mail_read',{})
    assert out['isError'] is True and out['content'][0]['text']=='database_unknown'


@pytest.mark.parametrize('text', ['x'*5000, 'é'*3000, 'x'*4096, 'é'*2048], ids=['ascii-over', 'utf8-over', 'ascii-boundary', 'utf8-boundary'])
def test_tool_call_preserves_response_and_marks_audit_byte_truncation(tools, text):
    tools._mail_read=lambda:T._result(text)
    response=tools.call('mail_read', {})
    assert response['content'][0]['text']==text
    with _db(tools) as conn:
        stored,truncated=conn.execute("SELECT text,truncated FROM office_v2_events WHERE kind='tool_result'").fetchone()
    assert stored==text.encode('utf-8')[:4096].decode('utf-8', 'ignore')
    assert truncated==int(len(text.encode('utf-8'))>4096)


def test_mail_read_gives_the_host_role_of_a_roster_sender(tools):
    c=_db(tools)
    role=c.execute("SELECT role FROM office_v2_roster WHERE actor_id='head'").fetchone()[0]
    assert not _call(tools,'mail_send',to='head',text='hi')['isError']
    mail=json.loads(_call(tools,'mail_read')['content'][0]['text'])
    assert mail and mail[0]['sender']=='head' and mail[0]['sender_role']==role
