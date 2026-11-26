"""News story router: classify one story into rooms (stocks, crypto, memes, polymarket, perps, sports, other) by keyword rules, source priors and overlap with open prediction-market questions.
Sniper's desk did this with seat model calls (the scout mapped a story to markets by BM25 over question and rules text, the headline_instruments seat named instruments, the memes seat named stories likely to be memed).
Here the rules run first and are crude on purpose; `classifier_hook` ("package.module:function") may override the result, and seats can overrule through the story_route tool. A hook error is recorded, never raised."""
import importlib
import json
import math
import re

ROOMS = ('stocks', 'crypto', 'memes', 'polymarket', 'perps', 'sports', 'other')
KEYWORDS = {
    'stocks': 'earnings|shares|stock|stocks|nasdaq|s&p|dow jones|wall street|ipo|analyst|upgrade|downgrade|guidance|revenue|profit|buyback|dividend|merger|acquisition|acquire|layoffs|nvidia|apple|tesla|microsoft|amazon|alphabet|google|netflix|amd|intel|boeing|jpmorgan|goldman|walmart|disney|sec filing',
    'crypto': 'bitcoin|btc|ethereum|ether|crypto|cryptocurrency|stablecoin|solana|binance|coinbase|tether|ripple|xrp|blockchain|altcoin|defi|token|dogecoin|crypto etf|on-chain',
    'perps': 'fed|fomc|federal reserve|rate cut|rate hike|interest rate|inflation|cpi|ppi|jobs report|payrolls|unemployment|gdp|treasury|yields?|tariffs?|oil|crude|opec|brent|gold|dollar|ecb|boj|sanctions|recession|commodit\\w+|liquidat\\w+|futures',
    'polymarket': 'election|elections|president|presidential|congress|senate|supreme court|court|ruling|ceasefire|war|treaty|summit|prime minister|vote|resign\\w*|impeach\\w*|indict\\w*|trial|verdict|referendum|nominee|poll|polls|parliament|invasion|strike on|missile|sanctions|government shutdown|bill signed',
    'sports': 'nba|nfl|mlb|nhl|ufc|formula 1|f1|premier league|champions league|world cup|playoffs?|finals?|grand slam|tennis|olympic\\w*|touchdown|goal|quarterback|coach|transfer|championship|match|tournament|wimbledon|super bowl|nascar|cricket',
}
SOURCE_ROOM = {'coindesk': 'crypto', 'cointelegraph': 'crypto', 'theblock': 'crypto', 'espn_top': 'sports', 'bbc_sport': 'sports', 'gn_sports': 'sports', 'wsj_markets': 'stocks',
               'bloomberg_markets': 'stocks', 'cnbc_finance': 'stocks', 'nvidia_ir': 'stocks', 'sec_press': 'stocks', 'fed_press': 'perps', 'ecb_press': 'perps', 'cnbc_economy': 'perps',
               'whitehouse': 'polymarket', 'cnbc_politics': 'polymarket', 'politico': 'polymarket', 'thehill': 'polymarket', 'bloomberg_politics': 'polymarket'}
MEME_NAMED = re.compile(r'\b(?:viral\s+)?memes?\s+(?:called|named)\s+["“]([^"”]{2,40})["”]', re.I)
MEME_ANIMAL = re.compile(
    r"\b(hippo|cat|dog|frog|pig|monkey|ape|duck|penguin|goat|llama|squirrel|raccoon|capybara) "
    r"(escapes?|steals?|interrupts?)\b", re.I)
MEME_GENERIC = frozenset(
    'viral trending internet news meme memes breaking video podcast latest updates world sports markets '
    'bbc cnn cnbc reuters associated press guardian bloomberg time forbes tech business entertainment '
    'the a an of daily top live new york times washington post financial al jazeera fox sky nbc abc ap npr espn '.split())
