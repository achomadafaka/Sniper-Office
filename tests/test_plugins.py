"""A project's own tools, wake reasons and hooks reach a seat only through the plug-in host."""
import json
import os
import sqlite3
import sys
import types

import pytest

from sniper_office import plugins as P,seat_events as E,seat_host as H,seat_main as MAIN,seat_retention as R,seat_tools as T,work as W


def make_tools(tmp_path,monkeypatch,host,**kw):
    monkeypatch.setattr(T.SeatTools,'_stop_units',lambda self:True)
    db=tmp_path/'o.db';conn=sqlite3.connect(db,isolation_level=None);W.ensure_schema(conn);E.ensure_schema(conn);conn.close()
    return T.SeatTools(actor='head',epoch='ep',generation=1,incarnation=1,workspace=tmp_path,path=str(tmp_path/'h.sock'),db_path=str(db),clock=lambda:5.,office_uid=os.getuid(),plugins=host,**kw)


def events(t,kind):
    return [(text,json.loads(refs)) for text,refs in sqlite3.connect(t.db_path).execute('SELECT text,refs_json FROM office_v2_events WHERE kind=? ORDER BY rowid',(kind,))]


def test_named_module_registers_and_a_bad_name_or_missing_module_is_refused(monkeypatch):
    module=types.ModuleType('demo_office_plugin');module.register=lambda host:host.add_tool('ping','Answer pong.',{},[],lambda tools:'pong')
    monkeypatch.setitem(sys.modules,'demo_office_plugin',module)
    host=P.load(dict(plugins=['demo_office_plugin']))
    assert set(host.tools)=={'ping'} and P.load({}).tools=={}
    for bad in (['os; import x'],'demo_office_plugin',[1]):
        with pytest.raises(ValueError):P.load(dict(plugins=bad))
    with pytest.raises(ImportError):P.load(dict(plugins=['no_such_office_plugin']))


def test_plugin_tool_is_listed_called_with_the_seat_and_recorded(tmp_path,monkeypatch):
    host=P.Host({})
    host.add_tool('item_read','Read one item.',{'item_id':{'type':'string'}},['item_id'],lambda tools,item_id:dict(item=item_id,actor=tools.actor,project=tools.cfg['project']),
                  call_refs=lambda a:dict(item_id=a['item_id']),result_refs=lambda a,text:dict(size=len(text)))
    t=make_tools(tmp_path,monkeypatch,host,cfg=dict(project='demo'))
    listed={x['name']:x for x in t._answer(dict(id=1,method='tools/list'))['result']['tools']}
    assert set(listed)==set(T.TOOLS)|{'item_read'} and listed['item_read']['inputSchema']['required']==['item_id']
    out=t.call('item_read',dict(item_id='i1'))
    assert not out['isError'] and json.loads(out['content'][0]['text'])==dict(item='i1',actor='head',project='demo')
    assert events(t,'tool_call')[0][1]==dict(tool='item_read',item_id='i1') and events(t,'tool_result')[0][1]['size']>0
    assert t.call('item_read',dict(wrong='x'))['content'][0]['text']=='bad_arguments'


def test_grouped_tool_exists_only_for_seats_with_the_group_and_denied_tools_cannot_be_named(tmp_path,monkeypatch):
    host=P.Host({});host.add_tool('write_x','w',{},[],lambda tools:'done',group='writers')
    plain=make_tools(tmp_path,monkeypatch,host)
    assert 'write_x' not in plain._names() and plain.call('write_x',{})['content'][0]['text']=='unknown_tool'
    writer=make_tools(tmp_path,monkeypatch,host,groups=['writers'])
    assert writer.call('write_x',{})['content'][0]['text']=='done'
    writer.denied=frozenset({'write_x','shell'})
    assert writer.call('write_x',{})['isError'] and 'shell' not in writer._names()


def test_plugin_fault_is_a_typed_error_without_its_text_and_the_server_keeps_answering(tmp_path,monkeypatch):
    host=P.Host({})
    def boom(tools):raise RuntimeError('token=s3cret')
    host.add_tool('boom','b',{},[],boom);host.add_tool('odd','o',{},[],lambda tools:object());host.add_tool('ok','o',{},[],lambda tools:'fine')
    t=make_tools(tmp_path,monkeypatch,host)
    out=t.call('boom',{})
    assert out['isError'] and out['content'][0]['text']=='tool_failed' and 's3cret' not in json.dumps(events(t,'tool_result'))
    assert t.call('odd',{})['content'][0]['text'].startswith('"<object') and t.call('ok',{})['content'][0]['text']=='fine'


def test_a_plugin_can_replace_a_builtin_and_bad_registrations_are_refused(tmp_path,monkeypatch):
    host=P.Host({});host.add_tool('db_read','project reader',{'sql':{'type':'string'}},['sql'],lambda tools,sql:'project:'+sql)
    assert make_tools(tmp_path,monkeypatch,host).call('db_read',dict(sql='q'))['content'][0]['text']=='project:q'
    for bad in (dict(name='Bad Name'),dict(handler=None),dict(group='Not Ok'),dict(required='x')):
        args=dict(name='t',description='d',properties={},required=[],handler=lambda tools:1,group=None);args.update(bad)
        with pytest.raises(ValueError):host.add_tool(args.pop('name'),args.pop('description'),args.pop('properties'),args.pop('required'),args.pop('handler'),**args)


