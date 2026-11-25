"""Router classification, seat tools, wake sources and the floor read API."""
import json
import sqlite3
import sys
import types

import pytest

from conftest import COND, NO, NOW, YES, put_market, put_perp, put_quote, put_stock
from sniper_office.markets import api, common as C, router as R, tools as T, wakes as WK
from sniper_office.plugins import Host


def rooms(title, source='x', summary='', markets=()):
    return R.classify(dict(source=source, title=title, summary=summary), markets)


@pytest.mark.parametrize('title,source,room', [
    ('Nvidia beats earnings estimates, raises guidance; shares jump', 'cnbc_top', 'stocks'),
    ('Bitcoin ETF sees record inflows', 'coindesk', 'crypto'),
    ('Fed signals rate cut as inflation cools', 'bbc_world', 'perps'),
    ('Senate votes to confirm new Supreme Court nominee', 'npr_news', 'polymarket'),
    ('Celtics beat Heat in NBA playoff opener', 'espn_top', 'sports'),
    ('Viral hippo escapes zoo and the internet renames it', 'bbc_world', 'memes'),
])
def test_router_rooms(title, source, room):
    r = rooms(title, source)
    assert room in r['rooms'] and 0 < r['rooms'][room]['score'] <= 100 and r['rooms'][room]['reason']


def test_router_other_and_threshold_and_meme_fields():
    r = rooms('Local council debates parking')
    assert list(r['rooms']) == ['other'] and r['meme'] is None
    m = rooms('Viral hippo escapes zoo and the internet renames it')['meme']
    assert m['name'] and m['ticker'] and m['ticker'].isalpha() and len(m['ticker']) <= 8 and m['markers']
    assert R.meme_proposal('Quarterly results unchanged') is None


def test_router_market_overlap_and_places():
    docs = [('c%d' % i, 'Will team%d win the title%d?' % (i, i)) for i in range(10)] + [('cf', 'Will Iran close the Strait of Hormuz before July?')]
    r = rooms('Iran threatens to close the Strait of Hormuz', markets=docs)
    assert r['markets'] == ['cf'] and r['rooms']['polymarket']['score'] >= 50
    assert {p[0] for p in r['places']} == {'IR'}
    assert rooms('Nothing common here at all', markets=docs)['markets'] == []


def hook_ok(item, result):
    result['rooms'] = {'crypto': dict(score=99.0, reason='hook says so')}
    return result


def test_router_hook_overrides_and_errors_are_recorded_not_raised():
    mod = types.ModuleType('hookmod'); mod.ok = hook_ok; mod.bad = lambda i, r: 1 / 0; mod.junk = lambda i, r: dict(rooms={'nowhere': {}})
    sys.modules['hookmod'] = mod
    assert rooms.__call__('x') and R.classify(dict(title='Local council debates parking'), hook='hookmod:ok')['rooms'] == {'crypto': dict(score=99.0, reason='hook says so')}
    bad = R.classify(dict(title='Bitcoin up'), hook='hookmod:bad')
    assert 'crypto' in bad['rooms'] and bad['hook_error'].startswith('ZeroDivisionError')
    assert 'crypto' in R.classify(dict(title='Bitcoin up'), hook='hookmod:junk')['rooms']             # an unknown room name from a hook is ignored
    assert R.classify(dict(title='Bitcoin up'), hook='no.such:fn')['hook_error']


class FakeTools:
    def __init__(self, tmp, role='pm_trader', **cfg):
        self.db_path, self.actor, self.epoch, self.incarnation = str(tmp / 'office.db'), 'ag_0123456789abcdef', 'ep', 1
        o = sqlite3.connect(self.db_path)
        o.execute('CREATE TABLE IF NOT EXISTS office_v2_roster(actor_id,epoch_id,role,sealed,incarnation)')
        o.execute('DELETE FROM office_v2_roster'); o.execute("INSERT INTO office_v2_roster VALUES(?,?,?,?,?)", (self.actor, 'ep', role, 0, 1)); o.commit(); o.close()
        self.cfg = dict(role=role, markets_db=str(tmp / 'feed.db'), markets_ledger_db=str(tmp / 'ledger.db'), **cfg)
        self.now = NOW
        self.clock = lambda: self.now


