"""The floor's server side, loaded by `ui.py` from the floor folder (trusted code, like rooms.json and sources.json): every markets room in one read, the J7 copy lane's live view, services (guard
file, systemd units, creator collector), token names, and the two token-gated writes. Everything is read only except `halt_book` and `say` (the markets plug-in's own functions on a read-write connection)
and the names cache table (this process's own file). `databases()` names the project databases `sources.json` may query; `routes()` returns the extra GET and POST routes."""
import json
import math
from pathlib import Path
import re
import sqlite3
import time

from sniper_office import views as V

DEFAULT_DBS = dict(j7='j7.db', markets='markets.db', markets_ledger='markets_ledger.db', wallet='wallet_network.db', narrative='narrative.db', x='x.db')


# ---- the floor's newer rooms: markets plug-in, J7 copy lane, services, token names (read only; the one write is the names cache)

def _unknown(reason):
    return dict(state='unknown', reason=reason)


def markets(markets_db, ledger_db, *, now, cfg=None):
    """Every markets room payload in one read (sniper_office.markets.api, read-only connections): {state, as_of, rooms: {room: payload or {state: unknown, reason}}}. A missing database or table is unknown, never an empty success."""
    from sniper_office.markets import api as MA, books as MB, common as MC
    if not (markets_db and ledger_db and Path(markets_db).is_file() and Path(ledger_db).is_file()):
        return dict(state='unknown', reason='markets_db_missing', as_of=now, rooms={})
    pol = MB.policy(cfg)
    rooms = {}
    try:
        feed, led = MC.ro(markets_db), MC.ro(ledger_db)
    except sqlite3.Error:
        return dict(state='unknown', reason='markets_db_unreadable', as_of=now, rooms={})
    try:
        for name, call in (('newsroom', lambda: MA.newsroom(feed, led, now, pol)), ('situation', lambda: MA.situation(feed, now)), ('strategies', lambda: MA.strategy_lab(led)),
                           ('paper_labs', lambda: MA.paper_labs(feed, led, now, pol)), ('sports', lambda: MA.sports(feed, now)), ('explorer', lambda: MA.explorer(feed, led, now)),
                           ('memes', lambda: MA.meme_candidates(feed, now)), ('coach', lambda: MA.coach(led)), ('lab', lambda: MA.video_lab(led)), ('control', lambda: MA.control(feed, led, now, pol))):
            try:
                rooms[name] = call()
            except (sqlite3.Error, KeyError, TypeError, ValueError) as exc:
                rooms[name] = _unknown('%s:%s' % (type(exc).__name__, str(exc)[:80]))
    finally:
        feed.close();led.close()
    return dict(state='known', as_of=now, rooms=rooms)


def halt_book(ledger_db, book_id, halted, reason, *, now):
    """The token-gated write: stop or resume new entries in one markets book (the plug-in's own book_halt on a read-write connection)."""
    from sniper_office.markets import api as MA, common as MC
    if not ledger_db or not Path(ledger_db).is_file():
        return dict(state='refused', reason='ledger_missing')
    led = MC.connect(ledger_db, MC.LEDGER_SCHEMA)
    try:
        return MA.book_halt(led, book_id, bool(halted), 'operator', now, str(reason or '')[:200])
    finally:
        led.close()


def say(ledger_db, to_role, text, *, now):
    from sniper_office.markets import api as MA, common as MC
    if not ledger_db or not Path(ledger_db).is_file():
        return dict(state='refused', reason='ledger_missing')
    led = MC.connect(ledger_db, MC.LEDGER_SCHEMA)
    try:
        return MA.operator_say(led, 'operator', str(to_role or '')[:40] or None, text, now)
    finally:
        led.close()


LIVE_WINDOW_S = 900
LIVE_COINS = 14
LIVE_ROWS = 4000


