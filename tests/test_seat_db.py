import json
import sqlite3
import subprocess
import sys

import pytest

from sniper_office import seat_db as D


@pytest.fixture
def spec(tmp_path):
    path=tmp_path/'app.db';c=sqlite3.connect(path)
    c.execute('CREATE TABLE tickets(id INTEGER,state TEXT,created_at TEXT,secret TEXT)')
    c.execute('CREATE TABLE event_log(at REAL,kind TEXT,detail TEXT)')
    c.execute('CREATE TABLE credentials(token TEXT)');c.execute("INSERT INTO credentials VALUES('s3cret')")
    c.executemany('INSERT INTO tickets VALUES(?,?,?,?)',[(1,'open','2026-01-01T00:00:00.000000+00:00','k1'),(2,'future','2099-01-01T00:00:00.000000+00:00','k2'),(3,'undated',None,'k3')])
    c.executemany('INSERT INTO event_log VALUES(?,?,?)',[(10.,'a','x'),(5000.,'late','y')]);c.commit();c.close()
    doc=dict(sources=dict(main=str(path),gone=str(tmp_path/'missing.db')),tables=dict(
        tickets=dict(source='main',time_column='created_at',time_kind='iso',columns=['id','state','created_at']),
        events=dict(source='main',source_table='event_log',time_column='at',time_kind='epoch',columns=['at','kind']),
        absent=dict(source='gone',time_column='at',time_kind='epoch',columns=['at'])))
    file=tmp_path/'spec.json';file.write_text(json.dumps(doc))
    return file

NOW=1_800_000_000.   # 2027


def test_rows_after_the_cut_and_undated_rows_are_never_copied(spec):
    out=D.read(D.load_spec(spec),'SELECT id,state FROM tickets ORDER BY id',now=NOW)
    assert out['state']=='known' and out['rows']==[[1,'open']]
    assert D.read(D.load_spec(spec),'SELECT kind FROM events',now=100.)['rows']==[['a']]


def test_unlisted_columns_and_tables_do_not_exist_for_the_seat(spec):
    s=D.load_spec(spec)
    for sql in ('SELECT secret FROM tickets','SELECT * FROM credentials','SELECT detail FROM events','SELECT * FROM event_log'):
        out=D.read(s,sql,now=NOW);assert out['state']=='unknown' and not out['rows'],sql
    assert 'k1' not in json.dumps(D.read(s,'SELECT * FROM tickets',now=NOW))


@pytest.mark.parametrize('sql',["ATTACH DATABASE '/etc/passwd' AS x","PRAGMA database_list","DELETE FROM tickets","CREATE TABLE t(a)",
                                "SELECT 1; SELECT 2","SELECT load_extension('x')","INSERT INTO tickets VALUES(9,'x','y')",""])
def test_anything_but_one_select_is_refused(spec,sql):
    assert D.read(D.load_spec(spec),sql,now=NOW)['state']=='unknown'


def test_missing_source_reads_unavailable_not_empty_and_marks_the_answer(spec):
    out=D.read(D.load_spec(spec),'SELECT count(*) FROM absent',now=NOW)
    assert out['coverage']['absent']['state']=='unavailable' and out['whole_table'] is False


def test_runaway_statement_hits_the_deadline_as_partial(spec,monkeypatch):
    monkeypatch.setattr(D,'DEADLINE_S',.2)
    out=D.read(D.load_spec(spec),'WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM r) SELECT count(*) FROM r',now=NOW)
    assert out==dict(state='partial',reason='deadline',rows=[])


def test_row_cap_is_reported_and_copy_cap_says_newest_only(spec,tmp_path,monkeypatch):
    c=sqlite3.connect(json.loads(spec.read_text())['sources']['main'])
    c.executemany('INSERT INTO event_log VALUES(?,?,?)',[(float(i),'k','d') for i in range(300)]);c.commit();c.close()
    out=D.read(D.load_spec(spec),'SELECT at FROM events',now=1000.)
    assert out['state']=='partial' and out['reason'] in ('row_limit','output_bytes') and len(out['rows'])<=D.MAX_ROWS
    monkeypatch.setattr(D,'COPY_ROWS',50)
    out=D.read(D.load_spec(spec),'SELECT count(*),min(at) FROM events',now=1000.)
    assert out['coverage']['events']['state']=='newest_only' and out['rows']==[[50,250.]] and out['whole_table'] is False


@pytest.mark.parametrize('change',[lambda d:d['tables']['tickets'].update(columns=['id; DROP TABLE x']),lambda d:d['tables']['tickets'].update(source='nope'),
    lambda d:d['tables']['tickets'].update(time_kind='guess'),lambda d:d['sources'].update(main='relative.db'),lambda d:d['tables'].update({'bad name':d['tables']['tickets']}),
    lambda d:d['tables']['tickets'].update(where='1=1'),lambda d:d['sources'].update(main='/x.db?mode=rw')])
def test_spec_names_that_are_not_plain_names_are_refused(spec,change):
    doc=json.loads(spec.read_text());change(doc);spec.write_text(json.dumps(doc))
    with pytest.raises(ValueError):D.load_spec(spec)


def test_child_process_prints_one_json_answer_and_a_bad_spec_reads_unknown(spec,tmp_path):
    run=lambda s:json.loads(subprocess.run([sys.executable,D.__file__,str(s),'SELECT id FROM tickets',repr(NOW)],capture_output=True,text=True,timeout=30,env={},cwd='/').stdout)
    assert run(spec)['rows']==[[1]]
    assert run(tmp_path/'nope.json')==dict(state='unknown',reason='spec_invalid',rows=[])
