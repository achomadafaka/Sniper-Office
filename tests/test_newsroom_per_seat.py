"""A quiet seat's mail and activity reach the floor even when a chatty seat floods the newest window; Hermes token fragments don't become the seat's thought."""
import importlib.util
from pathlib import Path

from sniper_office import seat_events as E, views as V

_spec = importlib.util.spec_from_file_location('ui_demo_data', Path(__file__).resolve().parents[1] / 'examples/ui_demo_data.py')
demo = importlib.util.module_from_spec(_spec);_spec.loader.exec_module(demo)
HEAD, RESEARCHER, REVIEWER = (s[0] for s in demo.SEATS[:3])
LAYOUT = dict(rooms=dict(decision=['head'], research=['researcher', 'reviewer']), fallback=None)


def test_head_mail_survives_a_flood_of_newer_mail_and_events(tmp_path):
    office, quota = demo.build(tmp_path)
    import sqlite3
    conn = sqlite3.connect(office, isolation_level=None)
    for i in range(400):        # reviewer floods researcher after Head's direction
        E.seat_dm(conn, from_actor=REVIEWER, from_generation=1, from_incarnation=2, to_actor_id=RESEARCHER, text='flood %d' % i, now=demo.NOW - 100 + i * 0.1)
        E.record_event(conn, epoch_id='first-run', actor_id=REVIEWER, generation=1, incarnation=2, at=demo.NOW - 100 + i * 0.1, kind='tool_call', text='web_search', refs=dict(tool='web_search'))
    doc = V.newsroom(str(office), str(quota), now=demo.NOW, layout=LAYOUT)
    assert any(m['from'] == 'head' and m['to'] == 'researcher' for m in doc['mail'])
    assert any(a['seat'] == 'head' and a['kind'] != 'mail' for a in doc['activity'])
    assert len(doc['mail']) <= V.MAIL


def test_streamed_thought_fragments_are_joined_into_one_line(tmp_path):
    office, quota = demo.build(tmp_path)
    import sqlite3
    conn = sqlite3.connect(office, isolation_level=None)
    for i, t in enumerate(('.', ' arrives', ' mail')):
        E.record_event(conn, epoch_id='first-run', actor_id=HEAD, generation=1, incarnation=2, at=demo.NOW - 50 + i, kind='thought', text=t)
    doc = V.newsroom(str(office), str(quota), now=demo.NOW, layout=LAYOUT)
    assert doc['seats']['head']['last_thought'] == '. arrives mail'


def test_per_seat_keeps_each_seats_newest_then_fills_to_the_cap():
    items = [dict(seat='loud', at=100 - i) for i in range(50)] + [dict(seat='quiet', at=1)]
    out = V._per_seat(items, 3, 10)
    assert len(out) == 10 and out[-1]['seat'] == 'quiet' and [e['seat'] for e in out].count('loud') == 9


def test_one_text_sent_to_several_seats_is_one_mail_row_naming_every_recipient(tmp_path):
    office, quota = demo.build(tmp_path)
    import sqlite3
    conn = sqlite3.connect(office, isolation_level=None)
    for to, at in ((RESEARCHER, demo.NOW - 40), (REVIEWER, demo.NOW - 39)):
        E.seat_dm(conn, from_actor=HEAD, from_generation=1, from_incarnation=2, to_actor_id=to, text='Check the filing. Then report back.', now=at)
    doc = V.newsroom(str(office), str(quota), now=demo.NOW, layout=LAYOUT)
    rows = [m for m in doc['mail'] if m['text'] == 'Check the filing. Then report back.']
    assert len(rows) == 1 and rows[0]['to_all'] == ['reviewer', 'researcher'] and rows[0]['from'] == 'head'


def test_mail_text_splits_into_a_bold_first_sentence_and_a_short_rest():
    import json, re, subprocess
    src = (Path(__file__).resolve().parents[1] / 'floor/static/office-rooms-office.js').read_text()
    fn = re.search(r'const mailParts = .*?;\n', src, re.S).group(0)
    js = fn + 'console.log(JSON.stringify([mailParts("Check the filing. Then " + "x".repeat(300)), mailParts("no stop here"), mailParts("Price 0.5 held. Done.")]))'
    a, b, c = json.loads(subprocess.run(['node', '-e', js], capture_output=True, text=True, check=True).stdout)
    assert a['first'] == 'Check the filing.' and len(a['preview']) <= 200 and a['preview'].endswith('…') and a['rest'].startswith('Then ')
    assert b == {'first': 'no stop here', 'rest': '', 'preview': ''}
    assert c['first'] == 'Price 0.5 held.' and c['rest'] == 'Done.'


