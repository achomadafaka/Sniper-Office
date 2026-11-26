"""Newsroom and Situation read API: 'other' (no rule matched) is not a route, room filters apply before the limit, place counts cover the whole window."""
from sniper_office.markets import api as A, common as C

NOW = 1_000_000.0


def feed(tmp_path, n=30):
    db = C.connect(str(tmp_path / 'm.db'), C.FEED_SCHEMA)
    for i in range(n):
        nid = 's%02d' % i
        db.execute('INSERT INTO mk_news(news_id,source,title,fetched_at) VALUES(?,?,?,?)', (nid, 'src%d' % (i % 2), 't' + nid, NOW - 100 - i))
        db.execute("INSERT INTO mk_routes(news_id,room,score,classifier,routed_at) VALUES(?,'other',10,'rules',?)", (nid, NOW))
        db.execute("INSERT INTO mk_story_places(news_id,code,name,lat,lon,classifier,at) VALUES(?,'CN','China',35,105,'rules',?)", (nid, NOW))
    # only the oldest story is a real sports route
    db.execute("INSERT INTO mk_routes(news_id,room,score,classifier,routed_at) VALUES('s29','sports',60,'rules',?)", (NOW,))
    db.commit()
    return db


def test_other_is_not_a_route_and_room_filter_runs_before_the_limit(tmp_path):
    db = feed(tmp_path)
    w = A.wire(db, NOW, n=5)
    assert len(w) == 5 and all(not s['routes'] for s in w)
    assert [s['news_id'] for s in A.wire(db, NOW, n=5, room='sports')] == ['s29']
    assert A.wire(db, NOW, n=5, room='other') == []


def test_situation_counts_places_over_the_window_not_the_page(tmp_path):
    s = A.situation(feed(tmp_path), NOW, n=10)
    assert len(s['stories']) == 10 and s['placed_total'] == 30
    assert s['places'][0]['code'] == 'CN' and s['places'][0]['n'] == 30
    assert all('other' not in x['rooms'] for x in s['stories'])