def call(name, tools, **kw):
    h = {t[1]: t[5] for t in T.TOOLS}[name]
    r = h(tools, **kw)
    return json.loads(r['content'][0]['text']), r['isError']


PLAN = dict(thesis='the data says so', disproof='the data reverses', exit_plan='sell at target or stop')


def test_tool_groups_and_registration():
    h = Host({})
    import sniper_office.markets as M
    M.register(h)
    groups = {}
    for name, t in h.tools.items():
        groups.setdefault(t.group, []).append(name)
    assert set(groups) == {'polymarket', 'perps', 'stocks', 'news'}
    for need in ('pm_order', 'pm_exit', 'pm_book', 'pm_markets', 'pm_market', 'perp_order', 'perp_book', 'perp_markets', 'stock_order', 'stock_quotes', 'story_list', 'story_route', 'meme_list'):
        assert need in h.tools
    for name in ('pm_order', 'perp_order', 'stock_order'):
        assert {'thesis', 'disproof', 'exit_plan'} <= set(h.tools[name].required) and any(k in h.tools[name].required for k in ('tp_price', 'tp_pct'))
    assert not any(n in ('shell', 'browse') for n in h.tools) and len(h.wake_sources) == 1


def test_order_flow_through_tools_pm(dbs):
    feed, led, tmp = dbs
    put_market(feed); put_quote(feed, YES, 0.48, 0.50)
    t = FakeTools(tmp)
    d, err = call('pm_markets', t, query='fed')
    assert not err and d['markets'][0]['condition_id'] == COND and d['markets'][0]['yes_ask'] == 0.5
    d, err = call('pm_market', t, condition_id=COND)
    assert d['tokens']['YES']['age_s'] == 0 and d['fee']['rate'] == 0.05 and d['question']
    d, err = call('pm_order', t, condition_id=COND, side='YES', usd=100, tp_price=0.7, sl_price=0.3, thesis='', disproof='x', exit_plan='y')
    assert err and d['reason'] == 'plan_missing'
    d, err = call('pm_order', t, condition_id=COND, side='YES', usd=100, tp_price=0.7, sl_price=0.3, **PLAN)
    assert not err and d['state'] == 'filled' and d['book_id'] == 'pm-pm_trader'
    book, _ = call('pm_book', t)
    assert book['open'][0]['live']['unrealized'] < 0 and book['state'] == 'known' and book['open'][0]['thesis'] == PLAN['thesis']
    d, err = call('pm_exit', t, pos_id=d['pos_id'], reason='test')
    assert d['state'] == 'closed'
    assert call('pm_exit', t, pos_id='nope', reason='x')[1]


def test_tools_refuse_a_seat_not_in_the_roster_and_unconfigured(dbs):
    feed, led, tmp = dbs
    t = FakeTools(tmp)
    t.incarnation = 9
    assert call('pm_book', t) == ({'state': 'refused', 'reason': 'seat_not_in_epoch'}, True)
    t = FakeTools(tmp)
    del t.cfg['markets_db']
    d, err = call('pm_book', t)
    assert err and d['reason'] == 'markets_config_unknown'


def test_perp_and_stock_tools(dbs):
    feed, led, tmp = dbs
    put_perp(feed); put_stock(feed)
    t = FakeTools(tmp, role='perps_trader')
    d, _ = call('perp_markets', t)
    assert d['markets'][0]['coin'] == 'BTC' and d['markets'][0]['funding_apr_pct'] == pytest.approx(0.0001 * 8760 * 100, rel=1e-3)
    d, err = call('perp_order', t, coin='BTC', side='short', margin_usd=100, leverage=3, tp_pct=4, sl_pct=2, **PLAN)
    assert d['state'] == 'filled' and call('perp_book', t)[0]['open'][0]['liq_price'] > 100
    d, _ = call('perp_market', t, coin='BTC')
    assert d['state'] == 'known' and call('perp_market', t, coin='NOPE')[1]
    d, _ = call('stock_quotes', t)
    assert d['quotes'][0]['symbol'] == 'NVDA' and d['quotes'][0]['session_open'] is True
    d, err = call('stock_order', t, symbol='NVDA', side='long', margin_usd=50, leverage=2, tp_pct=3, sl_pct=1, **PLAN)
    assert d['state'] == 'filled' and call('stock_book', t)[0]['lane'] == 'stocks'
    assert call('perp_strategies', t)[0]['strategies'] and call('stock_strategy_register', t, hypothesis='a', entry_rule='b', exit_rule='c', kill_criterion='d')[0]['state'] == 'registered'