def _markets_ledger(path):
    from sniper_office.markets import common as C
    led = C.connect(str(path), C.LEDGER_SCHEMA)
    led.execute("INSERT INTO bk_strategies(strategy_id,lane,owner_role,hypothesis,status,created_at) VALUES('s1','perps','researcher','h','active',?)", (demo.NOW - 60,))
    led.execute("INSERT INTO bk_strategies(strategy_id,lane,owner_role,hypothesis,status,created_at) VALUES('s0','perps','researcher','old','active',?)", (demo.NOW - 3 * 86400,))
    led.close()
    return str(path)


def test_desk_now_gives_each_active_seat_its_target_last_product_evidence_and_today(tmp_path):
    office, quota = demo.build(tmp_path)
    import sqlite3
    conn = sqlite3.connect(office, isolation_level=None)
    E.seat_dm(conn, from_actor=HEAD, from_generation=1, from_incarnation=2, to_actor_id=RESEARCHER, text='TARGET researcher: 1 perps strategy by 18:00 UTC\nWhy: perps has none.', now=demo.NOW - 90)
    E.seat_dm(conn, from_actor=HEAD, from_generation=1, from_incarnation=2, to_actor_id=RESEARCHER, text='Also check the filing.', now=demo.NOW - 30)
    layout = dict(rooms=dict(decision=['head'], research=['researcher'], control=['reviewer']), fallback=None)
    doc = V.newsroom(str(office), str(quota), now=demo.NOW, layout=layout, extra=dict(markets_ledger=_markets_ledger(tmp_path / 'ml.db')))
    dn = doc['desk_now']
    assert dn['as_of'] == demo.NOW and dn['day_start'] == demo.NOW - demo.NOW % 86400 and dn['paused'] == ['reviewer']
    rows = {r['role']: r for r in dn['seats']}
    assert 'reviewer' not in rows and {'head', 'researcher'} <= set(rows)
    r = rows['researcher']
    assert r['assignment']['text'] == 'Also check the filing.' and r['assignment']['target'] == dict(count=1, what='perps strategy', due='18:00 UTC', at=demo.NOW - 90)
    assert r['last_output'] == dict(kind='strategy', at=demo.NOW - 60, text='perps: h')
    assert r['today']['by_kind'] == dict(strategy=1) and r['today']['outputs'] == 1 and r['today']['orders'] is None and r['last_evidence'] is None      # the demo researcher sent no mail or request
    assert rows['head']['today']['mails'] >= 2 and rows['head']['assignment'] is None and rows['head']['last_evidence'] == dict(kind='mail', at=demo.NOW - 30, text='Also check the filing.')


def test_desk_now_says_unknown_not_zero_when_the_markets_ledger_is_missing(tmp_path):
    office, quota = demo.build(tmp_path)
    doc = V.newsroom(str(office), str(quota), now=demo.NOW, layout=LAYOUT)
    r = {x['role']: x for x in doc['desk_now']['seats']}['researcher']
    assert r['last_output'] is None and r['today']['outputs'] is None and r['today']['by_kind'] is None and doc['desk_now']['coverage']['markets_ledger'] == 'unavailable'


def test_a_target_from_an_earlier_utc_day_is_not_carried(tmp_path):
    office, quota = demo.build(tmp_path)
    import sqlite3
    conn = sqlite3.connect(office, isolation_level=None)
    day = demo.NOW - demo.NOW % 86400
    E.seat_dm(conn, from_actor=HEAD, from_generation=1, from_incarnation=2, to_actor_id=RESEARCHER, text='TARGET researcher: 3 briefs by 09:00 UTC', now=day - 60)
    doc = V.newsroom(str(office), str(quota), now=demo.NOW, layout=LAYOUT)
    r = {x['role']: x for x in doc['desk_now']['seats']}['researcher']
    assert r['assignment'] is not None and r['assignment']['target'] is None      # yesterday's TARGET mail is not today's target


def test_a_prose_target_to_one_seat_counts_and_an_idle_seat_says_idle(tmp_path):
    office, quota = demo.build(tmp_path)
    import sqlite3
    conn = sqlite3.connect(office, isolation_level=None)
    E.seat_dm(conn, from_actor=HEAD, from_generation=1, from_incarnation=2, to_actor_id=RESEARCHER,
              text="Today's target is 1 registration-ready strategy for rates by 23:59 UTC, November 26. Mail it to its owner.", now=demo.NOW - 20)
    doc = V.newsroom(str(office), str(quota), now=demo.NOW, layout=LAYOUT)
    rows = {x['role']: x for x in doc['desk_now']['seats']}
    assert rows['researcher']['assignment']['target'] == dict(count=1, what='registration-ready strategy for rates', due='23:59 UTC', at=demo.NOW - 20)
    assert all(r['status'] in ('working', 'sleeping', 'parked', 'idle') for r in rows.values())