def _median(values):
    values = sorted(values)
    return values[len(values) // 2] if values else None


def live(office_db, j7_db, ledgers=(), *, now, since=None):
    """The copy lane's live view, polled about once a second: the J7 book rows and open positions (desk_live_pnl, one row per order and a book row with order_id ''), the signals of the last hour, and
    the 1 s ticks of every coin shown, newer than `since` (default the last 15 minutes). tp/sl come from the order's frozen policy and are on the net exit value, so the lines are drawn from the entry price as an approximation.
    A missing table is unknown for that part, never zero."""
    full = not (V._finite(since) and since > now - 3600)
    since = now - LIVE_WINDOW_S if full else since
    out = dict(as_of=now, since=since, books=None, positions=None, signals=None, ticks={}, resolution=None, state='unknown')
    try:
        with V._ro(office_db) as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute('SELECT * FROM desk_live_pnl ORDER BY book_id,order_id LIMIT 200').fetchall()] if _has(conn, 'desk_live_pnl') else None
    except sqlite3.Error:
        rows = None
    if rows is not None:
        for r in rows:
            r.pop('state', None) if r.get('order_id') == '' else None
        out['books'] = [r for r in rows if r['order_id'] == '']
        out['positions'] = [r for r in rows if r['order_id'] != '']
        orders = {}
        for path in ledgers:
            try:
                with V._ro(path) as conn:
                    for o in conn.execute('SELECT order_id,entry_price_usd,filled_at,qty,spend_usd,policy_json FROM orders WHERE state=\'open\''):
                        orders[o[0]] = o
            except sqlite3.Error:
                continue
        for p in out['positions']:
            o = orders.get(p['order_id'])
            if o:
                try:
                    pol = json.loads(o[5] or '{}')
                except ValueError:
                    pol = {}
                p.update(entry_price_usd=o[1], filled_at=o[2], sl_pct=pol.get('sl_pct'), tp_pct=pol.get('tp_pct'), trail_pct=pol.get('trail_pct'))
    coins = []
    for p in out['positions'] or []:
        if p.get('chain') and p.get('mint'):
            coins.append((p['chain'], p['mint']))
    try:
        with V._ro(j7_db) as conn:
            conn.row_factory = sqlite3.Row
            sig = [dict(r) for r in conn.execute("SELECT signal_id,kind,trader_id,handle,chain,network_id,token,symbol,side,usd,price_usd,mcap_usd,recv_at,trade_at,ticket_id,outcome_json FROM j7_signals WHERE recv_at>? ORDER BY recv_at DESC LIMIT 60", (now - 3600,)).fetchall()] if _has(conn, 'j7_signals') else None
            out['signals'] = sig
            for s in sig or []:
                if s['side'] == 'buy' and (s['chain'], s['token']) not in coins:
                    coins.append((s['chain'], s['token']))
            coins = coins[:LIVE_COINS]
            have_ticks = _has(conn, 'ticks')
            budget = LIVE_ROWS
            first_signal = {}
            for s in sig or []:
                first_signal[(s['chain'], s['token'])] = min(s['recv_at'], first_signal.get((s['chain'], s['token']), s['recv_at']))
            for chain, token in coins if have_ticks else ():
                # a full read reaches back to a coin's earliest signal this hour (the recorder watches a coin only for a while after it), so its chart has the price since the signal
                start = min(since, first_signal.get((chain, token), since) - 60) if full else since
                rs = conn.execute('SELECT at,price_usd,changed FROM ticks WHERE chain=? AND token=? AND at>? ORDER BY at DESC LIMIT ?', (chain, token, start, min(budget, 1200))).fetchall()[::-1]
                budget -= len(rs)
                out['ticks'][chain + ':' + token] = [[r[0], r[1], r[2]] for r in rs]
            if have_ticks:
                gaps, moves = [], []
                for chain, token in coins[:3]:
                    rs = conn.execute('SELECT at,changed FROM ticks WHERE chain=? AND token=? AND at>? ORDER BY at', (chain, token, now - 300)).fetchall()
                    gaps += [b[0] - a[0] for a, b in zip(rs, rs[1:])]
                    ch = [r[0] for r in rs if r[1]]
                    moves += [b - a for a, b in zip(ch, ch[1:])]
                out['resolution'] = dict(poll_gap_median_s=_median(gaps), price_gap_median_s=_median(moves), coins=len(coins[:3]), window_s=300)
            out['ticks_last_at'] = conn.execute('SELECT max(at) FROM ticks').fetchone()[0] if have_ticks else None
    except sqlite3.Error:
        pass
    for s in out['signals'] or []:
        s['outcome'] = None
        try:
            s['outcome'] = json.loads(s.pop('outcome_json') or 'null')
        except ValueError:
            pass
    out['state'] = 'known' if out['books'] is not None or out['signals'] is not None else 'unknown'
    return out


