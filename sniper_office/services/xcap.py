"""X source-graph card capture: a local receiver for public post cards, their parse and their store. Display-only evidence; nothing here follows, posts, trades or sizes.

A userscript in the operator's own logged-in browser POSTs the HTML of visible post cards to this process on localhost. Each card is sanitized (only public structural landmarks kept; scripts,
forms, session material dropped, secret-looking attributes refuse the card), parsed into direct / repost / quote / reply cards, normalized (handles, coin mentions with the chain taken only from the
content itself) and stored in the Office's own db. Observation time is the corrected clock; with no fresh clock sample a row is kept as clock_domain 'legacy_unknown' and is excluded from reads.
Ported from the source project's receiver, DOM parser and normalizer; stdlib only (a small html.parser tree replaces the HTML library).

    python3 -m sniper_office.services.xcap --config x_receiver.json
Config: x_db, clock_file, env_file + token_key (the shared token the userscript sends in X-Office-Token; the value is never logged), host (127.0.0.1), port, handles (optional allowlist)."""
import argparse
from html.parser import HTMLParser
import hmac
import http.server
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse, urlsplit, urlunsplit

from . import common

PARSER_VERSION = 2
MAX_CARDS, MAX_CARD_BYTES, MAX_BODY = 50, 256 * 1024, 8 * 1024 * 1024
STATUSES = frozenset({'ok', 'no_new', 'coverage_gap', 'wrong_feed', 'logged_out', 'challenge', 'rate_limited', 'page_unready', 'parser_error'})
NON_CAPTURE = frozenset({'wrong_feed', 'logged_out', 'challenge', 'rate_limited', 'page_unready', 'parser_error'})
RUN_ID = re.compile(r'[A-Za-z0-9_.-]{1,64}\Z')

# ---- sanitized mini DOM -------------------------------------------------------------------------------------------------------------------------------------------------------------
DROP_TAGS = frozenset(('script', 'style', 'form', 'input', 'textarea', 'iframe', 'object', 'embed'))
VOID = frozenset(('br', 'img', 'hr', 'meta', 'link', 'input', 'source', 'wbr'))
KEEP_ATTRS = frozenset({'aria-label', 'data-testid', 'datetime', 'dir', 'href', 'lang', 'role'})
FORBIDDEN_ATTRS = frozenset({'authorization', 'cookie', 'set-cookie', 'storage_state', 'auth_token'})
SECRET_MARKERS = ('bearer ', 'auth_token=', 'sessionid=', 'ct0=')


class Text:
    def __init__(self, data, parent):
        self.data, self.parent = data, parent


class Node:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent, self.children = tag, attrs, parent, []

    def walk(self):
        for child in self.children:
            yield child
            if isinstance(child, Node):
                yield from child.walk()

    def nodes(self, tag=None, testid=None, href_has=None, href=False):
        for n in self.walk():
            if not isinstance(n, Node) or (tag and n.tag != tag) or (testid and n.attrs.get('data-testid') != testid):
                continue
            if (href_has and href_has not in n.attrs.get('href', '')) or (href and 'href' not in n.attrs):
                continue
            yield n

    def first(self, **kw):
        return next(self.nodes(**kw), None)

    def text(self):
        return ''.join(c.data for c in self.walk() if isinstance(c, Text))

    def find_parent(self, pred):
        node = self.parent
        while node is not None:
            if pred(node):
                return node
            node = node.parent
        return None


