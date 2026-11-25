"""db_read, host side. A seat's SELECT runs in a private in-memory database holding copied rows up to the cut, never on a connection that can reach a source. Rows are data.

Which tables a seat may read is a JSON spec the host owns (see config/data_sources.example.json):

    {"sources": {"main": "/abs/path/app.db"},
     "tables": {"tickets": {"source": "main", "time_column": "created_at", "time_kind": "iso", "columns": ["id", "state"]}}}

Named tables and named columns only, so a new column stays out until someone lists it. Every table names its time column:
rows after the cut, and rows whose time is NULL, are never copied. time_kind is "iso" (UTC text) or "epoch" (Unix seconds).
The newest COPY_ROWS rows per table are copied; `coverage` says where a table was cut, so a count is never mistaken for the whole table.
A source that cannot be opened or lacks the table reads `unavailable`, never an empty table.
Runs as a child process with an address-space limit: python3 seat_db.py <spec.json> <sql> <now>. Stdlib only."""
import json
import re
import sqlite3
import time
from datetime import datetime,timezone

MAX_ROWS=200
MAX_OUTPUT=12000   # below the tool's 16 KiB, so a result is never cut mid-JSON
MAX_VALUE=1<<20
MAX_COLUMNS=64   # both the column count and each name's length
COPY_ROWS=20000
CHILD_MEMORY=768*1024**2
CHILD_FILE=64*1024**2
COPY_BYTES=16*1024**2   # per table; bounds host memory
DEADLINE_S=2.   # the seat's statement
COPY_DEADLINE_S=6.   # copying the allowed tables
_NAME=re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,62}\Z')


def load_spec(path):
    """Refuses anything that is not a plain name: table, column and source names go into SQL text."""
    spec=json.loads(open(path,'rb').read())
    sources,tables=spec.get('sources'),spec.get('tables')
    if not isinstance(sources,dict) or not isinstance(tables,dict) or set(spec)-{'sources','tables'}:raise ValueError('spec_invalid')
    if not all(isinstance(k,str) and _NAME.match(k) and isinstance(v,str) and v.startswith('/') and '?' not in v and '#' not in v for k,v in sources.items()):raise ValueError('spec_source')
    for name,t in tables.items():
        if (not isinstance(name,str) or not _NAME.match(name) or not isinstance(t,dict) or set(t)-{'source','source_table','time_column','time_kind','columns'}
                or t.get('source') not in sources or t.get('time_kind') not in ('iso','epoch')
                or not isinstance(t.get('columns'),list) or not t['columns'] or len(t['columns'])>MAX_COLUMNS
                or not all(isinstance(c,str) and _NAME.match(c) for c in t['columns']+[t.get('time_column'),t.get('source_table',name)])):
            raise ValueError('spec_table')
    return spec