def _has(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


GUARD_MAX_AGE_S = 60


def guard_state(path, *, now):
    """The host watchdog's guard file as the launcher reads it: ready, paused with the reason, or stale/unreadable (which the launcher treats as not ready)."""
    if not path:
        return _unknown('no_guard_file_given')
    try:
        doc = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return dict(state='unreadable', reason='guard_file_missing_or_malformed')
    if not isinstance(doc, dict):
        return dict(state='unreadable', reason='guard_file_malformed')
    at = doc.get('at')
    age = now - at if V._finite(at) else None
    fresh = age is not None and -5 <= age <= GUARD_MAX_AGE_S
    return dict(state=doc.get('state') if fresh and doc.get('state') in ('ready', 'paused') else 'stale', reason=V._title(str(doc.get('reason') or ''), 120) or None, at=at if V._finite(at) else None, age_s=age,
                latched=doc.get('latched') is True, policy=V._title(str(doc.get('guard_policy_version') or ''), 40) or None)


UNITS = ('office-j7-listener', 'office-j7-ticks', 'office-paper-filler', 'office-desk-feed', 'office-narrative', 'office-xcap', 'office-host-watchdog', 'office-creator-collect', 'office-creator-venue',
         'office-crew-host', 'office-crew-bridge', 'office-markets-intel-hl', 'office-ui', 'office-clock') + tuple('office-markets-feed@%s' % n for n in ('polymarket', 'perps', 'stocks', 'news', 'sports', 'books'))
TIMED = ('office-clock',)          # oneshot units run by a timer of the same name: judged by the timer, never by the service being inactive
CLOCK_MAX_AGE_S = 600


def unit_states(units=UNITS, runner=None):
    """systemd user-unit state for a fixed allowlist of names ({unit: {active, sub, enabled, type}}; timers as 'name.timer'), or None when systemctl cannot answer. Runs `systemctl --user show` only."""
    import subprocess
    run = runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=3).stdout)
    out = {}
    names = [u + '.service' for u in units] + [u + '.timer' for u in units if u in TIMED]
    try:
        text = run(['systemctl', '--user', 'show', '-p', 'Id,ActiveState,SubState,LoadState,UnitFileState,Type', '--', *names])
    except (OSError, subprocess.SubprocessError):
        return None
    for block in text.split('\n\n'):
        f = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
        ident = f.get('Id', '')
        if ident.endswith(('.service', '.timer')) and f.get('LoadState') != 'not-found':
            out[ident.rsplit('.', 1)[0] + ('.timer' if ident.endswith('.timer') else '')] = dict(active=f.get('ActiveState'), sub=f.get('SubState'), enabled=f.get('UnitFileState'), type=f.get('Type'))
    return out


def judge_units(states, clock_age):
    """What the Control room lists: only units that are enabled, each with ok true or false and a note. A timed oneshot is judged by its timer (and, for the clock, the age of the clock file); a disabled unit is not listed, only counted."""
    if states is None:
        return None, None
    shown, hidden = {}, 0
    for name, v in sorted(states.items()):
        if name.endswith('.timer'):
            continue
        timer = states.get(name + '.timer')
        if v['type'] == 'oneshot' and timer:
            ok = timer['active'] == 'active' and (name != 'office-clock' or (clock_age is not None and 0 <= clock_age < CLOCK_MAX_AGE_S))
            shown[name] = dict(active='timer ' + str(timer['active']), sub=timer['sub'], ok=ok, note=None if name != 'office-clock' else ('clock file %s s old' % round(clock_age)) if clock_age is not None else 'clock file unreadable')
        elif v['enabled'] in ('enabled', 'enabled-runtime'):
            shown[name] = dict(active=v['active'], sub=v['sub'], ok=v['active'] == 'active', note=None)
        else:
            hidden += 1
    return shown, hidden


