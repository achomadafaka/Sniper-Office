"""News intake host process: public RSS/Atom feeds (the source project's wire list, no keys) into mk_news, each new story classified by router.py into mk_routes.
`python3 -m sniper_office.markets.news --config news.json [--once]`. https only, no redirects, 1 MB cap. A story keeps the publisher's own stamp (NULL when it has none, never filled from our clock) and our fetch time.
Not ported: the source project's X list reads (they need an account key). Add a feed by URL in the config."""
import hashlib
import html
import re
import xml.etree.ElementTree as ET
from xml.parsers import expat
from email.utils import parsedate_to_datetime
from datetime import datetime

from . import common as C, router as R

FEEDS = (  # (name, url, poll_s): the source project's list; each answered 200 with that day's items when it was chosen
    ('bbc_world', 'https://feeds.bbci.co.uk/news/world/rss.xml', 30), ('cnbc_top', 'https://www.cnbc.com/id/100003114/device/rss/rss.html', 30),
    ('fed_press', 'https://www.federalreserve.gov/feeds/press_all.xml', 60), ('npr_news', 'https://feeds.npr.org/1001/rss.xml', 60), ('guardian_world', 'https://www.theguardian.com/world/rss', 60),
    ('reuters_gn', 'https://news.google.com/rss/search?q=when:1h+site:reuters.com&hl=en-US&gl=US&ceid=US:en', 60), ('ap_gn', 'https://news.google.com/rss/search?q=when:1h+site:apnews.com&hl=en-US&gl=US&ceid=US:en', 60),
    ('gn_top', 'https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en', 60),
    ('gn_business', 'https://news.google.com/rss/topics/CAAqKggKIiRDQkFTRlFvSUwyMHZNRGx6TVdZU0JXVnVMVlZUR2dKVlV5Z0FQAQ?hl=en-US&gl=US&ceid=US:en', 60),
    ('gn_world', 'https://news.google.com/rss/topics/CAAqKggKIiRDQkFTRlFvSUwyMHZNRGx1YlY4U0JXVnVMVlZUR2dKVlV5Z0FQAQ?hl=en-US&gl=US&ceid=US:en', 60),
    ('gn_sports', 'https://news.google.com/rss/topics/CAAqKggKIiRDQkFTRlFvSUwyMHZNRFp1ZEdvU0JXVnVMVlZUR2dKVlV5Z0FQAQ?hl=en-US&gl=US&ceid=US:en', 60),
    ('nyt_world', 'https://rss.nytimes.com/services/xml/rss/nyt/World.xml', 60), ('nyt_politics', 'https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml', 60),
    ('nyt_business', 'https://rss.nytimes.com/services/xml/rss/nyt/Business.xml', 60), ('politico', 'https://rss.politico.com/politics-news.xml', 60), ('thehill', 'https://thehill.com/homenews/feed/', 60),
    ('aljazeera', 'https://www.aljazeera.com/xml/rss/all.xml', 60), ('sky_world', 'https://feeds.skynews.com/feeds/rss/world.xml', 60), ('bbc_business', 'https://feeds.bbci.co.uk/news/business/rss.xml', 60),
    ('bbc_sport', 'https://feeds.bbci.co.uk/sport/rss.xml', 60), ('espn_top', 'https://www.espn.com/espn/rss/news', 60), ('cnbc_economy', 'https://www.cnbc.com/id/20910258/device/rss/rss.html', 60),
    ('cnbc_politics', 'https://www.cnbc.com/id/10000113/device/rss/rss.html', 60), ('cnbc_finance', 'https://www.cnbc.com/id/10000664/device/rss/rss.html', 60),
    ('wsj_markets', 'https://feeds.a.dj.com/rss/RSSMarketsMain.xml', 60), ('bloomberg_markets', 'https://www.bloomberg.com/feeds/markets/news.rss', 60),
    ('bloomberg_politics', 'https://www.bloomberg.com/feeds/politics/news.rss', 60), ('coindesk', 'https://www.coindesk.com/arc/outboundfeeds/rss', 60), ('cointelegraph', 'https://cointelegraph.com/rss', 60),
    ('theblock', 'https://www.theblock.co/rss.xml', 60), ('sec_press', 'https://www.sec.gov/news/pressreleases.rss', 60), ('whitehouse', 'https://www.whitehouse.gov/news/feed/', 60),
    ('ecb_press', 'https://www.ecb.europa.eu/rss/press.html', 60), ('oman_fm', 'https://www.fm.gov.om/feed/', 300), ('irna_en', 'https://en.irna.ir/rss', 120),
)
DUP_WINDOW_S = 6 * 3600
TAG = re.compile(r'<[^>]+>')


def _local(tag):
    return tag.rsplit('}', 1)[-1] if isinstance(tag, str) else ''


