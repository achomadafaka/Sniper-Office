from contextlib import closing
from datetime import datetime, timezone

from sniper_office.markets import api, common


def test_ledger_boundary_keeps_archive_history_out_of_active_ledger(tmp_path):
    active = tmp_path / 'markets_ledger.db'
    folder = tmp_path / 'archive' / 'epoch1-20261126T102820Z'
    folder.mkdir(parents=True)
    archive = folder / active.name
    with closing(common.connect(archive, common.LEDGER_SCHEMA)) as conn:
        conn.execute("INSERT INTO bk_reviews(by_role,at,review) VALUES ('coach', 1, 'archived review')")
        conn.execute("INSERT INTO bk_reviews(by_role,at,review) VALUES ('coach', 1795690000, 'after boundary')")
    with closing(common.connect(active, common.LEDGER_SCHEMA)) as conn:
        conn.execute("INSERT INTO bk_reviews(by_role,at,review) VALUES ('coach', 1, 'current review')")
    boundary = api.ledger_boundary(active)
    assert boundary['state'] == 'known'
    assert boundary['started_at'] == datetime(2026, 11, 26, 10, 28, 20, tzinfo=timezone.utc).timestamp()
    assert boundary['archive_path'] == 'archive/epoch1-20261126T102820Z/markets_ledger.db'
    assert [r['review'] for r in boundary['history']['reviews']] == ['archived review']
    assert set(boundary['history']) == {'calls', 'closes', 'reviews', 'skills', 'explorer'}
    with closing(common.ro(active)) as conn:
        assert [r['review'] for r in api.coach(conn)['reviews']] == ['current review']


def test_ledger_boundary_absence_and_archive_failure_are_unknown(tmp_path):
    active = tmp_path / 'markets_ledger.db'
    assert api.ledger_boundary(active)['state'] == 'unknown'
    folder = tmp_path / 'archive' / 'epoch1-20261126T102820Z'
    folder.mkdir(parents=True)
    (folder / active.name).write_text('not a database')
    boundary = api.ledger_boundary(active)
    assert boundary['state'] == 'unknown'
    assert boundary['archive_path']
    assert boundary['history'] is None


def test_book_seat_prompt_names_archive_as_reference(tmp_path):
    from sniper_office.skills import system_prompt

    prompt = tmp_path / 'prompt.md'
    prompt.write_text('BASE')
    active = tmp_path / 'markets_ledger.db'
    folder = tmp_path / 'archive' / 'epoch1-20261126T102820Z'
    folder.mkdir(parents=True)
    (folder / active.name).touch()
    cfg = dict(prompt_file=str(prompt), markets_ledger_db=str(active))
    for role in ('head', 'coach', 'pm_trader', 'perps_trader', 'stocks_trader', 'copy_desk'):
        rendered = system_prompt(dict(cfg, role=role))
        assert 'run/data/archive/epoch1-20261126T102820Z/' in rendered
        assert 'archived reference' in rendered
    assert system_prompt(dict(cfg, role='newsdesk')) == 'BASE'