def creator_state(directory):
    """The BSC creator collector's checkpoint.json: status, calls, launches seen (token and transaction ids only). Unknown when there is none."""
    if not directory:
        return _unknown('no_creator_dir_given')
    doc = None
    for name in ('collect/checkpoint.json', 'checkpoint.json'):      # the collector's state_dir is a subfolder of the creator dir in the deployed layout
        try:
            raw = Path(directory, name).read_text()
            doc = json.loads(raw) if len(raw) < 2_000_000 else None
            break
        except (OSError, ValueError):
            continue
    else:
        return _unknown('no_checkpoint')
    if not isinstance(doc, dict):
        return _unknown('checkpoint_malformed')
    launches = [dict(token=str(x.get('token'))[:66], tx_hash=str(x.get('tx_hash'))[:80]) for x in doc.get('launches') or [] if isinstance(x, dict)][:20]
    return dict(state='known', status=doc.get('status'), reason=doc.get('reason'), calls=doc.get('calls'), last_error=V._title(str(doc.get('last_error') or ''), 120) or None, started_at=doc.get('started_at_utc'), launches=launches,
                chain='bsc', venue_discoveries=len(list(Path(directory, 'venue').glob('discovery-*.json'))[:5000]))


def services(office_db, j7_db, *, now, guard_file=None, creator_dir=None, runner=None, clock_file=None):
    """Control room extras: guard state, unit states, J7 listener liveness (newest signal; the listener keeps no status table), the tick recorder's newest tick, the crew bridge cursor, the creator collector."""
    try:
        clock_at = json.loads(Path(clock_file).read_text()).get('at') if clock_file else None
    except (OSError, ValueError, AttributeError):
        clock_at = None
    units, hidden = judge_units(unit_states(runner=runner), now - clock_at if V._finite(clock_at) else None)
    out = dict(as_of=now, guard=guard_state(guard_file, now=now), units=units, units_not_enabled=hidden, creator=creator_state(creator_dir), j7=_unknown('j7_db_unreadable'), ticks=_unknown('no_ticks_table'), crew=_unknown('no_crew_cursor'))
    try:
        with V._ro(j7_db) as conn:
            if _has(conn, 'j7_signals'):
                n, last, last1h = conn.execute('SELECT count(*),max(recv_at),sum(recv_at>?) FROM j7_signals', (now - 3600,)).fetchone()
                out['j7'] = dict(state='known', signals_total=n, signals_1h=last1h or 0, last_recv_at=last, note='the listener keeps no status table: liveness is the newest stored signal')
            if _has(conn, 'ticks'):
                last, n60 = conn.execute('SELECT max(at),count(DISTINCT chain||token) FROM ticks WHERE at>?', (now - 60,)).fetchone()
                out['ticks'] = dict(state='known', last_at=last, coins_60s=n60)
    except sqlite3.Error:
        pass
    try:
        with V._ro(office_db) as conn:
            if _has(conn, 'crew_bridge_cursor'):
                out['crew'] = dict(state='known', cursors={r[0]: r[1] for r in conn.execute('SELECT name,value FROM crew_bridge_cursor LIMIT 10')})
    except sqlite3.Error:
        pass
    return out


NAME_CHARS = re.compile(r'[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069]')
CHAIN_RE = re.compile(r'[a-z0-9][a-z0-9-]{1,23}')
MINT_RE = re.compile(r'[A-Za-z0-9]{20,66}')
NAMES_DDL = 'CREATE TABLE IF NOT EXISTS token_names(chain TEXT NOT NULL, token TEXT NOT NULL, name TEXT, symbol TEXT, source TEXT NOT NULL, fetched_at REAL NOT NULL, PRIMARY KEY(chain, token))'
NAMES_RETRY_S = 900.
NAMES_GAP_S = 1.2
NAMES_URL = 'https://api.dexscreener.com/tokens/v1/%s/%s'


