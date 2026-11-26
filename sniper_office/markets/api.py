"""Read API for the floor rooms: plain functions that take open (read-only is fine) connections to the feed db and the ledger and return JSON-able dicts. The UI wires them; nothing here serves HTTP.
Shapes are in docs/MARKETS.md. `panels(host)` also adds three operator-page panels through host.add_panel when the host config names markets_db and markets_ledger_db.
Write actions (for the UI's token-gated POST path): book_halt, operator_say."""
import json

from . import books as B, common as C, router as R, sports as SP, strategies as ST, tools as T


def _age(now, at):
    return None if at is None else round(now - at)


def feeds(feed, now):
    return [dict(feed=r['feed'], age_s=_age(now, r['at']), ok=bool(r['ok']), detail=r['detail']) for r in feed.execute('SELECT * FROM mk_feed_status ORDER BY feed')]


def wire(feed, now, n=25, room=None):
    """Newest stories (duplicates folded), each with its routes: the Newsroom wire."""
    rows = feed.execute('SELECT * FROM mk_news WHERE dup_of IS NULL ORDER BY fetched_at DESC LIMIT ?', (n,)).fetchall()
    out = []
    for r in rows:
        routes = [dict(room=x['room'], score=x['score'], why=x['reason'], classifier=x['classifier'], meme=x['meme_name'], ticker=x['meme_ticker']) for x in feed.execute('SELECT * FROM mk_routes WHERE news_id=?', (r['news_id'],))]
        if room and room not in {x['room'] for x in routes}:
            continue
        out.append(dict(news_id=r['news_id'], source=r['source'], title=r['title'], url=r['url'], published=r['published'], fetched_at=r['fetched_at'], age_s=_age(now, r['fetched_at']), routes=routes,
                        also_from=[x[0] for x in feed.execute('SELECT source FROM mk_news WHERE dup_of=?', (r['news_id'],))]))
    return out


def trading(feed, led, now, pol):
    """One entry per book: B.book_view (cash, equity, realized, unrealized, open positions with live marks, recent closed)."""
    return [B.book_view(led, feed, b['book_id'], now, pol) for b in led.execute('SELECT book_id FROM bk_books ORDER BY lane,book_id')]


def newsroom(feed, led, now, pol, wire_n=25):
    """The Newsroom room: wire, calls (positions that name a story, live), news-call lab scores, debates per story, meme candidates, feed health."""
    calls = [B.position_view(p, B.quote(feed, p['lane'], p['instrument'], now, pol), pol) for p in led.execute("SELECT * FROM bk_positions WHERE news_id IS NOT NULL ORDER BY entry_at DESC LIMIT 60")]
    debates = {}
    for d in led.execute('SELECT * FROM bk_debates ORDER BY id DESC LIMIT 200'):
        debates.setdefault(d['news_id'], []).append(dict(role=d['role'], stance=d['stance'], text=d['text'], at=d['at']))
    return dict(as_of=now, wire=wire(feed, now, wire_n), calls=calls, news_calls=T.call_scores(feed, led, now)[:40], debates=debates, memes=meme_candidates(feed, now), feeds=feeds(feed, now),
                room_counts={r[0]: r[1] for r in feed.execute('SELECT room,COUNT(*) FROM mk_routes r JOIN mk_news n USING(news_id) WHERE n.fetched_at>? GROUP BY room', (now - 86400,))})


def meme_candidates(feed, now, n=20):
    return [dict(news_id=r['news_id'], title=r['title'], source=r['source'], score=r['score'], name=r['meme_name'], ticker=r['meme_ticker'], why=r['reason'], age_s=_age(now, r['fetched_at']), proposed_by=r['classifier'])
            for r in feed.execute("SELECT r.*,n.title,n.source,n.fetched_at FROM mk_routes r JOIN mk_news n USING(news_id) WHERE r.room='memes' ORDER BY n.fetched_at DESC LIMIT ?", (n,))]


