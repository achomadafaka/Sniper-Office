"""Canonical records, atomic appends and independently verifiable chains.

Domain modules validate their JSON fields. Private tables use a separate file opened
with evaluation=True; the supervisor must never open that file.
A trusted external chain head is required to detect a removed tail.
A project adds its own tables and indexes with extend_schema() before open_store().
"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid

PRIVATE_TABLES=frozenset({'grades'})
APPEND_ONLY_TABLES=frozenset({'claims','decisions','org_changes','grades','receipts',
    'decision_bundles','sealed_views','memory_entries','memory_snapshots','trial_registry','evaluation_cases','evaluation_gaps'})
MUTABLE_TABLES=frozenset({'agents','turn_runs','action_requests','messages','tickets','heartbeats','spend','epochs'})
_COLUMNS={
    'memory_entries':{'known_at':"REAL NOT NULL CHECK(json_type(payload_json,'$.known_at') IN ('integer','real'))", 'namespace':'TEXT NOT NULL', 'content':'TEXT NOT NULL', 'decision_id':'TEXT', 'supersedes':'TEXT'},
    'memory_snapshots':{'snapshot_id':'TEXT NOT NULL','epoch_id':'TEXT','cut':"REAL NOT NULL CHECK(json_type(payload_json,'$.cut') IN ('integer','real'))"},
    'receipts':{'cache_key':'TEXT'},
    'claims':{'claim_hash':'TEXT'},
    'decisions':{'candidate_seq':'INTEGER','decision_id':'TEXT','claim_hash':'TEXT REFERENCES claims(claim_hash)'},
    'grades':{'decision_seq':'INTEGER','candidate_seq':'INTEGER','epoch_id':'TEXT','kind':'TEXT'},
    'decision_bundles':{'decision_id':'TEXT'},
    'sealed_views':{'epoch_id':'TEXT'},
    'trial_registry':{'k':'INTEGER'},
    'evaluation_cases':{'epoch_id':'TEXT','case_id':'TEXT'},
    'evaluation_gaps':{'epoch_id':'TEXT','source':'TEXT'},
}
_EXTRA_INDEXES=[]
_NAME=re.compile(r'[a-z][a-z0-9_]{0,62}\Z')


def extend_schema(*,append_only=(),private=(),columns=None,indexes=()):
    """Add a project's own append-only tables, generated columns and index statements. Call before open_store().

    Trusted configuration only: names are checked, but column definitions and index statements are run as SQL.
    A private table lives in the evaluation file. An index statement must start with CREATE [UNIQUE] INDEX IF NOT EXISTS."""
    global APPEND_ONLY_TABLES,PRIVATE_TABLES
    append_only,private,columns,indexes=set(append_only),set(private),dict(columns or {}),list(indexes)
    for name in append_only|private|set(columns):
        if not isinstance(name,str) or not _NAME.match(name):raise ValueError('schema_table_name')
    if (set(columns)-(APPEND_ONLY_TABLES|append_only|private)) or (append_only|private)&MUTABLE_TABLES:raise ValueError('schema_table_unknown')
    for table,spec in columns.items():
        if not isinstance(spec,dict) or any(not isinstance(k,str) or not _NAME.match(k) or not isinstance(v,str) for k,v in spec.items()):raise ValueError('schema_column')
    for statement in indexes:
        if not isinstance(statement,str) or not re.match(r'CREATE (UNIQUE )?INDEX IF NOT EXISTS ',statement) or ';' in statement:raise ValueError('schema_index')
    APPEND_ONLY_TABLES=APPEND_ONLY_TABLES|append_only|private
    PRIVATE_TABLES=PRIVATE_TABLES|private
    for table,spec in columns.items():_COLUMNS[table]={**_COLUMNS.get(table,{}),**spec}
    _EXTRA_INDEXES.extend(s for s in indexes if s not in _EXTRA_INDEXES)


def canonical_json(obj):
    return json.dumps(obj,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')


@contextmanager
def write_transaction(conn):
    """Serialize a write, or join an existing transaction without committing it."""
    nested=conn.in_transaction
    name='office_'+uuid.uuid4().hex
    conn.execute('SAVEPOINT '+name if nested else 'BEGIN IMMEDIATE')
    try:
        yield conn
        if nested:conn.execute('RELEASE SAVEPOINT '+name)
        else:conn.commit()
    except BaseException:
        if nested:
            conn.execute('ROLLBACK TO SAVEPOINT '+name)
            conn.execute('RELEASE SAVEPOINT '+name)
        else:conn.rollback()
        raise


def open_store(path: Path, *, evaluation=False):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(str(path),timeout=1.)
    conn.row_factory=sqlite3.Row
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=ON')
        purpose='evaluation' if evaluation else 'office'
        tables=PRIVATE_TABLES if evaluation else APPEND_ONLY_TABLES-PRIVATE_TABLES
        with write_transaction(conn):
            conn.execute('CREATE TABLE IF NOT EXISTS _store_info(id INTEGER PRIMARY KEY CHECK(id=1),purpose TEXT NOT NULL)')
            prior=conn.execute('SELECT purpose FROM _store_info WHERE id=1').fetchone()
            if prior is not None and prior[0]!=purpose:
                raise ValueError('store_purpose_mismatch')
            conn.execute('INSERT OR IGNORE INTO _store_info VALUES(1,?)',(purpose,))
            for table in sorted(tables):
                columns={key:key+' '+kind+" GENERATED ALWAYS AS (json_extract(payload_json,'$."+key+"')) VIRTUAL"
                         for key,kind in _COLUMNS.get(table,{}).items()}
                extra=''.join(', '+definition for definition in columns.values())
                conn.execute('CREATE TABLE IF NOT EXISTS '+table+'('
                    "seq INTEGER PRIMARY KEY,payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_type(payload_json)='object'),"
                    "prev_hash TEXT NOT NULL UNIQUE CHECK(prev_hash='' OR length(prev_hash)=64),"
                    'row_hash TEXT NOT NULL UNIQUE CHECK(length(row_hash)=64)'+extra+')')
                present={row[1] for row in conn.execute('PRAGMA table_xinfo('+table+')')}
                for key,definition in columns.items():
                    if key not in present:
                        conn.execute('ALTER TABLE '+table+' ADD COLUMN '+definition)
                for operation in ('UPDATE','DELETE'):
                    conn.execute('CREATE TRIGGER IF NOT EXISTS '+table+'_no_'+operation.lower()+' BEFORE '+operation+
                                 " ON "+table+" BEGIN SELECT RAISE(ABORT,'append_only'); END")
                for key in _COLUMNS.get(table,{}):
                    if table=='memory_entries' and key in {'content','namespace'}:continue
                    if (table,key) in {('claims','claim_hash'),('decisions','decision_id'),('memory_snapshots','snapshot_id')}:
                        conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS '+table+'_'+key+'_identity ON '+table+'('+key+')')
                    else:
                        conn.execute('CREATE INDEX IF NOT EXISTS '+table+'_'+key+'_seq ON '+table+'('+key+',seq)')
            if not evaluation:
                from .runtime_observations import ensure_schema
                ensure_schema(conn)
                from .work import ensure_schema as ensure_work_schema
                ensure_work_schema(conn)
                conn.execute('CREATE INDEX IF NOT EXISTS memory_entries_namespace_known_at ON memory_entries(namespace,known_at,seq)')
                fts_exists=conn.execute("SELECT 1 FROM sqlite_master WHERE name='memory_entries_fts'").fetchone() is not None
                conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_entries_fts USING fts5(content,content='memory_entries',content_rowid='seq')")
                conn.execute("CREATE TRIGGER IF NOT EXISTS memory_entries_fts_insert AFTER INSERT ON memory_entries BEGIN INSERT INTO memory_entries_fts(rowid,content) VALUES(new.seq,new.content); END")
                if not fts_exists:
                    conn.execute("INSERT INTO memory_entries_fts(memory_entries_fts) VALUES('rebuild')")
                conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS decision_bundles_decision ON decision_bundles(decision_id)')
                conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS evaluation_cases_identity ON evaluation_cases(epoch_id,case_id)')
                # One use per launcher authority: a replayed nonce cannot append a second receipt.
                conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS receipts_launcher_nonce ON receipts(json_extract(payload_json,'$.launcher_policy_nonce')) WHERE json_extract(payload_json,'$.kind')='launcher_policy_use'")
                for statement in _EXTRA_INDEXES:conn.execute(statement)
                for table in sorted(MUTABLE_TABLES):
                    conn.execute('CREATE TABLE IF NOT EXISTS '+table+"(id TEXT NOT NULL PRIMARY KEY,payload_json TEXT NOT NULL "
                                 "CHECK(json_valid(payload_json) AND json_type(payload_json)='object'))")
                conn.execute("CREATE INDEX IF NOT EXISTS turn_runs_created_at ON turn_runs(json_extract(payload_json,'$.created_at'))")
                conn.execute("CREATE INDEX IF NOT EXISTS turn_runs_deferred_deadline ON turn_runs(json_extract(payload_json,'$.deadline_at'),id) WHERE json_extract(payload_json,'$.state')='deferred'")
            if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise sqlite3.IntegrityError('foreign_key_violation')
        return conn
    except BaseException:
        conn.close()
        raise


def _table(table):
    if table not in APPEND_ONLY_TABLES:raise ValueError('unknown_append_table')
    return table


def append(conn, table: str, row: dict) -> str:
    table=_table(table)
    if not conn.execute('PRAGMA foreign_keys').fetchone()[0]:raise ValueError('foreign_keys_required')
    if not isinstance(row,dict):raise TypeError('record_must_be_object')
    if {'seq','row_hash','prev_hash'}&row.keys():raise ValueError('reserved_record_fields')
    payload=canonical_json(row)
    with write_transaction(conn):
        prior=conn.execute('SELECT row_hash FROM '+table+' ORDER BY seq DESC LIMIT 1').fetchone()
        prev='' if prior is None else prior[0]
        digest=hashlib.sha256(payload+prev.encode('ascii')).hexdigest()
        conn.execute('INSERT INTO '+table+'(payload_json,prev_hash,row_hash) VALUES(?,?,?)',
                     (payload.decode('utf-8'),prev,digest))
    return digest


def verify_chain(conn, table: str) -> tuple[bool,int|None]:
    table=_table(table);previous='';expected_seq=1
    for seq,payload,prev,digest in conn.execute('SELECT seq,payload_json,prev_hash,row_hash FROM '+table+' ORDER BY seq'):
        try:
            if not isinstance(payload,str):return False,seq
            row=json.loads(payload);canonical=canonical_json(row)
            expected=hashlib.sha256(canonical+previous.encode('ascii')).hexdigest()
            if (not isinstance(row,dict) or canonical.decode('utf-8')!=payload or seq!=expected_seq
                    or prev!=previous or digest!=expected):return False,seq
        except (ValueError,TypeError,UnicodeError):return False,seq
        previous=digest;expected_seq+=1
    return True,None


def latest(conn, table: str, key_col: str, key):
    """Privileged, indexed latest-record lookup; never expose raw stores to agents."""
    table=_table(table)
    if key_col not in {'row_hash'}|_COLUMNS.get(table,{}).keys():raise ValueError('unindexed_lookup_key')
    row=conn.execute('SELECT seq,payload_json,prev_hash,row_hash FROM '+table+' WHERE '+key_col+'=? ORDER BY seq DESC LIMIT 1',(key,)).fetchone()
    return None if row is None else dict(zip(('seq','payload_json','prev_hash','row_hash'),row))