WORD = re.compile(r"[a-z0-9][a-z0-9.\-']*")
STOP = frozenset('a an the of to in on at for and or is are was were be been will would by with from as that this it its after before over under than then not no yes who what when which about into up down new says say said'.split())
MIN_ROUTE = 30.0
PLACES = {   # display anchors for places (lat, lon), not inferred event locations; the same anchors the source project's Situation map used, matched here by name
    'united states': ('US', 'United States', 39, -98), 'canada': ('CA', 'Canada', 56, -106), 'mexico': ('MX', 'Mexico', 23, -102), 'britain': ('GB', 'United Kingdom', 54, -2),
    'france': ('FR', 'France', 46, 2), 'germany': ('DE', 'Germany', 51, 10), 'italy': ('IT', 'Italy', 42, 12), 'spain': ('ES', 'Spain', 40, -4), 'poland': ('PL', 'Poland', 52, 19),
    'ukraine': ('UA', 'Ukraine', 49, 32), 'russia': ('RU', 'Russia', 59, 60), 'belarus': ('BY', 'Belarus', 54, 28), 'turkey': ('TR', 'Turkiye', 39, 35), 'greece': ('GR', 'Greece', 39, 22),
    'iran': ('IR', 'Iran', 32, 54), 'israel': ('IL', 'Israel', 31, 35), 'gaza': ('PS', 'Palestinian Territories', 32, 35), 'lebanon': ('LB', 'Lebanon', 34, 36), 'syria': ('SY', 'Syria', 35, 39),
    'iraq': ('IQ', 'Iraq', 33, 44), 'saudi': ('SA', 'Saudi Arabia', 24, 45), 'yemen': ('YE', 'Yemen', 15, 48), 'qatar': ('QA', 'Qatar', 25, 51), 'oman': ('OM', 'Oman', 21, 57),
    'hormuz': ('IR', 'Iran', 32, 54), 'china': ('CN', 'China', 35, 105), 'taiwan': ('TW', 'Taiwan', 24, 121), 'japan': ('JP', 'Japan', 36, 138), 'south korea': ('KR', 'South Korea', 36, 128),
    'north korea': ('KP', 'North Korea', 40, 127), 'india': ('IN', 'India', 22, 79), 'pakistan': ('PK', 'Pakistan', 30, 70), 'afghanistan': ('AF', 'Afghanistan', 34, 66),
    'indonesia': ('ID', 'Indonesia', -3, 118), 'philippines': ('PH', 'Philippines', 13, 122), 'australia': ('AU', 'Australia', -25, 134), 'brazil': ('BR', 'Brazil', -10, -53),
    'argentina': ('AR', 'Argentina', -36, -64), 'venezuela': ('VE', 'Venezuela', 8, -66), 'colombia': ('CO', 'Colombia', 4, -73), 'cuba': ('CU', 'Cuba', 22, -79), 'egypt': ('EG', 'Egypt', 27, 30),
    'sudan': ('SD', 'Sudan', 16, 30), 'ethiopia': ('ET', 'Ethiopia', 9, 40), 'south africa': ('ZA', 'South Africa', -30, 25), 'nigeria': ('NG', 'Nigeria', 10, 8), 'kenya': ('KE', 'Kenya', 0, 38),
    'baltic': ('BALTIC SEA', 'Baltic Sea', 57, 20), 'europe': ('EUROPE', 'Europe', 50, 15), 'middle east': ('MIDDLE EAST', 'Middle East', 29, 45),
}


def tokens(text):
    return [w for w in WORD.findall(str(text).lower().replace('’', "'")) if w not in STOP and len(w) > 1]


def title_key(title):
    return ' '.join(WORD.findall(str(title).lower()))[:160]


def places(text):
    low = ' ' + str(text).lower() + ' '
    return list({v[0]: v for k, v in PLACES.items() if re.search(r'\b' + re.escape(k) + r'\b', low)}.values())[:6]


def meme_proposal(title, source=None):
    """Recognize a named meme or a concrete animal incident. Never infer a ticker."""
    named = MEME_NAMED.search(title)
    animal = MEME_ANIMAL.search(title)
    if named:
        name = named[1].strip()
        words = set(re.findall(r"[^\W\d_]+", name.casefold()))
        source_words = re.findall(r"[^\W\d_]+", (source or '').casefold())
        if not words - MEME_GENERIC - set(source_words):
            return None
        return dict(name=name, ticker=None, score=60.0, markers=['named meme'], why='The headline names a specific meme.')
    if animal:
        return dict(name=animal[0].title(), ticker=None, score=45.0, markers=['animal incident'],
                    why='The headline describes a specific animal incident.')
    return None