def situation(feed, now, since_s=86400):
    """The Situation room: stories with the places they were placed at (rule match, or a seat's story_place), and the places with counts. A story with no place is not on the map."""
    stories, places = [], {}
    for n in feed.execute('SELECT * FROM mk_news WHERE fetched_at>? AND dup_of IS NULL AND news_id IN (SELECT news_id FROM mk_story_places) ORDER BY fetched_at DESC LIMIT 150', (now - since_s,)):
        pl = [dict(code=p['code'], name=p['name'], lat=p['lat'], lon=p['lon'], classifier=p['classifier']) for p in feed.execute('SELECT * FROM mk_story_places WHERE news_id=?', (n['news_id'],))]
        stories.append(dict(news_id=n['news_id'], title=n['title'], source=n['source'], url=n['url'], age_s=_age(now, n['fetched_at']), places=pl, rooms=[x[0] for x in feed.execute('SELECT room FROM mk_routes WHERE news_id=?', (n['news_id'],))]))
        for p in pl:
            d = places.setdefault(p['code'], dict(p, n=0))
            d['n'] += 1
    return dict(as_of=now, stories=stories, places=sorted(places.values(), key=lambda p: -p['n']), note='places are display anchors for countries and regions named in the headline, not event locations')


def strategy_lab(led):
    return ST.results(led)


def sports(feed, now):
    return dict(as_of=now, games=[dict(r) for r in feed.execute('SELECT * FROM mk_sports_games WHERE fetched_at>? ORDER BY start_ts LIMIT 100', (now - 3600,))], edges=SP.edges(feed, now), feeds=[f for f in feeds(feed, now) if f['feed'] == 'sports'])


def paper_labs(feed, led, now, pol):
    """The Paper Labs room: the news-call lab (forced picks, control arm), every book's positions by lane with live PnL, and the strategy results."""
    trackers = [dict(r) for r in feed.execute('SELECT handle,title,total,pace,end_ts FROM mk_pm_trackers WHERE active=1 ORDER BY end_ts LIMIT 12')]
    rule = dict(temperature=feed.execute("SELECT COUNT(*) FROM mk_pm_markets WHERE closed=0 AND question LIKE '%temperature%'").fetchone()[0],
                posts=feed.execute("SELECT COUNT(*) FROM mk_pm_markets WHERE closed=0 AND (question LIKE '%tweet%' OR question LIKE '%post%')").fetchone()[0], trackers=trackers)
    return dict(as_of=now, news_calls=T.call_scores(feed, led, now), books=trading(feed, led, now, pol), strategies=ST.results(led)['totals_by_lane'], rule_markets=rule,
                not_ported='algorithm lab (runs seat-written code in a jail), post-count lane (needs a post counter source), auto-fire lane (needs the source desk\'s rule feed)')


def explorer(feed, led, now):
    return dict(as_of=now, notes=[dict(r) for r in led.execute('SELECT * FROM bk_explorer ORDER BY id DESC LIMIT 30')], leads=T.lead_outcomes(feed, led, now))


def coach(led):
    return dict(lessons=[dict(r) for r in led.execute('SELECT * FROM bk_lessons ORDER BY id DESC LIMIT 60')], skills=[dict(r) for r in led.execute('SELECT * FROM bk_skills ORDER BY id DESC LIMIT 100')],
                reviews=[dict(r) for r in led.execute('SELECT * FROM bk_reviews ORDER BY id DESC LIMIT 20')],
                retired=[dict(r) for r in led.execute('SELECT strategy_id,lane,retired_at,stats_json FROM bk_strategies WHERE retired_at IS NOT NULL ORDER BY retired_at DESC LIMIT 20')],
                halts=[dict(r) for r in led.execute("SELECT book_id,kind,at,detail_json FROM bk_events WHERE kind IN ('halt','resume','strategy_retired') ORDER BY id DESC LIMIT 20")])


def video_lab(led):
    return dict(rules=[dict(r) for r in led.execute('SELECT * FROM bk_video_rules ORDER BY at DESC LIMIT 60')])