def seed_stories(feed):
    for i, (title, room, score) in enumerate([('Nvidia earnings beat', 'stocks', 80.), ('Fed cuts rates', 'perps', 70.), ('Hippo escapes', 'memes', 60.), ('Minor stock note', 'stocks', 40.)]):
        feed.execute('INSERT INTO mk_news VALUES(?,?,?,?,?,?,?,?,?)', ('n%d' % i, 'cnbc_top', 'https://ex.example/%d' % i, title, '', None, NOW - 60 * i, title.lower(), None))
        feed.execute('INSERT INTO mk_routes(news_id,room,score,reason,classifier,meme_name,meme_ticker,routed_at) VALUES(?,?,?,?,?,?,?,?)', ('n%d' % i, room, score, 'kw', 'rules', 'Hippo' if room == 'memes' else None, 'HIPPO' if room == 'memes' else None, NOW))
    feed.execute("INSERT INTO mk_story_places VALUES('n1','US','United States',39,-98,'rules',?)", (NOW,))


def test_story_tools(dbs):
    feed, led, tmp = dbs
    seed_stories(feed)
    t = FakeTools(tmp, role='newsdesk')
    d, _ = call('story_list', t, room='stocks')
    assert [s['news_id'] for s in d['stories']] == ['n0', 'n3'] and d['stories'][0]['published'] is None and d['stories'][0]['fetched_at'] == NOW
    assert [s['news_id'] for s in call('story_list', t, room='stocks', min_score=50)[0]['stories']] == ['n0']
    assert call('story_list', t, room='bogus')[1]
    assert call('meme_list', t)[0]['candidates'][0]['meme'] == dict(name='Hippo', ticker='HIPPO')
    assert call('story_route', t, news_id='n3', room='crypto', note='mentions a token')[0]['classifier'] == 'seat:newsdesk'
    assert [s['news_id'] for s in call('story_list', t, room='crypto')[0]['stories']] == ['n3']
    assert call('story_route', t, news_id='n3', room='crypto', note='')[1] and call('story_route', t, news_id='zz', room='crypto', note='x')[1]
    assert call('story_place', t, news_id='n0', codes=['IR', 'XX'])[0]['placed'] == ['IR'] and call('story_place', t, news_id='n0', codes=['XX'])[1]
    call('story_debate', t, news_id='n0', text='bull case', stance='for')
    assert call('story_read', t, news_id='n0')[0]['debate'][0]['text'] == 'bull case'
    assert call('story_debates', t, news_id='n0')[0]['turns'][0]['role'] == 'newsdesk'


def test_news_call_lab_scores_against_control(dbs):
    feed, led, tmp = dbs
    put_market(feed); put_quote(feed, YES, 0.48, 0.50); put_quote(feed, NO, 0.50, 0.52)
    t = FakeTools(tmp, role='pm_trader')
    d, err = call('news_call', t, condition_id=COND, side='YES', expected_move_cents=5, horizon='1h', confidence=3, mechanism='new fact', reason='r')
    assert not err and d['entry_ask'] == 0.5 and d['control_ask'] == 0.52
    assert call('news_call', t, condition_id=COND, side='MAYBE', expected_move_cents=5, horizon='1h', confidence=3, mechanism='m', reason='r')[1]
    pending = T.call_scores(feed, led, NOW + 10)[0]
    assert pending['marks']['15m']['state'] == 'pending'
    put_quote(feed, YES, 0.60, 0.62, at=NOW + 900); put_quote(feed, NO, 0.38, 0.40, at=NOW + 900)
    s = T.call_scores(feed, led, NOW + 1000)[0]['marks']
    fee = lambda p: 0.05 * p * (1 - p)
    assert s['15m']['state'] == 'scored' and s['15m']['call_pp'] == pytest.approx((0.60 - 0.50 - fee(0.5) - fee(0.6)) * 100, abs=0.01)
    assert s['15m']['control_pp'] == pytest.approx((0.38 - 0.52 - fee(0.52) - fee(0.38)) * 100, abs=0.01) and s['1h']['state'] == 'pending'
    assert T.call_scores(feed, led, NOW + 90000)[0]['marks']['6h']['state'] == 'censored'            # no stored bid near that horizon: censored, not zero
    put_market(feed, closed=1, resolved=1.0, accepting=0)
    assert T.call_scores(feed, led, NOW + 90000)[0]['resolved_pp'] == pytest.approx((1 - 0.5 - fee(0.5)) * 100, abs=0.01)


