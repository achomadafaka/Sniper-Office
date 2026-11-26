import json
import sqlite3

from floor import routes


def test_tool_request_queue_joins_explicit_dispositions(tmp_path, monkeypatch):
    database = tmp_path / 'office.db'
    with sqlite3.connect(database) as conn:
        conn.executescript("""
            CREATE TABLE office_v2_roster (actor_id TEXT, role TEXT);
            INSERT INTO office_v2_roster VALUES ('a', 'researcher');
            CREATE TABLE office_v2_events (
                seq INTEGER PRIMARY KEY, event_id TEXT, actor_id TEXT,
                corrected_at REAL, kind TEXT, text TEXT, refs_json TEXT
            );
            INSERT INTO office_v2_events VALUES
                (1, 'one', 'a', 10, 'request', 'Research read', '{"kind":"tool","why":"Repeated task"}'),
                (2, 'two', 'a', 11, 'request', 'Another read', '{"kind":"tool"}'),
                (3, 'three', 'a', 12, 'request', 'New request', '{"kind":"capability"}'),
                (4, 'four', 'a', 13, 'thought', 'Not a request', '{}'),
                (5, 'five', 'a', 14, 'request', 'Rejected request', '{"kind":"tool"}');
        """)
    manifest = tmp_path / 'dispositions.json'
    manifest.write_text(json.dumps({
        'one': {'state': 'shipped', 'reason': 'Approved source', 'tool_name': 'research_read'},
        'two': {'state': 'building', 'reason': 'Under review', 'tool_name': None},
        'five': {'state': 'refused', 'reason': 'Already available', 'tool_name': None},
    }))
    monkeypatch.setattr(routes, 'TOOL_REQUEST_DISPOSITIONS', manifest)
    gets, posts = routes.routes({'office_db': database}, routes.databases({'office_db': database}))
    path = '/api/office/tool-requests'
    assert path not in posts
    doc = gets[path]({}, 20)
    assert doc['state'] == 'known' and not doc['limited']
    rows = {row['id']: row for row in doc['items']}
    assert list(rows) == ['five', 'three', 'two', 'one']
    assert [rows[key]['state'] for key in rows] == ['refused', 'new', 'building', 'shipped']
    assert rows['one']['tool_name'] == 'research_read'
    assert rows['one']['why'] == 'Repeated task'
    assert rows['five']['reason'] == 'Already available'
    manifest.unlink()
    doc = gets[path]({}, 20)
    assert doc['dispositions'] == 'unavailable'
    assert len(doc['items']) == 4 and all(row['state'] == 'new' for row in doc['items'])


def test_tool_request_queue_marks_bad_sources_unknown(tmp_path, monkeypatch):
    manifest = tmp_path / 'dispositions.json'
    monkeypatch.setattr(routes, 'TOOL_REQUEST_DISPOSITIONS', manifest)
    db = tmp_path / 'missing.db'
    assert routes.tool_requests(db, now=20)['reason'] == 'requests_unavailable'
    manifest.write_text('{"a":{"state":"shipped","reason":"","tool_name":null}}')
    assert routes.tool_requests(db, now=20)['reason'] == 'dispositions_invalid:a'
    manifest.write_text('{}')
    assert routes.tool_requests(db, now=20)['reason'] == 'requests_unavailable'
    assert not db.exists()