def control(feed, led, now, pol):
    """What the Control room shows beyond the Office's own seats and wakes: feed and engine health, per-book halt and cash, the tracked-universe sizes, the operator chat, the last events."""
    return dict(as_of=now, feeds=feeds(feed, now), books=[dict(book_id=b['book_id'], lane=b['lane'], role=b['role'], halted=bool(b['halted']), cash=b['cash'], start_cash=b['start_cash']) for b in led.execute('SELECT * FROM bk_books')],
                universe=dict(pm_markets=feed.execute('SELECT COUNT(*) FROM mk_pm_markets WHERE closed=0').fetchone()[0], perp_coins=feed.execute('SELECT COUNT(DISTINCT coin) FROM mk_perp_ticks').fetchone()[0],
                              stock_symbols=feed.execute('SELECT COUNT(DISTINCT symbol) FROM mk_stock_quotes').fetchone()[0], stories_24h=feed.execute('SELECT COUNT(*) FROM mk_news WHERE fetched_at>?', (now - 86400,)).fetchone()[0]),
                events=[dict(r) for r in led.execute('SELECT * FROM bk_events ORDER BY id DESC LIMIT 40')], operator_chat=[dict(r) for r in led.execute('SELECT * FROM bk_operator_chat ORDER BY id DESC LIMIT 40')],
                jobs=[dict(name=n, every_s=s) for n, s in (('polymarket', 60), ('perps', 30), ('stocks', 60), ('news', 30), ('sports', 300), ('intel_hl', 15), ('books', 15))])


def book_halt(led, book_id, halted, by, now, reason=''):
    """Operator action: stop (or resume) new entries in one book. Exits and rails keep running."""
    if not led.execute('SELECT 1 FROM bk_books WHERE book_id=?', (book_id,)).fetchone():
        return dict(state='refused', reason='book_unknown')
    led.execute('UPDATE bk_books SET halted=? WHERE book_id=?', (1 if halted else 0, book_id))
    B.event(led, book_id, None, now, 'halt' if halted else 'resume', dict(by=by, reason=reason[:200]))
    return dict(state='known', book_id=book_id, halted=bool(halted))


def operator_say(led, sender, to_role, text, now):
    """The write path of the Control room's chat: a row the seats' mail path or the UI can show. Mail delivery to a seat stays the Office's own /api/mail."""
    if not str(text or '').strip():
        return dict(state='refused', reason='text_required')
    led.execute('INSERT INTO bk_operator_chat(at,sender,to_role,text) VALUES(?,?,?,?)', (now, str(sender)[:40], to_role, str(text)[:2000]))
    return dict(state='recorded')


def register_panels(host):
    cfg = host.cfg
    if not cfg.get('markets_db') or not cfg.get('markets_ledger_db'):
        return
    import time

    def open_():
        return C.ro(cfg['markets_db']), C.ro(cfg['markets_ledger_db']), B.policy(cfg)

    def books_panel(now):
        feed, led, pol = open_()
        rows = [[b['book_id'], b['lane'], round(b['cash'], 2), None if b['equity'] is None else round(b['equity'], 2), round(b['realized_total'], 2), None if b['unrealized_total'] is None else round(b['unrealized_total'], 2), len(b['open']), b['closed_n'], b['halted']] for b in trading(feed, led, now, pol)]
        return dict(shape='table', columns=['book', 'lane', 'cash', 'equity', 'realized', 'unrealized', 'open', 'closed', 'halted'], rows=rows, as_of=now)

    def feeds_panel(now):
        feed, _, _ = open_()
        return dict(shape='table', columns=['feed', 'age s', 'ok', 'detail'], rows=[[f['feed'], f['age_s'], f['ok'], f['detail']] for f in feeds(feed, now)], as_of=now)

    def stories_panel(now):
        feed, _, _ = open_()
        return dict(shape='table', columns=['age s', 'source', 'rooms', 'title'], rows=[[w['age_s'], w['source'], ','.join(r['room'] for r in w['routes']), w['title']] for w in wire(feed, now, 40)], as_of=now)
    host.add_panel('markets_books', 'Paper books', books_panel)
    host.add_panel('markets_feeds', 'Market feeds', feeds_panel)
    host.add_panel('markets_stories', 'Story wire', stories_panel)