def test_explorer_coach_video_tools(dbs):
    feed, led, tmp = dbs
    put_market(feed); put_quote(feed, YES, 0.48, 0.50)
    t = FakeTools(tmp, role='explorer')
    d, _ = call('explorer_note', t, topic='central banks', why_this='fresh', leads=[dict(condition_id=COND, side='YES', note='cheap'), dict(condition_id='bad', side='YES')])
    assert d['leads_recorded'] == 1
    put_market(feed, closed=1, resolved=1.0, accepting=0)
    lead = call('explorer_notes', t)[0]['leads'][0]
    assert lead['price_at'] == 0.5 and lead['resolved_pp'] == 50.0
    call('lesson_add', t, lesson='size down after a stop', for_role='pm_trader')
    assert call('lessons_read', FakeTools(tmp, role='pm_trader'))[0]['lessons'][0]['lesson'].startswith('size down')
    call('skill_save', t, skill='fade', steps='1. read 2. size', why='v1'); call('skill_save', t, skill='fade', steps='1. read 2. check 3. size', why='v2')
    sk = call('skills_read', t)[0]
    assert sk['current'][0]['why'] == 'v2' and sk['history_n'] == 2
    assert call('video_rule_add', t, rule_text='buy the opening range break', source_url='http://not-https.example/v')[1]
    assert call('video_rule_add', t, rule_text='buy the opening range break', source_url='https://video.example/v')[0]['state'] == 'recorded' and call('video_rules', t)[0]['rules'][0]['status'] == 'testing'
    assert call('review_add', t, review='desk is overtrading', priorities='fewer orders')[0]['state'] == 'recorded'


class FakeSeat:
    def __init__(self, tmp, role, **cfg):
        self.office = sqlite3.connect(tmp / 'seat.db', isolation_level=None)
        self.cfg = dict(role=role, markets_db=str(tmp / 'feed.db'), markets_ledger_db=str(tmp / 'ledger.db'), **cfg)
        self.state, self.now = {}, NOW + 120
        self.clock = lambda: self.now
        self.actor = 'ag_x'


def test_story_wake_rate_limited_once_per_story_and_room_scoped(dbs):
    feed, led, tmp = dbs
    seed_stories(feed)
    s = FakeSeat(tmp, 'stocks_trader', story_rooms=['stocks'], story_wake_min_s=300)
    w = WK.source(s, False)
    assert w['type'] == 'story' and 'Nvidia earnings beat' in w['prompt'] and 'n0' in w['prompt'] and not w['urgent']
    assert WK.source(s, False) is None                                  # rate limited
    s.now += 301
    assert WK.source(s, False) is None                                  # n3 scored 40 < 60: no wake; n0 already done
    assert WK.source(FakeSeat(tmp, 'x', story_rooms=['sports']), False) is None
    m = FakeSeat(tmp, 'meme_scout', story_rooms=['memes'], story_wake_min_score=50)
    assert 'HIPPO' in WK.source(m, False)['prompt']
    assert WK.source(FakeSeat(tmp, 'y'), False) is None and WK.source(s, True) is None