def overlap(title, market_rows):
    """Condition ids of open prediction markets sharing rare words with the story (BM25's idea, cheaply: summed idf of shared tokens, two or more shared words or one rare one)."""
    docs = [(r[0], set(tokens(r[1]))) for r in market_rows]
    if not docs:
        return []
    df = {}
    for _, ws in docs:
        for w in ws:
            df[w] = df.get(w, 0) + 1
    mine, out = set(tokens(title)), []
    for cid, ws in docs:
        shared = mine & ws
        score = sum(math.log(1 + len(docs) / df[w]) for w in shared)
        if shared and (len(shared) >= 2 or max(df[w] for w in shared) <= 2) and score >= 4.0:
            out.append((score, cid))
    return [cid for _, cid in sorted(out, reverse=True)[:5]]


def classify(item, market_rows=(), hook=None):
    """item: dict(source, title, summary). Returns dict(rooms={room: dict(score, reason)}, meme=None|dict, markets=[condition ids], places=[...], hook_error=None|str)."""
    text = ' '.join(filter(None, (item.get('title'), item.get('summary'))))
    rooms = {}
    for room, pat in KEYWORDS.items():
        hits = sorted({m.lower() for m in re.findall(r'\b(?:%s)\b' % pat, text, re.I)})
        if hits:
            rooms[room] = dict(score=min(100.0, 25.0 * len(hits) + 10), reason='keywords: ' + ', '.join(hits[:5]))
    prior = SOURCE_ROOM.get(item.get('source'))
    if prior:
        got = rooms.get(prior, dict(score=0.0, reason=''))
        rooms[prior] = dict(score=min(100.0, got['score'] + 20.0), reason=(got['reason'] + '; ' if got['reason'] else '') + 'source ' + item['source'])
    if 'crypto' in rooms and re.search(r'\b(surge[sd]?|plunge[sd]?|crash\w*|rall\w+|liquidat\w+|etf|hack\w*)\b', text, re.I):
        rooms['perps'] = dict(score=max(rooms.get('perps', {}).get('score', 0.0), rooms['crypto']['score'] - 10), reason='crypto mover; ' + rooms.get('perps', {}).get('reason', ''))
    matched = overlap(item.get('title') or '', market_rows)
    if matched:
        got = rooms.get('polymarket', dict(score=0.0, reason=''))
        rooms['polymarket'] = dict(score=min(100.0, got['score'] + 50.0 + 10 * len(matched)), reason=(got['reason'] + '; ' if got['reason'] else '') + '%d open market(s) share its words' % len(matched))
    meme = meme_proposal(item.get('title') or '', item.get('source'))
    if meme:
        rooms['memes'] = dict(score=meme['score'], reason=meme['why'])
    rooms = {r: v for r, v in rooms.items() if v['score'] >= MIN_ROUTE}
    out = dict(rooms=rooms or {'other': dict(score=10.0, reason='no rule matched')}, meme=meme if 'memes' in rooms else None, markets=matched, places=places(text), hook_error=None)
    if hook:
        try:
            mod, _, fn = hook.partition(':')
            replaced = getattr(importlib.import_module(mod), fn)(dict(item), json.loads(json.dumps(out)))
            if isinstance(replaced, dict) and isinstance(replaced.get('rooms'), dict) and all(r in ROOMS for r in replaced['rooms']):
                out = {**out, **replaced, 'hook_error': None}
        except Exception as exc:
            out['hook_error'] = '%s: %s' % (type(exc).__name__, str(exc)[:120])
    return out


def store(feed, news_id, item, result, now, classifier='rules'):
    """Write the routes and places of one classified story. Idempotent per (news_id, room, classifier)."""
    meme = result.get('meme') or {}
    for room, v in result['rooms'].items():
        feed.execute('INSERT OR IGNORE INTO mk_routes(news_id,room,score,reason,classifier,markets_json,meme_name,meme_ticker,routed_at) VALUES(?,?,?,?,?,?,?,?,?)',
                     (news_id, room, v['score'], v['reason'] + (' | hook ' + result['hook_error'] if result.get('hook_error') else ''), classifier,
                      json.dumps(result.get('markets') or []) if room == 'polymarket' else None, meme.get('name') if room == 'memes' else None, meme.get('ticker') if room == 'memes' else None, now))
    for code, name, lat, lon in result.get('places') or ():
        feed.execute('INSERT OR IGNORE INTO mk_story_places VALUES(?,?,?,?,?,?,?)', (news_id, code, name, lat, lon, classifier, now))
