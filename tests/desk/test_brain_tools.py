"""brain_read / brain_note kinds, topics and filters; no-match is not an outage; lesson facts for coin and lane closes; a handover leaves no fact."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_desk_plugin import NOW, call, seat  # noqa: E402
from test_wakes import FakeSeat, POL, book  # noqa: E402

from sniper_office.desk import brain as BR  # noqa: E402
from sniper_office.markets import common as C  # noqa: E402

EP = 'episode 5392ba90-ec5e-4b72-9915-5f896cb59616 (1 part(s))'
EPID = '5392ba90-ec5e-4b72-9915-5f896cb59616'


def fake(tmp, **replies):
    """A brain CLI that logs argv and answers each op with (rc, stdout) from `replies`."""
    pkg = tmp / 'pkg' / 'brain'
    pkg.mkdir(parents=True)
    log = tmp / 'calls.jsonl'
    (pkg / '__init__.py').write_text('')
    table = dict(episode=(0, EP), write=(0, 'written 1'), ask=(0, ''), entity=(0, ''), boot=(0, 'b'))
    table.update(replies)
    (pkg / '__main__.py').write_text('import json,sys\nopen(%r,"a").write(json.dumps(sys.argv[1:])+"\\n")\nrc,out=json.loads(%r)[sys.argv[1]]\nprint(out)\nsys.exit(rc)\n' % (str(log), json.dumps(table)))
    return dict(brain_dir=str(tmp), brain_package=str(tmp / 'pkg'), brain_python=sys.executable, role='judge'), log


def calls(log):
    return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


def tools(tmp_path, monkeypatch, **replies):
    cfg, log = fake(tmp_path, **replies)
    return seat(tmp_path, monkeypatch, cfg=cfg), log


def test_a_read_with_no_match_is_known_and_empty_not_an_outage(tmp_path, monkeypatch):
    t, _ = tools(tmp_path, monkeypatch)
    err, doc = call(t, 'brain_read', query='nothing like this')
    assert not err and doc['state'] == 'known' and doc['facts'] == 0 and 'broaden' in doc['hint']


def test_a_dead_brain_is_still_unavailable(tmp_path, monkeypatch):
    t, _ = tools(tmp_path, monkeypatch, ask=(4, ''))
    assert call(t, 'brain_read', query='q')[1]['reason'] == 'brain_unavailable'


def test_read_filters_by_kind_client_side(tmp_path, monkeypatch):
    out = '[unverified] 11111111 observation: a\n[verified] 22222222 rule: b'
    t, _ = tools(tmp_path, monkeypatch, ask=(0, out))
    err, text = call(t, 'brain_read', query='q', kind='rule')
    assert not err and '22222222' in text and '11111111' not in text and t.plugin_state['brain_fact_ids'] == ['22222222']
    err, doc = call(t, 'brain_read', query='q', kind='trap')
    assert doc['state'] == 'known' and doc['facts'] == 0


def test_read_by_topic_lists_the_entity_facts(tmp_path, monkeypatch):
    t, log = tools(tmp_path, monkeypatch, entity=(0, 'lane perps aliases=[]\n[unverified] 33333333 finding: c'))
    err, text = call(t, 'brain_read', topic='lane:perps')
    assert not err and '33333333' in text and calls(log) == [['entity', '--', 'lane', 'perps']]
    assert call(t, 'brain_read', topic='bad topic')[1]['reason'] == 'topic_invalid'
    assert call(t, 'brain_read', query='q', kind='nonsense')[1]['reason'] == 'kind_invalid'
    assert call(t, 'brain_read')[1]['reason'] == 'text_invalid'


def test_an_unknown_topic_entity_is_known_and_empty(tmp_path, monkeypatch):
    t, _ = tools(tmp_path, monkeypatch, entity=(2, 'no such entity'))
    err, doc = call(t, 'brain_read', topic='lane:nope')
    assert not err and doc['state'] == 'known' and doc['facts'] == 0


def test_a_plain_note_is_still_an_observation_on_the_actor(tmp_path, monkeypatch):
    t, log = tools(tmp_path, monkeypatch)
    assert not call(t, 'brain_note', text='saw X')[0]
    w = calls(log)[1]
    assert w[:9] == ['write', '--role', 'judge', '--kind', 'observation', '--entity', 'actor:judge', '--episode', EPID] and '--evidence' not in w


def test_a_lesson_note_is_a_finding_with_topics_and_episode_evidence(tmp_path, monkeypatch):
    t, log = tools(tmp_path, monkeypatch)
    text = 'Next time: perps longs entered after a 3% hourly run-up lost; wait for the pullback.'
    err, _ = call(t, 'brain_note', text=text, kind='lesson', topics=['lane:perps', 'coin:BTC'])
    w = calls(log)[1]
    assert not err and w[:6] == ['write', '--role', 'judge', '--kind', 'finding', '--entity'] and w.count('--entity') == 3
    assert 'lane:perps' in w and 'coin:BTC' in w and 'actor:judge' in w and 'episode:' + EPID in w and w[-1] == text and '--section' in w


def test_note_validation_refuses_before_the_brain_is_called(tmp_path, monkeypatch):
    t, log = tools(tmp_path, monkeypatch)
    long = 'x' * 50
    for args, reason in [(dict(text=long, kind='lesson'), 'topic_required'), (dict(text='short', kind='rule', topics=['lane:perps']), 'text_too_short'),
                         (dict(text=long, kind='gossip'), 'kind_invalid'), (dict(text=long, topics=['Lane:perps']), 'topic_invalid'),
                         (dict(text=long, topics=['lane:perps'] * 7), 'topics_invalid'), (dict(text='x' * 601, kind='finding'), 'text_too_long'),
                         (dict(text=long, topics='lane:perps'), 'topics_invalid')]:
        assert call(t, 'brain_note', **args)[1]['reason'] == reason, args
    assert calls(log) == []


def test_a_handover_leaves_an_episode_and_no_fact(tmp_path):
    cfg, log = fake(tmp_path)
    BR.on_handover(FakeSeat(tmp_path, **cfg), 'Working on mint X.')
    assert [c[0] for c in calls(log)] == ['episode']


def test_a_coin_close_lesson_is_also_a_finding_fact(tmp_path):
    cfg, log = fake(tmp_path)
    ledger = book(tmp_path, [('c1', 'ep', 'ep', 'MINTX', 'p', 'closed', NOW - 950, NOW - 900, NOW - 100, 25., 27.5, 'trail', 100., 0.25, 30., POL, json.dumps(dict(price_usd=0.31)))])
    BR.lessons(FakeSeat(tmp_path, ledger_db=ledger, **cfg))
    ep, w = calls(log)
    assert ep[0] == 'episode' and w[:6] == ['write', '--role', 'judge', '--kind', 'finding', '--entity']
    assert 'lane:coin' in w and 'mint:MINTX' in w and 'episode:' + EPID in w and 'MINTX' in w[-1] and len(w[-1]) <= 600


def test_a_lane_book_close_leaves_an_episode_and_a_finding_once(tmp_path):
    cfg, log = fake(tmp_path)
    feed, led = tmp_path / 'feed.db', C.connect(tmp_path / 'ledger.db', C.LEDGER_SCHEMA)
    C.connect(feed, C.FEED_SCHEMA).close()
    led.execute("INSERT INTO bk_positions(pos_id,book_id,lane,instrument,label,side,qty,entry_price,entry_at,leverage,margin,notional,thesis,disproof,exit_plan,strategy_id,status,exit_price,exit_at,exit_reason,realized,meta_json) "
                "VALUES('p1','perps-judge','perps','hyperliquid:BTC','BTC long','long',1,100,?,5,100,500,'th','dis','plan','s_breakout','closed',89,?,'stop_loss',-11.5,'{}')", (NOW - 900, NOW - 100))
    led.close()
    s = FakeSeat(tmp_path, book_lanes=['perps'], markets_db=str(feed), markets_ledger_db=str(tmp_path / 'ledger.db'), **cfg)
    BR.lessons(s)
    BR.lessons(s)
    ep, w = calls(log)
    assert ep[:6] == ['episode', '--role', 'judge', '--kind', 'report', '--'] and 'stop_loss' in ep[6] and '-11.5' in ep[6] and 'dis' in ep[6]
    assert w[:6] == ['write', '--role', 'judge', '--kind', 'finding', '--entity'] and {'lane:perps', 'coin:BTC', 'strategy:s_breakout', 'actor:judge'} <= set(w)


def test_a_lane_close_without_ledgers_or_brain_is_skipped(tmp_path):
    BR.lessons(FakeSeat(tmp_path, book_lanes=['perps']))   # no brain keys: nothing, no raise
