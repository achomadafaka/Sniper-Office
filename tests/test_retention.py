import sqlite3

import pytest

from sniper_office import seat_events as E,seat_retention as R

NOW=2000000000.


def test_events_older_than_14_days_go_and_unknown_time_stays():
    conn=sqlite3.connect(':memory:',isolation_level=None);E.ensure_schema(conn)
    for at,name in ((NOW-15*86400,'old'),(NOW-13*86400,'new'),(None,'unknown')):
        conn.execute("INSERT INTO office_v2_events(event_id,epoch_id,actor_id,generation,incarnation,corrected_at,kind,state) VALUES(?,'e','a',1,1,?,'thought','known')",(name,at))
    assert R.prune_events(conn,now=NOW)==1
    assert {r[0] for r in conn.execute('SELECT event_id FROM office_v2_events')}=={'new','unknown'}


def test_logs_drop_whole_old_days_only(tmp_path):
    for name in ('tool-20200101.jsonl','model-20330518.jsonl','other.jsonl','tool-bad.jsonl'):(tmp_path/name).write_text('x')
    assert R.prune_logs(tmp_path,now=NOW)==1
    assert sorted(p.name for p in tmp_path.iterdir())==['model-20330518.jsonl','other.jsonl','tool-bad.jsonl']


def test_invalid_numeric_log_date_is_left_untouched(tmp_path):
    path=tmp_path/'tool-20261340.jsonl';path.write_text('keep')
    assert R.prune_logs(tmp_path,now=NOW)==0 and path.read_text()=='keep'


@pytest.mark.parametrize('sizes,reason',[((10,10),None),((101,10),'workspace_over_cap'),((10,101),'workspace_over_cap'),((None,10),'workspace_size_unknown')])
def test_cap_parks_when_over_or_unmeasurable(monkeypatch,sizes,reason):
    table=dict(zip(('w','s'),sizes))
    monkeypatch.setattr(R,'workspace_bytes',lambda p:table[p])
    assert R.over_cap(dict(workspace='w',state_dir='s',workspace_cap_bytes=100))==reason


def test_du_is_parsed_and_failure_is_none(monkeypatch):
    monkeypatch.setattr(R,'_as_office',lambda *a,**k:type('D',(),{'returncode':0,'stdout':'123\t/x\n'})())
    assert R.workspace_bytes('/x')==123
    monkeypatch.setattr(R,'_as_office',lambda *a,**k:type('D',(),{'returncode':1,'stdout':''})())
    assert R.workspace_bytes('/x') is None


@pytest.mark.parametrize('failure',['timeout','missing','negative'])
def test_unmeasurable_du_parks_instead_of_raising_or_admitting(monkeypatch,failure):
    import subprocess
    def measure(*a,**k):
        if failure=='timeout':raise subprocess.TimeoutExpired('du',120)
        if failure=='missing':raise OSError('cannot start du')
        return type('D',(),{'returncode':0,'stdout':'-1\t/x\n'})()
    monkeypatch.setattr(R,'_as_office',measure)
    assert R.over_cap(dict(workspace='w',state_dir='s'))=='workspace_size_unknown'


def test_prune_keeps_the_current_session_and_reports_failure(monkeypatch):
    calls=[]
    def fake(*argv,**k):
        calls.append(argv);return type('D',(),{'returncode':0 if argv[0]=='sh' else rc[0],'stdout':'present\n'})()
    rc=[0];monkeypatch.setattr(R,'_as_office',fake)
    assert R.prune_sessions('/s',current='abc') is True and 'abc.jsonl' in calls[-1]
    rc[0]=1;assert R.prune_sessions('/s',current='abc') is False


@pytest.mark.parametrize('rc,out,ok',[(0,'absent\n',True),(1,'',False),(126,'',False),(0,'garbage',False)])
def test_only_a_printed_absent_counts_as_fine(monkeypatch,rc,out,ok):
    monkeypatch.setattr(R,'_as_office',lambda *a,**k:type('D',(),{'returncode':rc,'stdout':out})())
    assert R.prune_sessions('/s') is ok


def test_prune_timeout_is_reported(monkeypatch):
    def boom(*a,**k):raise R.subprocess.TimeoutExpired('x',1)
    monkeypatch.setattr(R,'_as_office',boom)
    assert R.prune_sessions('/s') is False