def test_position_closed_alert_and_candle_wakes(dbs):
    feed, led, tmp = dbs
    put_perp(feed)
    t = FakeTools(tmp, role='perps_trader')
    pid = call('perp_order', t, coin='BTC', side='long', margin_usd=100, leverage=5, tp_pct=20, sl_pct=10, **PLAN)[0]['pos_id']
    s = FakeSeat(tmp, 'perps_trader', book_lanes=['perps'], alert_loss_pct=10, candle_wakes_pct=1.0)
    s.now = NOW + 10
    put_perp(feed, mid=98.0, at=NOW + 10)          # about -10% of margin at 5x on a 2% move
    a = WK.source(s, True)
    assert a['type'] == 'position_alert' and a['urgent'] and pid in a['prompt'] and 'loss' in a['prompt']
    assert WK.source(s, True) is None                                    # once per trigger
    put_perp(feed, mid=96.0, at=NOW + 20); s.now = NOW + 20
    from sniper_office.markets import books as B
    B.engine_tick(led, feed, NOW + 20, B.policy())                       # 4% adverse x5 = 20% of margin; the engine closes nothing yet (stop 10%)
    put_perp(feed, mid=89.0, at=NOW + 30); s.now = NOW + 30
    assert B.engine_tick(led, feed, NOW + 30, B.policy()) == {'stop_loss': 1}
    c = WK.source(s, False)
    assert c['type'] == 'position_closed' and 'stop_loss' in c['prompt'] and 'thesis=the data says so' in c['prompt'] and WK.source(s, False) is None
    start = (int(NOW) // 3600) * 3600
    feed.execute("INSERT INTO mk_perp_candles VALUES('BTC','1h',?,'ok',100,103,99,102.5,5,?,'hl',?)", (start, NOW, NOW + 20))
    cw = WK.source(s, False)
    assert cw['type'] == 'candle_move' and '2.50%' in cw['prompt'] and WK.source(s, False) is None


def test_api_shapes_for_the_floor_rooms(dbs):
    feed, led, tmp = dbs
    seed_stories(feed); put_market(feed); put_quote(feed, YES, 0.48, 0.50); put_perp(feed)
    feed.execute("INSERT INTO mk_news VALUES('d','bbc','u','Nvidia earnings beat',NULL,NULL,?,?,'n0')", (NOW, 'nvidia earnings beat'))
    C.status(feed, 'news', True, 'ok', NOW)
    from sniper_office.markets import books as B
    pol = B.policy()
    assert B.place(led, feed, 'pm-a', 'polymarket', dict(condition_id=COND, side='YES', usd=50, tp_price=.7, sl_price=.3, news_id='n1', **PLAN), NOW, pol)['state'] == 'filled'
    call('story_debate', FakeTools(tmp, role='pm_critic'), news_id='n1', text='bear case')
    nr = api.newsroom(feed, led, NOW + 5, pol)
    assert {'wire', 'calls', 'news_calls', 'debates', 'memes', 'feeds', 'room_counts'} <= set(nr)
    assert [w['news_id'] for w in nr['wire']][0] == 'n0' and nr['wire'][0]['also_from'] == ['bbc'] and nr['calls'][0]['news_id'] == 'n1' and nr['debates']['n1'][0]['text'] == 'bear case' and nr['memes'][0]['ticker'] == 'HIPPO'
    sit = api.situation(feed, NOW + 5)
    assert sit['places'][0]['code'] == 'US' and sit['stories'][0]['title'] == 'Fed cuts rates' and 'perps' in sit['stories'][0]['rooms']
    assert api.strategy_lab(led)['strategies'] and api.paper_labs(feed, led, NOW + 5, pol)['books'][0]['book_id'] == 'pm-a'
    ctl = api.control(feed, led, NOW + 5, pol)
    assert ctl['books'][0]['halted'] is False and ctl['universe']['pm_markets'] == 1 and ctl['feeds'][0]['feed'] == 'news'
    assert api.book_halt(led, 'pm-a', True, 'operator', NOW)['halted'] is True and api.book_halt(led, 'nope', True, 'operator', NOW)['reason'] == 'book_unknown'
    assert B.place(led, feed, 'pm-a', 'polymarket', dict(condition_id=COND, side='YES', usd=50, tp_price=.7, sl_price=.3, **PLAN), NOW, pol)['reason'] == 'book_halted'
    assert api.operator_say(led, 'operator', 'pm_trader', 'size down', NOW)['state'] == 'recorded' and api.operator_say(led, 'o', None, ' ', NOW)['state'] == 'refused'
    assert api.coach(led)['lessons'] == [] and api.explorer(feed, led, NOW)['notes'] == [] and api.video_lab(led)['rules'] == [] and api.sports(feed, NOW)['games'] == []
    json.dumps([nr, sit, ctl])      # all of it is plain JSON


def test_panels_register_on_a_host(dbs):
    feed, led, tmp = dbs
    seed_stories(feed)
    h = Host(dict(markets_db=str(tmp / 'feed.db'), markets_ledger_db=str(tmp / 'ledger.db')))
    api.register_panels(h)
    assert set(h.panels) == {'markets_books', 'markets_feeds', 'markets_stories'}
    doc = h.panels['markets_stories'][1](NOW)
    assert doc['shape'] == 'table' and len(doc['rows']) == 4
    assert api.register_panels(Host({})) is None


def test_seatgen_copies_runtime_keys_keeps_actors_and_denies_order_tools(tmp_path):
    from sniper_office.markets import seatgen
    head = dict(actor='ag_aaaaaaaaaaaaaaaa', epoch='ep1', generation=1, family='codex', model='gpt-6.1-sol', codex_auth='/x/auth.json', codex_sha256='abc', office_db='/srv/o/run/data/office.db', role='head',
                desk_db='/srv/o/run/data/desk.db', ticket_wakes=True, data_sources='/srv/o/run/config/data_sources.json')
    (tmp_path / 'head.json').write_text(json.dumps(head))
    assert seatgen.main(['--head', str(tmp_path / 'head.json'), '--out-dir', str(tmp_path / 'out')]) == 0
    first = {r: json.loads((tmp_path / 'out' / ('seat_%s.json' % r)).read_text()) for r in seatgen.SEATS}
    seatgen.main(['--head', str(tmp_path / 'head.json'), '--out-dir', str(tmp_path / 'out')])
    for r, cfg in first.items():
        again = json.loads((tmp_path / 'out' / ('seat_%s.json' % r)).read_text())
        assert again['actor'] == cfg['actor'] and cfg['actor'].startswith('ag_') and len(cfg['actor']) == 19 and int(cfg['actor'][3:], 16) >= 0
        assert (cfg['family'], cfg['model'], cfg['codex_auth'], cfg['codex_sha256']) == ('codex', 'gpt-6.1-sol', '/x/auth.json', 'abc') and 'desk_db' not in cfg and 'ticket_wakes' not in cfg
        assert cfg['plugins'] == ['sniper_office.desk', 'sniper_office.markets'] and cfg['role'] == r and cfg['prompt_file'].endswith('/prompts/%s.md' % r)
    assert len({c['actor'] for c in first.values()}) == len(first)
    assert 'pm_order' not in first['pm_trader']['denied_tools'] and 'perp_order' in first['pm_trader']['denied_tools']
    assert set(first['pm_critic']['denied_tools']) == {'pm_order', 'pm_exit', 'perp_order', 'perp_exit', 'stock_order', 'stock_exit'} and set(first['coach']['denied_tools']) == set(first['pm_critic']['denied_tools'])
    assert first['perps_trader']['book_lanes'] == ['perps'] and first['meme_scout']['story_rooms'] == ['memes'] and 'book_lanes' not in first['newsdesk']
    import sniper_office.markets as M
    from pathlib import Path
    for r in seatgen.SEATS:
        assert (Path(M.__file__).parent / 'prompts' / ('%s.md' % r)).read_text().startswith('Paper only.')
    h = Host({})
    M.register(h)
    for cfg in first.values():       # every tool a seat is denied, and every group it names, exists
        assert set(cfg['denied_tools']) <= set(h.tools) and set(cfg['tool_groups']) - {'brain'} <= {t.group for t in h.tools.values()}


def test_written_strategies_cover_each_lane_and_the_prompts_name_real_tools():
    from sniper_office.markets import strategies as ST
    from pathlib import Path
    import re
    import sniper_office.markets as M
    for lane, n in (('polymarket', 8), ('perps', 3), ('stocks', 2)):
        assert len(ST.options(lane)) >= n
    h = Host({})
    M.register(h)
    names = set(h.tools) | {'mail_read', 'mail_send', 'db_read', 'brain_read', 'brain_note', 'web_search', 'browse', 'shell', 'sleep', 'request', 'notify_operator'}
    for f in (Path(M.__file__).parent / 'prompts').glob('*.md'):
        called = set(re.findall(r'`([a-z_]+)\(', f.read_text()))
        assert called <= names, (f.name, called - names)