def test_labels_merge_plugin_sources_and_a_failing_source_says_unknown(tmp_path,monkeypatch):
    host=P.Host({});host.add_labels(lambda tools:dict(items_read=3))
    t=make_tools(tmp_path,monkeypatch,host,wake_labels=dict(model='m'))
    assert t._labels()['items_read']==3 and t._labels()['model']=='m' and t._labels()['tainted_by_web'] is False
    host.add_labels(lambda tools:1/0)
    assert make_tools(tmp_path,monkeypatch,host)._labels()['plugin_labels']=='unknown'


def test_a_request_notice_names_the_seat_and_count_only_and_is_rate_limited(tmp_path,monkeypatch):
    seen=[];host=P.Host({});host.set_notifier(seen.append)
    monkeypatch.setattr(T,'_ping_state',dict(last=None,count=0))
    for _ in range(3):T._ping(host.notifier,'ag_0123456789abcdef')
    assert seen==[dict(kind='request',actor='ag_0123456789abcdef',count=1)]
    T._ping(host.notifier,'not-an-actor');T._ping(None,'ag_0123456789abcdef')
    assert len(seen)==1


def seat(host,**state):
    s=H.Seat.__new__(H.Seat);s.plugins=host;s.actor='a';s.state=dict(state);s.cfg={};s.clock=lambda:100.
    return s


def test_wake_source_answers_only_its_urgency_and_a_broken_one_is_no_signal():
    host=P.Host({})
    host.add_wake_source(lambda seat,urgent:1/0)
    host.add_wake_source(lambda seat,urgent:dict(type='new_item',prompt='Item 7 arrived (host data).',urgent=False))
    host.add_wake_source(lambda seat,urgent:dict(type='alarm',urgent=True))
    host.add_wake_source(lambda seat,urgent:dict(type='Bad Type',urgent=urgent))
    s=seat(host)
    assert s._plugin_wake(True)=='alarm' and s.state['wake_extra'] is None
    assert s._plugin_wake(False)=='new_item' and s.state['wake_extra']=='Item 7 arrived (host data).'
    assert seat(P.Host({}))._plugin_wake(False) is None


def test_rotation_waits_while_a_plugin_is_busy_and_an_unreadable_check_reads_busy():
    host=P.Host({});s=seat(host)
    assert s._busy() is False
    flag=[True];host.add_busy_check(lambda seat:flag[0])
    assert s._busy() is True
    flag[0]=False;assert s._busy() is False
    host.add_busy_check(lambda seat:1/0);assert s._busy() is True


def test_heartbeat_guard_is_off_without_a_path_and_parks_on_not_ok_unreadable_or_sustained_staleness(tmp_path):
    assert H.HeartbeatGuard().check(1.) is None
    path=tmp_path/'hb.json';guard=H.HeartbeatGuard(path,limit_s=5,samples=3,spacing_s=10)
    assert guard.check(1.)=='guard_heartbeat_unreadable'
    path.write_text(json.dumps(dict(ok=False)));assert guard.check(1.)=='guard_not_ok'
    path.write_text(json.dumps(dict(ok=True)));at=path.stat().st_mtime
    assert guard.check(at+1) is None
    assert [guard.check(at+t) for t in (10,20,25,30)]==[None,None,None,'guard_heartbeat_stale']
    os.utime(path,(at+31,at+31));assert guard.check(at+32) is None and guard.over==[]
    named=H.HeartbeatGuard(path,ok_key='auth_ok');assert named.check(at+32)=='guard_not_ok'


def test_deny_hosts_grow_only_and_a_failing_source_parks_after_the_grace():
    host=P.Host({});hosts=[{'a.example'}]
    def source():
        if hosts[0] is None:raise OSError()
        return hosts[0]
    host.add_deny_hosts(source)
    proxy=types.SimpleNamespace(deny=frozenset({'base.example'}));now=[0.]
    r=MAIN.DenyRefresher(host,[proxy],clock=lambda:now[0])
    r.refresh();assert proxy.deny=={'base.example','a.example'} and r.park_reason() is None
    hosts[0]={'b.example'};r.refresh();assert proxy.deny=={'base.example','a.example','b.example'}
    hosts[0]=None;r.refresh();now[0]=74.;assert r.park_reason() is None and proxy.deny=={'base.example','a.example','b.example'}
    now[0]=75.;assert r.park_reason()=='deny_hosts_unknown'
    hosts[0]=set();r.refresh();assert r.park_reason() is None


def test_plugin_retention_rule_runs_with_the_builtin_ones_and_a_failing_rule_is_reported(tmp_path,monkeypatch):
    office=sqlite3.connect(':memory:',isolation_level=None);E.ensure_schema(office);quota=sqlite3.connect(':memory:')
    monkeypatch.setattr(R,'prune_sessions',lambda *a,**k:True);monkeypatch.setattr(R.Q,'prune',lambda *a,**k:0)
    policy=tmp_path/'p.json';policy.write_text('{"daily_budget":1}')
    cfg=dict(office_db=str(tmp_path/'o.db'),search_policy=str(policy),proxy_log_dir=str(tmp_path),state_dir=str(tmp_path))
    lines=R.run(cfg,office,quota,now=1e9,extra=[lambda cfg,office,now:'project_rows_deleted=4',lambda cfg,office,now:1/0])
    assert 'project_rows_deleted=4' in lines and 'plugin_retention_failed=ZeroDivisionError' in lines and 'notifications_deleted=0' in lines