def _stamp(text):
    if not text:
        return None
    try:
        return parsedate_to_datetime(text.strip()).timestamp()
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(text.strip().replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def clean(text, cap=600):
    return ' '.join(html.unescape(TAG.sub(' ', text or '')).split())[:cap]


def parse(raw):
    """[(guid, title, url, summary, published)] from RSS 2.0 or Atom bytes; [] when it does not parse. Any DTD or entity declaration rejects the feed (expat handlers, not a byte search)."""
    def refuse(*args):
        raise ValueError('dtd_refused')
    check = expat.ParserCreate()                     # a first pass whose only job is to reject any DTD or entity declaration, in any encoding and anywhere in the document
    check.StartDoctypeDeclHandler = refuse
    check.EntityDeclHandler = refuse
    try:
        check.Parse(raw, True)
        root = ET.fromstring(raw)
    except (ET.ParseError, ValueError, expat.ExpatError):
        return []
    items = []
    for el in root.iter():
        if _local(el.tag) not in ('item', 'entry'):
            continue
        f = {}
        for ch in el:
            k = _local(ch.tag)
            if k == 'link' and ch.get('href'):
                f.setdefault('link', ch.get('href') if ch.get('rel') in (None, 'alternate') else f.get('link'))
            elif k in ('title', 'link', 'guid', 'id', 'description', 'summary', 'content', 'pubDate', 'published', 'updated', 'date'):
                f.setdefault(k, (ch.text or '').strip())
        title = clean(f.get('title'), 300)
        if title:
            items.append((f.get('guid') or f.get('id') or f.get('link') or title, title, f.get('link') or None,
                          clean(f.get('description') or f.get('summary') or f.get('content')), _stamp(f.get('pubDate') or f.get('published') or f.get('updated') or f.get('date'))))
    return items[:60]


def ingest(feed, source, items, now, hook=None):
    """Store new items, classify the first of each story. Returns (seen, new)."""
    markets = feed.execute('SELECT condition_id,question FROM mk_pm_markets WHERE closed=0 OR closed IS NULL').fetchall()
    new = 0
    for guid, title, url, summary, published in items:
        nid = hashlib.sha256(('%s|%s' % (source, guid)).encode()).hexdigest()[:20]
        if feed.execute('SELECT 1 FROM mk_news WHERE news_id=?', (nid,)).fetchone():
            continue
        key = R.title_key(title)
        dup = feed.execute('SELECT news_id FROM mk_news WHERE title_key=? AND dup_of IS NULL AND fetched_at>?', (key, now - DUP_WINDOW_S)).fetchone()
        feed.execute('INSERT INTO mk_news VALUES(?,?,?,?,?,?,?,?,?)', (nid, source, url, title, summary, published, now, key, dup[0] if dup else None))
        new += 1
        if not dup:
            R.store(feed, nid, dict(source=source, title=title), R.classify(dict(source=source, title=title, summary=summary), markets, hook), now)
    return len(items), new


def tick(cfg, now, fetch=None):
    feed = C.connect(cfg['markets_db'], C.FEED_SCHEMA)
    feeds = [tuple(f) for f in cfg.get('feeds') or FEEDS]
    done = bad = new_total = 0
    for name, url, poll_s in feeds:
        last = feed.execute('SELECT MAX(fetched_at) FROM mk_news_polls WHERE source=? AND url=?', (name, url)).fetchone()[0]
        if last is not None and now - last < poll_s:
            continue
        try:
            if not C.public_url(url):
                raise ValueError('url_refused')
            seen, new = ingest(feed, name, parse(C.FETCH(url)), now, cfg.get('classifier_hook'))
            feed.execute('INSERT INTO mk_news_polls VALUES(?,?,?,?,?,?,?)', (name, url, now, 'ok', seen, new, None))
            done, new_total = done + 1, new_total + new
        except Exception as exc:
            bad += 1
            feed.execute('INSERT INTO mk_news_polls VALUES(?,?,?,?,?,?,?)', (name, url, now, 'error', None, None, '%s: %s' % (type(exc).__name__, str(exc)[:160])))
    keep = now - 86400 * cfg.get('retention_days', 14)
    feed.execute('DELETE FROM mk_news WHERE fetched_at<?', (keep,))
    feed.execute('DELETE FROM mk_news_polls WHERE fetched_at<?', (now - 86400 * 3,))
    feed.execute('DELETE FROM mk_routes WHERE news_id NOT IN (SELECT news_id FROM mk_news)')
    feed.execute('DELETE FROM mk_story_places WHERE news_id NOT IN (SELECT news_id FROM mk_news)')
    detail = 'feeds_ok=%d feeds_error=%d new_stories=%d' % (done, bad, new_total)
    C.status(feed, 'news', bad == 0 or done > 0, detail, now)
    return detail


def main(argv=None):
    return C.main_loop('markets-news', tick, argv, 30.0)


if __name__ == '__main__':
    raise SystemExit(main())