def _cut(kind,now):
    return now if kind=='epoch' else datetime.fromtimestamp(now,timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f+00:00')


def _authorizer(action,a,b,db,view):
    # What a seat's statement runs against holds only copied rows, so reads are free. This keeps it to reading: no attach, pragma or write.
    if action in (sqlite3.SQLITE_SELECT,sqlite3.SQLITE_RECURSIVE,sqlite3.SQLITE_READ):return sqlite3.SQLITE_OK
    if action==sqlite3.SQLITE_FUNCTION and b not in ('load_extension','readfile','writefile','fts3_tokenizer'):return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _fill(conn,source,table,t,now):
    """Newest rows by rowid (arrival order, no scan), at or before the cut, named columns only."""
    cols=t['columns'];column=t['time_column']
    cursor=source.execute('SELECT %s FROM "%s" WHERE "%s" IS NOT NULL AND "%s"<=? ORDER BY rowid DESC LIMIT ?'%(
        ','.join('"%s"'%c for c in cols),t.get('source_table',table),column,column),(_cut(t['time_kind'],now),COPY_ROWS+1))
    used=0;n=0;state='complete'
    for row in cursor:
        used+=sum(len(v) for v in row if isinstance(v,(str,bytes)))
        if n==COPY_ROWS or used>COPY_BYTES:
            state='newest_only';break
        conn.execute('INSERT INTO "%s" VALUES(%s)'%(table,','.join('?'*len(cols))),row);n+=1
    return dict(rows=n,state=state,source=t['source'])


def read(spec,sql,*,now):
    """Returns state known with rows, or state unknown with a reason. A statement that hits the deadline is partial, never an empty result."""
    if not isinstance(sql,str) or not sql.strip() or len(sql)>4000 or ';' in sql.rstrip().rstrip(';'):return dict(state='unknown',reason='statement_invalid')
    deadline=time.monotonic()+COPY_DEADLINE_S
    check=lambda:int(time.monotonic()>deadline)
    conn=sqlite3.connect(':memory:');opened={};coverage={}
    try:
        for name,path in spec['sources'].items():
            try:
                opened[name]=sqlite3.connect('file:%s?mode=ro'%path,uri=True,timeout=.5)
                opened[name].set_progress_handler(check,2000)
            except sqlite3.Error:opened[name]=None
        for table,t in spec['tables'].items():
            conn.execute('CREATE TABLE "%s"(%s)'%(table,','.join('"%s"'%c for c in t['columns'])))
            source=opened[t['source']]
            try:
                if source is None:raise sqlite3.OperationalError('source')
                coverage[table]=_fill(conn,source,table,t,now)
            except sqlite3.Error:   # this table only, never every table
                conn.execute('DELETE FROM "%s"'%table)
                coverage[table]=dict(rows=0,state='unavailable',source=t['source'])
        for source in opened.values():
            if source is not None:source.close()
        opened={}
        conn.execute('PRAGMA temp_store=MEMORY')   # the sorter would otherwise spill to temp files, past the address-space limit
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,MAX_VALUE)   # zeroblob and printf cannot allocate past this before the deadline fires
        conn.set_authorizer(_authorizer)
        deadline=time.monotonic()+DEADLINE_S
        conn.set_progress_handler(check,2000)
        cursor=conn.execute(sql)
        names=[d[0] for d in cursor.description or []]
        if len(names)>MAX_COLUMNS or any(len(n)>MAX_COLUMNS for n in names):return dict(state='unknown',reason='response_too_large',rows=[])
        rows,used,reason=[],0,None
        for row in cursor:   # one at a time, so the byte cap also bounds host memory
            used+=len(json.dumps(row,default=str))
            if used>MAX_OUTPUT:reason='output_bytes';break
            if len(rows)==MAX_ROWS:reason='row_limit';break
            rows.append(list(row))
        out=dict(state='partial' if reason else 'known',columns=names,rows=rows,limited=bool(reason),reason=reason,cut=now,coverage=coverage,
                 whole_table=all(v['state']=='complete' for v in coverage.values()))   # an unavailable source is not complete
        # The cap is on the whole answer: column names and escaping count too, and the tool must never cut JSON mid-way.
        while len(json.dumps(out,default=str))>MAX_OUTPUT and out['rows']:
            out['rows'].pop();out.update(state='partial',limited=True,reason='output_bytes')
        if len(json.dumps(out,default=str))>MAX_OUTPUT:return dict(state='unknown',reason='response_too_large',rows=[])
        return out
    except sqlite3.Error as exc:
        # The progress handler's abort arrives as a plain DatabaseError, so the deadline is told apart by time, not class.
        if time.monotonic()>deadline:return dict(state='partial',reason='deadline',rows=[])
        return dict(state='unknown',reason=re.sub(r'\s+',' ',str(exc))[:200] or type(exc).__name__,rows=[])
    finally:
        conn.close()
        for source in opened.values():
            if source is not None:source.close()


def main(argv):
    """One read in a child with an address-space limit: a recursive query's temp structures are bounded by the kernel, not only by the deadline. Memory failure reads unknown."""
    import resource
    resource.setrlimit(resource.RLIMIT_AS,(CHILD_MEMORY,CHILD_MEMORY))
    resource.setrlimit(resource.RLIMIT_FSIZE,(CHILD_FILE,CHILD_FILE))
    try:out=read(load_spec(argv[1]),argv[2],now=float(argv[3]))
    except MemoryError:out=dict(state='unknown',reason='memory_limit',rows=[])
    except (OSError,ValueError):out=dict(state='unknown',reason='spec_invalid',rows=[])
    print(json.dumps(out,default=str))


if __name__=='__main__':
    import sys
    main(sys.argv)