def _safe_href(value):
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if not parts.scheme and not parts.netloc:
        return 'https://x.com' + parts.path if parts.path.startswith('/') else None
    if parts.scheme not in ('http', 'https') or not parts.netloc:
        return None
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, '', ''))


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node('#root', {}, None)
        self.cur, self.drop, self.secret = self.root, 0, False

    def _attrs(self, attrs):
        kept = {}
        for key, value in attrs:
            value = value or ''
            if key.lower() in FORBIDDEN_ATTRS or any(m in value.lower() for m in SECRET_MARKERS):
                self.secret = True
            if key in KEEP_ATTRS:
                if key == 'href':
                    value = _safe_href(value)
                    if value is None:
                        continue
                kept[key] = value
        return kept

    def handle_starttag(self, tag, attrs):
        if self.drop or tag in DROP_TAGS:
            self.drop += 0 if tag in VOID else 1
            return
        node = Node(tag, self._attrs(attrs), self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        if not self.drop and tag not in DROP_TAGS:
            self.cur.children.append(Node(tag, self._attrs(attrs), self.cur))

    def handle_endtag(self, tag):
        if self.drop:
            self.drop -= 1 if tag in DROP_TAGS or self.drop > 0 else 0
            return
        node = self.cur
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self.cur = node.parent

    def handle_data(self, data):
        if not self.drop:
            self.cur.children.append(Text(data, self.cur))


def sanitize(html):
    """The kept tree of a card fragment. ValueError('secret_material') or ('not_tweet_article') refuses the card."""
    b = _Builder()
    b.feed(html or '')
    b.close()
    if b.secret:
        raise ValueError('secret_material')
    if b.root.first(tag='article', testid='tweet') is None:
        raise ValueError('not_tweet_article')
    return b.root


# ---- parse ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------
_STATUS_RE = re.compile(r'https?://x\.com/([A-Za-z0-9_]{1,15})/status/(\d+)')
_PROFILE_RE = re.compile(r'https?://x\.com/([A-Za-z0-9_]{1,15})$')
_REPOST_RE = re.compile(r'\b(?:reposted|republic[oó]|repost[eé]|repostou|hat repostet)\b', re.I)
_REPLY_RE = re.compile(r'(?:\breplying to\b|\ben respuesta a\b|\ben réponse à\b|\bem resposta a\b|\bantwort an\b|\bin risposta a\b|返信先|답글 대상)', re.I)


def _inbox(node):
    return node.find_parent(lambda t: t.tag == 'div' and t.attrs.get('role') == 'link') is not None


def _card(pid, author, actor, rel, text, complete, posted, urls, ref_author=None, ref_id=None):
    return dict(canonical_post_id=pid, url='https://x.com/%s/status/%s' % (author, pid), author_handle=author, actor_handle=actor, relationship=rel, full_text=text, text_complete=complete,
                posted_at_raw=posted, urls=urls, referenced_author=ref_author, referenced_post_id=ref_id)


def _complete(tt):
    if tt is None:
        return True
    scope = tt.parent or tt
    if scope.first(testid='tweet-text-show-more-link') is not None:
        return False
    return not re.search(r'\b(?:show more|mostrar más)\b', scope.text(), re.I)


def _external(node, skip_box):
    urls = []
    for a in node.nodes(tag='a', href=True):
        h = a.attrs['href']
        if h.startswith('http') and 'x.com' not in h and 'twitter.com' not in h and not (skip_box and _inbox(a)) and h not in urls:
            urls.append(h)
    return urls


def _referenced(tt, actor, rel):
    if tt is None:
        return None
    box = tt.find_parent(lambda t: t.tag == 'div' and t.attrs.get('role') == 'link')
    anchor = box.first(tag='a', href_has='/status/') if box else None
    m = _STATUS_RE.search(anchor.attrs.get('href', '')) if anchor else None
    if not m:
        return None
    author, pid = m.groups()
    t = box.first(tag='time')
    return _card(pid, author, actor, rel, tt.text().strip(), _complete(tt), t.attrs.get('datetime') if t else None, _external(box, False), ref_author=author, ref_id=pid)


def parse_article(root):
    """-> (cards, diagnostics) for one sanitized tree. Attribution comes only from the DOM; nested quoted or replied text is never flattened into the outer author's text."""
    art = root.first(tag='article', testid='tweet')
    if art.first(testid='placementTracking') is not None:
        return (), ('promoted_card',)
    ta = next((a for a in art.nodes(tag='a', href_has='/status/') if a.first(tag='time')), None)
    m = _STATUS_RE.search(ta.attrs.get('href', '')) if ta else None
    if not m:
        return (), ('missing_post_id',)
    author, pid = m.groups()
    t = art.first(tag='time')
    posted = t.attrs.get('datetime') if t else None
    texts = list(art.nodes(testid='tweetText'))
    outer = [x for x in texts if not _inbox(x)]
    full = outer[0].text().strip() if outer else ''
    complete = _complete(outer[0] if outer else None)
    urls = _external(art, True)
    sc = art.first(testid='socialContext')
    sa = sc.find_parent(lambda n: n.tag == 'a' and 'href' in n.attrs) if sc else None
    sp = _PROFILE_RE.match(sa.attrs.get('href', '')) if sa else None
    reposter = sp.group(1) if sp else None
    label = bool(sc and _REPOST_RE.search(sc.text()))
    is_repost = bool(sc and reposter and (reposter.lower() != author.lower() or label))
    is_reply = any(isinstance(n, Text) and _REPLY_RE.search(n.data) and not _inbox(n.parent) for n in art.walk())
    qbox = next((x for x in texts if _inbox(x)), None)
    diags = []
    if label and not reposter:
        return (), ('missing_actor',)
    if is_repost:
        cards = [_card(pid, author, reposter, 'repost', full, complete, posted, urls, ref_author=author, ref_id=pid)]
        if qbox:
            diags.append('missing_referenced_post_id')
    else:
        cards = [_card(pid, author, author, 'direct', full, complete, posted, urls)]
        if is_reply or qbox:
            ref = _referenced(qbox, author, 'reply' if is_reply else 'quote')
            if ref:
                cards.append(ref)
            else:
                diags.append('missing_referenced_post_id')
    return tuple(cards), tuple(diags)


# ---- normalize ------------------------------------------------------------------------------------------------------------------------------------------------------------------------
_EVM_RE = re.compile(r'(?<![A-Za-z0-9])0x[a-fA-F0-9]{40}(?![A-Za-z0-9])')
_SOL_CA_RE = re.compile(r'\b(?i:ca|contract)\s*[:=]\s*([1-9A-HJ-NP-Za-km-z]{32,44})\b')
_TICKER_RE = re.compile(r'(?<![A-Za-z0-9])\$([A-Za-z][A-Za-z0-9]{1,14})\b')
SLUGS = {'solana': 'solana', 'base': '8453', 'ethereum': '1', 'eth': '1', 'bsc': '56', 'bnb': '56', 'avalanche': '43114', 'avax': '43114', 'robinhood': '4663'}
_MARKERS = ((re.compile(r'\b(?:robinhood\s+chain|chain\s*4663)\b', re.I), '4663'), (re.compile(r'\b(?:on\s+base|base\s+chain)\b', re.I), '8453'),
            (re.compile(r'\bsolana\b', re.I), 'solana'), (re.compile(r'\b(?:ethereum|on\s+eth)\b', re.I), '1'))


def handle_of(h):
    v = str(h).strip().lstrip('@') if h is not None else ''
    return v.lower() or None


def is_evm(a):
    return bool(a) and bool(re.fullmatch(r'0x[a-fA-F0-9]{40}', a))


def is_sol(a):
    return bool(a) and not a.startswith('0x') and bool(re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}', a))


def _host(host, d):
    return host == d or host.endswith('.' + d)


def _url_fact(url):
    p = urlparse(url or '')
    host, parts = (p.hostname or '').lower(), [unquote(x) for x in p.path.split('/') if x]
    if _host(host, 'dexscreener.com'):
        return SLUGS.get(parts[0].lower()) if parts else None, 'dex_url', None        # a pair page: its path id is not a mint
    if _host(host, 'pump.fun') or _host(host, 'bonk.fun'):
        mint = parts[-1] if len(parts) >= 2 and parts[-2].lower() in ('coin', 'token') else None
        return 'solana', 'launchpad_url', mint if mint and is_sol(mint) else None
    if _host(host, 'hood.fun'):
        mint = parts[-1] if len(parts) >= 2 and parts[-2].lower() in ('coin', 'token') else None
        return '4663', 'launchpad_url', mint if mint and is_evm(mint) else None
    return None, None, None


def _mk(network, mint, ticker, method):
    return dict(network_id=network, mint=mint, ticker=ticker, extraction_method=method, ambiguous=bool(mint) and not network, eligible=bool(mint) and bool(network))


def mentions(text, urls):
    text, urls, out, seen = text or '', urls or [], [], set()
    nets = {n for pat, n in _MARKERS if pat.search(text)} | {n for n in (_url_fact(u)[0] for u in urls) if n}
    sole = next(iter(nets)) if len(nets) == 1 else None

    def add(m):
        k = (m['network_id'], m['mint'], m['ticker'], m['extraction_method'])
        if k not in seen:
            seen.add(k)
            out.append(m)
    for u in urls:
        net, method, mint = _url_fact(u)
        if method and mint:
            add(_mk(net, mint.lower() if is_evm(mint) else mint, None, method))
    for raw in _EVM_RE.findall(text):
        net = sole if sole and sole != 'solana' else None
        add(_mk(net, raw.lower(), None, 'contract_text'))
    for raw in _SOL_CA_RE.findall(text):
        if is_sol(raw):
            add(_mk('solana' if not nets or nets == {'solana'} else None, raw, None, 'contract_text'))
    for tk in _TICKER_RE.findall(text):
        add(_mk(None, None, tk.upper(), 'ticker_only'))      # a ticker collides: hypothesis evidence, never eligible
    return out


def _iso(raw):
    try:
        v = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
    except ValueError:
        return None
    return v.astimezone(timezone.utc).isoformat() if v.tzinfo else None


def normalize(card):
    pid, author, actor, rel = card.get('canonical_post_id'), handle_of(card.get('author_handle')), handle_of(card.get('actor_handle')), card.get('relationship')
    ms = mentions(card.get('full_text', ''), card.get('urls') or [])
    if not card.get('text_complete'):
        ms = [dict(m, eligible=False) for m in ms]
    if not pid or not author or not actor or rel not in ('direct', 'repost', 'quote', 'reply') or (rel == 'direct' and actor != author):
        return None
    return dict(post=dict(id=str(pid), url=card.get('url'), author=author, text=card.get('full_text'), complete=bool(card.get('text_complete')), posted_at=_iso(card.get('posted_at_raw')),
                          urls=card.get('urls') or []),
                relation=dict(actor=actor, relationship=rel, ref_author=handle_of(card.get('referenced_author')), ref_post=card.get('referenced_post_id')), mentions=ms)


# ---- store ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------
SCHEMA = '''
CREATE TABLE IF NOT EXISTS x_runs(run_id TEXT PRIMARY KEY, received_at REAL NOT NULL, observed_at REAL, clock_domain TEXT NOT NULL, status TEXT NOT NULL, cards_in INTEGER NOT NULL, posts_new INTEGER NOT NULL, diagnostics TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS x_posts(post_id TEXT PRIMARY KEY, url TEXT, author TEXT NOT NULL, text TEXT, text_complete INTEGER NOT NULL, posted_at TEXT, urls_json TEXT NOT NULL, parser_version INTEGER NOT NULL,
  first_seen_at REAL, clock_domain TEXT NOT NULL, run_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS x_relations(post_id TEXT NOT NULL, actor TEXT NOT NULL, relationship TEXT NOT NULL, ref_author TEXT, ref_post TEXT, seen_at REAL, run_id TEXT NOT NULL, PRIMARY KEY(post_id, actor, relationship));
CREATE TABLE IF NOT EXISTS x_mentions(post_id TEXT NOT NULL, network_id TEXT NOT NULL DEFAULT '', mint TEXT NOT NULL DEFAULT '', ticker TEXT NOT NULL DEFAULT '', method TEXT NOT NULL, ambiguous INTEGER NOT NULL, eligible INTEGER NOT NULL,
  seen_at REAL, clock_domain TEXT NOT NULL, run_id TEXT NOT NULL, PRIMARY KEY(post_id, network_id, mint, ticker, method));
CREATE INDEX IF NOT EXISTS x_mentions_mint ON x_mentions(network_id, mint);
CREATE INDEX IF NOT EXISTS x_mentions_ticker ON x_mentions(ticker);
'''


def open_db(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript(SCHEMA)
    return conn


def ingest(conn, payload, *, observed_at, received_at, handles=None):
    """Store one delivered batch. -> dict(state, run_id, cards, posts_new, diagnostics). A run id is stored once; a repeat delivery is a no-op."""
    if not isinstance(payload, dict) or not RUN_ID.match(str(payload.get('run_id', ''))) or payload.get('status') not in STATUSES:
        raise ValueError('bad_payload')
    cards = payload.get('cards', [])
    if not isinstance(cards, list) or len(cards) > MAX_CARDS or any(not isinstance(c, str) or len(c.encode()) > MAX_CARD_BYTES for c in cards):
        raise ValueError('bad_cards')
    run, domain = payload['run_id'], 'observed_real_utc' if observed_at is not None else 'legacy_unknown'
    diags, new = [], 0
    with conn:
        if conn.execute('SELECT 1 FROM x_runs WHERE run_id=?', (run,)).fetchone():
            return dict(state='duplicate', run_id=run)
        if payload['status'] not in NON_CAPTURE:
            for html in cards:
                try:
                    batch, d = parse_article(sanitize(html))
                except ValueError as exc:
                    diags.append(str(exc))
                    continue
                diags.extend(d)
                for card in batch:
                    n = normalize(card)
                    if n is None:
                        diags.append('invalid_card')
                        continue
                    if handles is not None and n['relation']['actor'] not in handles and n['post']['author'] not in handles:
                        diags.append('handle_not_allowed')
                        continue
                    p = n['post']
                    new += conn.execute('INSERT OR IGNORE INTO x_posts VALUES(?,?,?,?,?,?,?,?,?,?,?)', (p['id'], p['url'], p['author'], p['text'], p['complete'], p['posted_at'], json.dumps(p['urls']),
                                                                                                         PARSER_VERSION, observed_at, domain, run)).rowcount
                    r = n['relation']
                    conn.execute('INSERT OR IGNORE INTO x_relations VALUES(?,?,?,?,?,?,?)', (p['id'], r['actor'], r['relationship'], r['ref_author'], r['ref_post'], observed_at, run))
                    for m in n['mentions']:
                        conn.execute('INSERT OR IGNORE INTO x_mentions VALUES(?,?,?,?,?,?,?,?,?,?)', (p['id'], m['network_id'] or '', m['mint'] or '', m['ticker'] or '', m['extraction_method'],
                                                                                                      m['ambiguous'], m['eligible'], observed_at, domain, run))
        conn.execute('INSERT INTO x_runs VALUES(?,?,?,?,?,?,?,?)', (run, received_at, observed_at, domain, payload['status'], len(cards), new, json.dumps(sorted(set(diags)))))
    return dict(state='stored', run_id=run, cards=len(cards), posts_new=new, diagnostics=sorted(set(diags)), clock_domain=domain)


def mentions_of(path, *, mint=None, ticker=None, network_id=None, since=None, limit=50):
    """Read-only: stored mentions of a coin (by mint, or a ticker as ambiguous hypothesis evidence), newest first, observed-clock rows only."""
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=1.)
    try:
        q, a = ["m.clock_domain='observed_real_utc'"], []
        for col, v in (('m.mint', mint), ('m.ticker', ticker.upper() if ticker else None), ('m.network_id', network_id)):
            if v:
                q.append(col + '=?')
                a.append(v)
        if since is not None:
            q.append('m.seen_at>=?')
            a.append(since)
        rows = conn.execute('SELECT m.post_id,p.author,p.text,p.posted_at,m.network_id,m.mint,m.ticker,m.method,m.eligible,m.seen_at,p.text_complete FROM x_mentions m JOIN x_posts p USING(post_id) WHERE '
                            + ' AND '.join(q) + ' ORDER BY m.seen_at DESC LIMIT ?', a + [max(1, min(int(limit), 200))]).fetchall()
    finally:
        conn.close()
    keys = ('post_id', 'author', 'text', 'posted_at', 'network_id', 'mint', 'ticker', 'method', 'eligible', 'seen_at', 'text_complete')
    return [dict(zip(keys, r)) for r in rows]


# ---- receiver -------------------------------------------------------------------------------------------------------------------------------------------------------------------------
ORIGINS = ('https://x.com', 'https://twitter.com')


def make_handler(cfg, token):
    handles = {handle_of(h) for h in cfg['handles']} if cfg.get('handles') else None

    class H(http.server.BaseHTTPRequestHandler):
        server_version = 'office-xcap'

        def log_message(self, *a):
            pass

        def _send(self, code, doc=None):
            body = json.dumps(doc or {}).encode()
            self.send_response(code)
            origin = self.headers.get('Origin')
            if origin in ORIGINS:
                self.send_header('Access-Control-Allow-Origin', origin)
                self.send_header('Vary', 'Origin')
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            origin = self.headers.get('Origin')
            if origin in ORIGINS:
                for k, v in (('Access-Control-Allow-Origin', origin), ('Access-Control-Allow-Headers', 'content-type, x-office-token'), ('Access-Control-Allow-Methods', 'POST'), ('Vary', 'Origin')):
                    self.send_header(k, v)
            self.send_header('Content-Length', '0')
            self.end_headers()

        def do_POST(self):
            if self.path != '/capture' or not hmac.compare_digest(self.headers.get('X-Office-Token', '').encode(), token.encode()):
                return self._send(403, dict(state='refused', reason='auth'))
            try:
                n = int(self.headers.get('Content-Length', ''))
            except ValueError:
                return self._send(400, dict(state='refused', reason='length'))
            if not 0 < n <= MAX_BODY:
                return self._send(413, dict(state='refused', reason='size'))
            try:
                payload = json.loads(self.rfile.read(n))
                now = time.time()
                clock = common.clock_now(cfg['clock_file'], now)
                conn = open_db(cfg['x_db'])
                try:
                    doc = ingest(conn, payload, observed_at=clock, received_at=now, handles=handles)
                finally:
                    conn.close()
            except ValueError as exc:
                return self._send(400, dict(state='refused', reason=str(exc)[:40]))
            self._send(200, doc)
    return H


def serve(cfg):
    token = common.env_values(cfg['env_file']).get(cfg['token_key'], '')
    if len(token) < 16:
        raise ValueError('token_missing_or_short')
    host = cfg.get('host', '127.0.0.1')
    if host not in ('127.0.0.1', '::1', 'localhost'):
        raise ValueError('loopback_only')
    http.server.ThreadingHTTPServer((host, int(cfg.get('port', 8765))), make_handler(cfg, token)).serve_forever()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    try:
        serve(common.load_config(parser.parse_args(argv).config))
    except (OSError, ValueError, KeyError) as exc:
        print('x receiver: %s' % type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
