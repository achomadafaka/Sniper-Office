from types import SimpleNamespace

from sniper_office import plugins
from sniper_office.markets import common
from sniper_office.services import narrative

PLUGIN = 'sniper_office.approved_tools'


def registry(role, grants, **paths):
    cfg = dict(plugins=[PLUGIN], role=role, approved_tools=grants, **paths)
    return plugins.load(cfg).tools, SimpleNamespace(cfg=cfg, clock=lambda: 1000)


def test_grants_require_named_role_and_cannot_import_workspace_code():
    grants = {'coach': ['debate_index', 'workspace.module']}
    assert not registry('researcher', grants)[0]
    assert set(registry('coach', grants)[0]) == {'debate_index'}
    assert not registry('coach', {})[0]


def test_pm_rules_pages_without_mixing_snapshots(tmp_path):
    path = str(tmp_path / 'feed.db')
    with common.connect(path, common.FEED_SCHEMA) as conn:
        conn.execute("INSERT INTO mk_pm_markets(condition_id,rules,source,fetched_at) VALUES('id',?,'gamma',900)", ('x' * 2400,))
        conn.execute("INSERT INTO mk_pm_markets(condition_id,rules,source,fetched_at) VALUES('old',?,'gamma',850)", ('x' * 1500,))
    tools, ctx = registry('pm_trader', {'pm_trader': ['pm_rules']}, markets_db=path)
    call = tools['pm_rules'].handler
    first = call(ctx, condition_id='id')
    assert len(first['rules']) == 1000 and first['next_offset'] == 1000
    assert call(ctx, condition_id='id', offset=1000, fetched_at=899)['reason'] == 'snapshot_changed'
    assert call(ctx, condition_id='old')['state'] == 'partial'
    tail = call(ctx, condition_id='id', offset=2000, fetched_at=900)
    assert tail['rules'] == 'x' * 400 and tail['next_offset'] is None


def test_debate_index_groups_turns_and_does_not_create_missing_db(tmp_path):
    path = tmp_path / 'ledger.db'
    tools, ctx = registry('coach', {'coach': ['debate_index']}, markets_ledger_db=str(path))
    call = tools['debate_index'].handler
    assert call(ctx)['state'] == 'unknown' and not path.exists()
    with common.connect(path, common.LEDGER_SCHEMA) as conn:
        conn.executemany("INSERT INTO bk_debates(news_id,role,text,at) VALUES(?,'coach','text',?)",
                         [('a', 100), ('a', 300), ('b', 200), ('future', 1100)])
    rows = call(ctx, since_s=750)['items']
    assert [(row['news_id'], row['turns']) for row in rows] == [('a', 1)]


def test_narrative_coverage_excludes_future_and_preserves_unknown(tmp_path):
    path = str(tmp_path / 'narrative.db')
    with narrative.open_db(path) as conn:
        narrative.write_generation(conn, [dict(theme_id='frog', confidence=75, names=['Fresh Frog'])],
                                   now=600, actor='narrative')
        guess = conn.execute('SELECT guess_id FROM guesses').fetchone()[0]
        narrative.label(conn, 'solana', 'm1', 'Fresh Frog', 'FRG', 700, 'provider', 701)
        narrative.label(conn, 'solana', 'm2', None, None, 720, 'provider', 721)
        narrative.label(conn, 'solana', 'm3', 'Different', 'OTHER', 730, 'provider', 731)
        narrative.label(conn, 'solana', 'm4', 'Fresh Frog', 'FRG', 740, 'provider', 1100)
    tools, ctx = registry('narrative', {'narrative': ['narrative_guess_coverage']}, narrative_db=path)
    data = tools['narrative_guess_coverage'].handler(ctx, guess_id=guess)
    assert data['eligible_observed'] == 4
    assert data['counts'] == dict(exact=1, fuzzy=0, theme_only=None, none=1, unknown=2)
    assert data['name_or_symbol_known'] == 3 and data['observation_cutoff'] == 1000
    assert tools['narrative_guess_coverage'].handler(ctx, theme_id='frog', slot_ts=600) == data
