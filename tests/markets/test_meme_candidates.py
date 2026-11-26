import pytest

from sniper_office.markets import api, router
from conftest import NOW


@pytest.mark.parametrize('title', [
    'Trending News: Bitcoin gains on the internet',
    'Musk Escapes Scandal on Spotify Podcast',
    'BBC News calls the internet viral',
    'Viral meme called “Trending News”',
])
def test_generic_markers_do_not_route_memes(title):
    assert 'memes' not in router.classify(dict(title=title))['rooms']


def test_candidates_use_latest_seat_verdict_and_rank_after_filtering(dbs):
    feed, _, _ = dbs

    def story(key, title):
        feed.execute('INSERT INTO mk_news(news_id,source,url,title,fetched_at) VALUES(?,?,?,?,?)',
                     (key, 'bbc_world', 'https://example.test', title, NOW))

    def route(key, classifier, score, name=None, ticker=None, reason='Candidate', at=NOW):
        feed.execute("""INSERT INTO mk_routes(news_id,room,score,reason,classifier,meme_name,meme_ticker,routed_at)
                        VALUES(?,'memes',?,?,?,?,?,?)""", (key, score, reason, classifier, name, ticker, at))

    for i in range(7):
        key = str(i)
        story(key, 'Hippo escapes zoo')
        route(key, 'rules', 90, 'Hippo', 'HIPPO')
    route('0', 'seat:narrative', 20, 'Tracked Hippo', 'TRACK')
    route('1', 'seat:narrative', 99, 'Old Hippo', 'OLD', at=NOW - 1)
    route('1', 'seat:newsdesk', 85, 'Zoo Break', 'ZOO',
          'The clip has a simple visual hook. publisher1795667340 fetch1795667400')
    route('2', 'seat:narrative', 90, reason='This is not a meme.')
    story('junk', 'Trending News on the internet')
    route('junk', 'rules', 100, 'Trending News', 'TRENDING')
    story('unnamed', 'Musk on a podcast')
    route('unnamed', 'seat:newsdesk', 99)

    rows = api.meme_candidates(feed, NOW)
    assert [row['news_id'] for row in rows] == ['1', '3', '4', '5', '6']
    assert (rows[0]['name'], rows[0]['ticker'], rows[0]['score']) == ('Zoo Break', 'ZOO', 85)
    assert rows[0]['why'] == 'The clip has a simple visual hook.'
    assert '1795667340' in rows[0]['why_full']
    assert all(row['name'] and row['ticker'] is None for row in rows[1:])