def clean_name(text, n=40):
    """Provider text is untrusted: control and bidi characters out, whitespace folded, length capped."""
    return ' '.join(NAME_CHARS.sub('', text).split())[:n] if isinstance(text, str) else None


def parse_ids(raw):
    """'chain:mint,chain:mint' -> [(chain, mint)], at most 60, malformed ones dropped."""
    out = []
    for part in (raw or '').split(',')[:60]:
        chain, _, mint = part.partition(':')
        if CHAIN_RE.fullmatch(chain) and MINT_RE.fullmatch(mint) and (chain, mint) not in out:
            out.append((chain, mint))
    return out


def _dexscreener(fetch, chain, token):
    """(name, symbol) from DexScreener's token endpoint, or None. https only, one host, no redirects (markets.common.http)."""
    doc = json.loads(fetch(NAMES_URL % (chain, token)))
    for pair in doc if isinstance(doc, list) else []:
        base = pair.get('baseToken') if isinstance(pair, dict) else None
        if isinstance(base, dict) and str(base.get('address', '')).lower() == token.lower() and clean_name(base.get('name')):
            return clean_name(base.get('name')), clean_name(base.get('symbol'), 16)
    return None


class TokenNames:
    """Token names for the page. Local data first (J7 signal symbols, the narrative launch table, ledger tickers), then a host-side cache table filled one DexScreener lookup at a time (at most one per NAMES_GAP_S,
    a miss is retried after NAMES_RETRY_S), never on a request thread. Names are provider text: cleaned and capped here, rendered as text by the page. Seats never read this."""

    def __init__(self, cache_db, local, fetch=None, start=True):
        import queue
        import threading
        self.cache_db, self.local, self.queue, self.pending = cache_db, local, queue.Queue(maxsize=200), set()
        self.lock = threading.Lock()
        self.fetch = fetch or (lambda url: __import__('sniper_office.markets.common', fromlist=['http']).http(url, allow={'api.dexscreener.com'}, timeout=8.))
        if cache_db:
            with sqlite3.connect(cache_db, timeout=5) as c:
                c.execute(NAMES_DDL)
        if start and cache_db:
            threading.Thread(target=self._worker, daemon=True).start()

    def _cached(self, ids, now):
        if not self.cache_db:
            return {}
        with sqlite3.connect('file:%s?mode=ro' % self.cache_db, uri=True, timeout=2) as c:
            return {(r[0], r[1]): r[2:] for r in c.execute('SELECT chain,token,name,symbol,fetched_at FROM token_names WHERE chain||\':\'||token IN (%s)' % ','.join('?' * len(ids)), [a + ':' + b for a, b in ids])} if ids else {}

    def lookup(self, ids, now):
        """{'chain:mint': {name, symbol, source}} for what is known; unknown coins are queued for the worker and come back on a later call."""
        got = self.local(ids) if self.local else {}
        cached = self._cached(ids, now)
        out = {}
        for key in ids:
            local, c = got.get(key, {}), cached.get(key)
            name, symbol = (c[0] if c else None), (c[1] if c else None) or local.get('symbol')
            name = name or local.get('name')
            if name or symbol:
                out[key[0] + ':' + key[1]] = dict(name=name, symbol=symbol, source='dexscreener' if c and c[0] else 'local')
            if not name and not (c and now - c[2] < NAMES_RETRY_S) and self.cache_db:
                with self.lock:
                    if key not in self.pending:
                        try:
                            self.queue.put_nowait(key);self.pending.add(key)
                        except Exception:
                            pass
        return out

    def step(self, key, now):
        """One lookup, recorded either way (a miss is a row with no name, so it is not retried before NAMES_RETRY_S)."""
        try:
            found = _dexscreener(self.fetch, *key)
        except (OSError, ValueError, KeyError):
            found = None
        with sqlite3.connect(self.cache_db, timeout=5) as c:
            c.execute('INSERT OR REPLACE INTO token_names VALUES(?,?,?,?,?,?)', (key[0], key[1], found[0] if found else None, found[1] if found else None, 'dexscreener', now))
        with self.lock:
            self.pending.discard(key)

    def _worker(self):
        while True:
            key = self.queue.get()
            self.step(key, time.time())
            time.sleep(NAMES_GAP_S)


