import hashlib
import json

import pytest

from sniper_office import capabilities as C, config, store


def test_pin_mismatch_and_unknown_key_are_refused_before_anything_is_applied(tmp_path):
    before = dict(C.CAPS)
    path = tmp_path / 'office.json'
    path.write_text(json.dumps({'roles': {'intruder': ['book.submit']}}))
    with pytest.raises(config.ConfigMismatch):
        config.load(path, '0' * 64)
    with pytest.raises(ValueError):
        config.apply({'rolez': {'intruder': ['book.submit']}})
    assert C.CAPS == before


def test_pinned_load_applies_and_an_unlisted_role_gets_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(C, 'CAPS', {})
    monkeypatch.setattr(C, 'OFFCHAIN_TOOLS', frozenset())
    path = tmp_path / 'office.json'
    path.write_text(json.dumps({'roles': {'clerk': ['file.read']}, 'cut_free_tools': ['file.read']}))
    config.load(path, hashlib.sha256(path.read_bytes()).hexdigest())
    assert C.CAPS == {'clerk': frozenset({'file.read'})} and C.OFFCHAIN_TOOLS == frozenset({'file.read'})
    assert 'file.read' not in C.CAPS.get('visitor', frozenset())


@pytest.mark.parametrize('bad', [
    dict(append_only=['Bad Name']), dict(append_only=['agents']), dict(columns={'missing_table': {'a': 'TEXT'}}),
    dict(indexes=['DROP TABLE receipts']), dict(indexes=['CREATE INDEX IF NOT EXISTS x ON receipts(seq); DROP TABLE receipts'])])
def test_extend_schema_refuses_bad_names_and_non_index_statements(bad, monkeypatch):
    monkeypatch.setattr(store, 'APPEND_ONLY_TABLES', store.APPEND_ONLY_TABLES)
    monkeypatch.setattr(store, '_EXTRA_INDEXES', list(store._EXTRA_INDEXES))
    with pytest.raises(ValueError):
        store.extend_schema(**bad)


def test_extended_table_is_append_only_chained_and_added_to_an_existing_file(tmp_path, monkeypatch):
    for name in ('APPEND_ONLY_TABLES', 'PRIVATE_TABLES'):
        monkeypatch.setattr(store, name, getattr(store, name))
    monkeypatch.setattr(store, '_COLUMNS', dict(store._COLUMNS))
    monkeypatch.setattr(store, '_EXTRA_INDEXES', list(store._EXTRA_INDEXES))
    path = tmp_path / 'office.db'
    store.open_store(path).close()                                   # created before the project table existed
    store.extend_schema(append_only=['site_visits'], columns={'site_visits': {'site': 'TEXT'}},
                        indexes=['CREATE UNIQUE INDEX IF NOT EXISTS site_visits_site ON site_visits(site)'])
    conn = store.open_store(path)
    store.append(conn, 'site_visits', {'site': 'a'})
    with pytest.raises(Exception):
        store.append(conn, 'site_visits', {'site': 'a'})
    with pytest.raises(Exception):
        conn.execute('DELETE FROM site_visits')
    assert store.verify_chain(conn, 'site_visits') == (True, None)
    conn.close()