def local_names(j7_db, narrative_db, office_db=None):
    """A `local` reader for TokenNames: symbols from J7 signals, names and symbols from the narrative launch table. Read only; an unreadable file contributes nothing."""
    def read(ids):
        out = {}
        for path, sql, cols in ((j7_db, 'SELECT chain,token,symbol FROM j7_signals WHERE chain=? AND token=? AND symbol IS NOT NULL AND symbol!=\'\' ORDER BY recv_at DESC LIMIT 1', 'cs'),
                                (narrative_db, 'SELECT network_id,mint,name,symbol FROM launches WHERE network_id=? AND mint=?', 'cns')):
            if not path:
                continue
            try:
                with V._ro(path) as conn:
                    for chain, token in ids:
                        r = conn.execute(sql, (chain, token)).fetchone()
                        if r:
                            d = out.setdefault((chain, token), {})
                            if cols == 'cs':
                                d['symbol'] = d.get('symbol') or clean_name(r[2], 16)
                            else:
                                d['name'], d['symbol'] = d.get('name') or clean_name(r[2]), d.get('symbol') or clean_name(r[3], 16)
            except sqlite3.Error:
                continue
        return out
    return read


def databases(cfg):
    """Every project database name -> path: the flags given (`--db NAME=PATH`, `--markets-db`, ...), else the conventional file beside the office store."""
    here = Path(cfg['office_db']).resolve().parent
    given = cfg.get('databases') or {}
    return {**{k: given.get(k) or str(here / v) for k, v in DEFAULT_DBS.items()}, **{k: v for k, v in given.items() if k not in DEFAULT_DBS}}


def _since(query):
    raw = query.get('since', [''])[0]
    return float(raw) if raw else None


def routes(cfg, dbs):
    """(gets, posts): gets maps a path to fn(query, now) -> doc; posts maps a path to (exact body keys, fn(body, now) -> doc). A ValueError from a function is a 400 to the caller."""
    ledgers = cfg.get('ledgers') or ()
    names = TokenNames(cfg.get('names_db'), local_names(dbs['j7'], dbs['narrative']), start=cfg.get('names_lookup', True)) if cfg.get('names_db') else None

    def halt(body, now):
        if not isinstance(body['book_id'], str) or type(body['halted']) is not bool or not isinstance(body['reason'], str):
            raise ValueError('halt_body')
        return halt_book(dbs['markets_ledger'], body['book_id'], body['halted'], body['reason'], now=now)

    def note(body, now):
        if not isinstance(body['to_role'], str) or not isinstance(body['text'], str):
            raise ValueError('say_body')
        return say(dbs['markets_ledger'], body['to_role'], body['text'], now=now)

    gets = {'/api/office/markets': lambda q, now: markets(dbs['markets'], dbs['markets_ledger'], now=now),
            '/api/office/live': lambda q, now: live(cfg['office_db'], dbs['j7'], ledgers, now=now, since=_since(q)),
            '/api/office/services': lambda q, now: services(cfg['office_db'], dbs['j7'], now=now, guard_file=cfg.get('guard_file'), creator_dir=cfg.get('creator_dir'), clock_file=cfg.get('clock_file')),
            '/api/office/token-names': lambda q, now: dict(names=names.lookup(parse_ids(q.get('ids', [''])[0]), now) if names else {}, lookup=names is not None)}
    return gets, {'/api/markets/halt': ({'book_id', 'halted', 'reason'}, halt), '/api/markets/say': ({'to_role', 'text'}, note)}
