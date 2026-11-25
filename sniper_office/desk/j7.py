"""J7 listener for the pro-trader copy lane. A host process, never a seat:

    python3 -m sniper_office.desk.j7 --config j7.json [--once --duration 120 --no-refresh]

J7Tracker relays what fomo users it tracks do, over a Socket.IO feed (wallet-tracker service). Payloads (recorded from the live relay by the source project, 2026-09):
  fomo_event kind=trade  {id, side buy|sell, userId, userHandle, usdAmount, price, marketCap, timestamp, token{address, networkId, network, symbol}}   received_at = J7's clock, ms
  fomo_event kind=thesis {id, userId, userHandle, thesis (text), holdingUsd, pnlUsd, pnlPercent, timestamp, token{...}}
  pump_event kind=trade  {author{wallet,username}, token{address,networkId}, side, tx, usdAmount, priceUsd, marketCapUsd, timestamp}   (Solana only)
  tracked_fomo / tracked_pump: the roster J7 tracks for the account (arrives once per connect)
J7 does not relay the native amount or, for Solana, a tx signature: token_qty is usd / price, native_amount stays NULL, tx_ref is J7's event id (an EVM id embeds the tx hash, which is parsed out).

An event is accepted when its trader is on our roster (config traders_file), its chain is known, it is fresh (`max_age_s`) and big enough (`min_usd` per chain, per-trader override; thesis posts
need a roster trader and text). An accepted signal is stored once (signal_id is J7's stable event id), and a buy or a thesis also becomes a PASS ticket in the same db (table desk_tickets, the
schema paper.py reads) so the copy seat's sheet_read / paper_order work on it; a sell is stored for the record and for its trader's history, with no ticket and no wake.
Token refresh is the source project's: the access JWT is renewed 1 h before expiry through J7's refresh cookie pair, the rotated pair written back to the env file atomically under a lock.
No fomo credential, no key, no wallet: read-only listening."""
import argparse
import base64
import fcntl
import hashlib
import http.cookies
import json
import logging
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import chains as CH, feed as F, ws as WS

log = logging.getLogger('desk.j7')
HOST, PATH, REFRESH_URL = 'https://nj.j7tracker.io', '/wallets/socket.io/', 'https://core.j7tracker.io/api/auth/refresh'
HEADERS = {'Origin': 'https://j7tracker.io', 'Referer': 'https://j7tracker.io/',
           'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'}   # J7's edge refuses clients that do not look like its site
DEFAULTS = dict(max_age_s=300., watch_after_signal_s=600., thesis_min_holding_usd=0., min_mcap_ratio=0., accept_unlisted=False, refresh=True, backoff_s=[5, 10, 30, 60, 120])
SCHEMA = '''
CREATE TABLE IF NOT EXISTS j7_signals(signal_id TEXT PRIMARY KEY, kind TEXT NOT NULL, trader_id TEXT NOT NULL, handle TEXT, chain TEXT NOT NULL, network_id TEXT, token TEXT NOT NULL, symbol TEXT,
  side TEXT NOT NULL, usd REAL, price_usd REAL, token_qty REAL, native_amount REAL, mcap_usd REAL, holding_usd REAL, pnl_usd REAL, pnl_pct REAL, text TEXT, tx_ref TEXT, trade_at REAL, j7_at REAL,
  recv_at REAL NOT NULL, account INTEGER, ticket_id TEXT, watch_until REAL, outcome_json TEXT);
CREATE INDEX IF NOT EXISTS j7_signals_recv ON j7_signals(recv_at);
CREATE INDEX IF NOT EXISTS j7_signals_trader ON j7_signals(trader_id, recv_at);
CREATE INDEX IF NOT EXISTS j7_signals_coin ON j7_signals(chain, token, recv_at);
''' + F.SCHEMA


def open_db(path):
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript(SCHEMA)
    return conn


# ---- credentials --------------------------------------------------------------------------------------------------------------------------------------------------------

def load_env(path):
    env = {}
    try:
        for line in Path(path).read_text().splitlines():
            key, sep, val = line.strip().partition('=')
            if sep and key.strip().replace('_', '').isalnum():
                env[key.strip()] = val.strip().strip('\'"')
    except OSError:
        pass
    return env


def claims(token):
    """The JWT payload (unverified: a deadline is read, nothing is authenticated), or None. Never logs or returns the token."""
    try:
        body = str(token).split('.')[1]
        doc = json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))
        return doc if isinstance(doc, dict) else None
    except (IndexError, ValueError):
        return None


def token_exp(token):
    exp = (claims(token) or {}).get('exp')
    return float(exp) if isinstance(exp, (int, float)) and math.isfinite(exp) and exp > 0 else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _post_refresh(access, refresh):
    """(new access, rotated refresh) from the Set-Cookie pair; raises on any malformed answer."""
    req = urllib.request.Request(REFRESH_URL, b'{}', dict(HEADERS, **{'Content-Type': 'application/json', 'X-J7-Auth': '1', 'Cookie': '__Host-j7_rt=%s; __Secure-j7_at=%s' % (refresh, access)}), method='POST')
    with urllib.request.build_opener(_NoRedirect()).open(req, timeout=10) as r:
        got = {}
        for field in r.headers.get_all('Set-Cookie', []):
            jar = http.cookies.SimpleCookie()
            jar.load(field)
            for name, c in jar.items():
                if name in ('__Host-j7_rt', '__Secure-j7_at') and c['secure'] and c['httponly'] and name not in got:
                    got[name] = c.value
    return got['__Secure-j7_at'], got['__Host-j7_rt']


def refresh_token(env_path, key='J7_TOKEN', now=None, force=False, post=_post_refresh):
    """Renew one account's access token if it expires within 1 h. Returns (status, new_token_or_None); status is not_due, refreshed, no_refresh_token, reauth_required, backoff or conflict.
    The rotated cookie pair replaces the old one in the env file atomically under a lock, and only if the file still holds the pair we started from."""
    now = time.time() if now is None else now
    rkey = 'J7_REFRESH_TOKEN' + ('_2' if key.endswith('_2') else '')
    lock = os.open(str(env_path) + '.lock', os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        text = Path(env_path).read_text()
        env = load_env(env_path)
        access, refresh = env.get(key), env.get(rkey)
        exp = token_exp(access)
        if not refresh:
            return 'no_refresh_token', None
        if exp is None or (not force and exp - now > 3600):
            return 'not_due', None
        try:
            new_access, new_refresh = post(access, refresh)
            old_c, new_c = claims(access) or {}, claims(new_access) or {}
            if new_refresh == refresh or new_c.get('userId') != old_c.get('userId') or (token_exp(new_access) or 0) <= max(now, exp):
                return 'backoff', None
        except urllib.error.HTTPError as exc:
            return ('reauth_required' if exc.code in (401, 403) else 'backoff'), None
        except (OSError, ValueError, KeyError, urllib.error.URLError):
            return 'backoff', None
        if Path(env_path).read_text() != text:
            return 'conflict', None
        out, seen = [], set()
        for line in text.splitlines(keepends=True):
            k = line.strip().partition('=')[0].strip()
            if k in (key, rkey):
                line, seen = '%s=%s\n' % (k, new_access if k == key else new_refresh), seen | {k}
            out.append(line)
        fd, tmp = tempfile.mkstemp(prefix='.j7.env.', dir=str(Path(env_path).parent))
        with os.fdopen(fd, 'w') as fh:
            os.fchmod(fh.fileno(), 0o600)
            fh.write(''.join(out))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, env_path)
        return 'refreshed', new_access
    finally:
        os.close(lock)


# ---- events -> signals --------------------------------------------------------------------------------------------------------------------------------------------------

def _num(v):
    f = F.num(v)
    return f


def _iso(text):
    try:
        return datetime.fromisoformat(str(text).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def normalize(name, evt, recv_at):
    """One J7 event -> a signal dict, or (None, reason). Nothing is guessed: a missing size or price stays None."""
    if not isinstance(evt, dict) or not isinstance(evt.get('data'), dict):
        return None, 'not_an_event'
    d, kind = evt['data'], evt.get('kind')
    tok = d.get('token') if isinstance(d.get('token'), dict) else {}
    if name == 'pump_event':
        if kind != 'trade' or not isinstance(d.get('author'), dict):
            return None, 'not_a_signal'
        wallet, tx = d['author'].get('wallet'), d.get('tx')
        side, token = str(d.get('side') or '').lower(), tok.get('address')
        sid = 'pump:%s:%s:%s:%s' % (tx, wallet, side, token)
        trader, handle, chain = 'pump:%s' % wallet, d['author'].get('username') or d['author'].get('displayName') or wallet, 'solana'
        usd, price, mcap, tx_ref = _num(d.get('usdAmount')), _num(d.get('priceUsd')), _num(d.get('marketCapUsd')), tx
    elif name == 'fomo_event' and kind in ('trade', 'thesis'):
        sid, trader, handle = str(d.get('id') or ''), str(d.get('userId') or ''), d.get('userHandle') or ''
        side, token = ('thesis' if kind == 'thesis' else str(d.get('side') or '').lower()), tok.get('address')
        chain = CH.chain_of(tok.get('networkId'), tok.get('network'))
        usd, price, mcap = _num(d.get('usdAmount')), _num(d.get('price')), _num(d.get('marketCap'))
        parts = sid.split(':')
        tx_ref = parts[2] if len(parts) == 5 and parts[0] == 'swap' else sid
    else:
        return None, 'not_a_signal'
    if not sid or not trader or side not in ('buy', 'sell', 'thesis') or not CH.valid_address(token):
        return None, 'malformed'
    if chain is None:
        return None, 'chain_unknown'
    trade_at = _iso(d.get('timestamp'))
    j7_ms = _num(evt.get('received_at'))
    sig = dict(signal_id=sid, kind='thesis' if side == 'thesis' else 'trade', trader_id=trader, handle=str(handle)[:64], chain=chain, network_id=str(tok.get('networkId') or '') or None, token=CH_norm(token),
               symbol=''.join(c for c in str(tok.get('symbol') or '') if c.isalnum() or c in '._$-')[:16] or None, side=side, usd=usd, price_usd=price,
               token_qty=usd / price if usd and price and price > 0 else None, native_amount=None, mcap_usd=mcap, holding_usd=None, pnl_usd=None, pnl_pct=None, text=None, tx_ref=tx_ref,
               trade_at=trade_at, j7_at=j7_ms / 1000. if j7_ms else None, recv_at=recv_at)
    if side == 'thesis':
        sig.update(usd=None, holding_usd=_num(d.get('holdingUsd')), pnl_usd=_num(d.get('pnlUsd')), pnl_pct=_num(d.get('pnlPercent')), text=str(d.get('thesis') or '')[:2000])
    return sig, None


def CH_norm(a):
    return a.lower() if isinstance(a, str) and a[:2].lower() == '0x' else a


class Roster:
    """Who we follow: the traders_file list ({id, handle, tier, min_usd, chains}), matched by J7 user id or by case-insensitive handle."""
    def __init__(self, path=None, traders=None):
        doc = json.loads(Path(path).read_text()) if path else dict(traders=traders or [])
        self.traders = [t for t in doc.get('traders', []) if isinstance(t, dict)]
        self.by_id = {str(t['id']): t for t in self.traders if t.get('id')}
        # A handle is display text anyone can choose: only a trader listed without an id may be matched by it, never one whose id carries a tier or overrides
        self.by_handle = {str(t['handle']).lower(): t for t in self.traders if t.get('handle') and not t.get('id')}

    def find(self, sig):
        return self.by_id.get(sig['trader_id']) or self.by_handle.get((sig['handle'] or '').lower())


def decide(sig, cfg, roster, now):
    """(accepted, reason, trader). The filter: roster trader, followed chain, fresh, big enough."""
    trader = roster.find(sig)
    if trader is None and not cfg.get('accept_unlisted'):
        return False, 'not_selected', None
    chains = (trader or {}).get('chains')
    if chains and sig['chain'] not in chains:
        return False, 'chain_not_followed', trader
    at = sig['trade_at'] or sig['j7_at'] or sig['recv_at']
    if now - at > cfg['max_age_s']:
        return False, 'stale', trader
    if sig['kind'] == 'thesis':
        if not sig['text']:
            return False, 'thesis_empty', trader
        if (sig['holding_usd'] or 0.) < cfg['thesis_min_holding_usd']:
            return False, 'thesis_small', trader
        return True, 'ok', trader
    if not sig['usd'] or sig['usd'] < CH.min_usd(cfg, sig['chain'], trader):
        return False, 'below_size', trader
    if cfg['min_mcap_ratio'] and sig['mcap_usd'] and sig['usd'] < cfg['min_mcap_ratio'] * sig['mcap_usd']:
        return False, 'below_mcap_ratio', trader
    return True, 'ok', trader


def store(db, sig, cfg, trader=None):
    """Write an accepted signal once, with its ticket (buy or thesis). True when it was new. One transaction: a restart or a replay never writes twice."""
    ticketed = sig['side'] in ('buy', 'thesis')
    ticket = 'j7:' + hashlib.sha256(('%s:%s' % (sig['chain'], sig['signal_id'])).encode()).hexdigest()[:24] if ticketed else None
    until = sig['recv_at'] + cfg['watch_after_signal_s']
    db.execute('BEGIN IMMEDIATE')
    try:
        cols = [c for c in sig]
        cur = db.execute('INSERT OR IGNORE INTO j7_signals(%s,ticket_id,watch_until) VALUES(%s,?,?)' % (','.join(cols), ','.join('?' * len(cols))), [sig[c] for c in cols] + [ticket, until])
        if cur.rowcount == 1 and ticketed:
            ev = dict(network_id=sig['chain'], mint=sig['token'], pool=None, known_at=datetime.fromtimestamp(sig['recv_at'], timezone.utc).isoformat(), checks={}, market=None,
                      signal={k: sig[k] for k in ('signal_id', 'kind', 'trader_id', 'handle', 'side', 'usd', 'price_usd', 'mcap_usd', 'holding_usd', 'symbol', 'tx_ref', 'trade_at', 'recv_at')},
                      trader=dict(tier=(trader or {}).get('tier')), note='J7 relay of a followed trader; price and size are the relay\'s claim, not a fill. No market snapshot is stored with the ticket: read the live sheet.')
            db.execute("INSERT OR IGNORE INTO desk_tickets(ticket_id,network_id,mint,pool,stage,verdict,reason,ts,inputs_json) VALUES(?,?,?,NULL,'hard_filter','PASS','j7_signal',?,?)",
                       (ticket, sig['chain'], sig['token'], datetime.fromtimestamp(sig['recv_at'], timezone.utc).isoformat(), json.dumps(dict(evidence=ev), default=str, sort_keys=True)))
        db.execute('COMMIT')
        return cur.rowcount == 1
    except sqlite3.Error:
        db.execute('ROLLBACK')
        raise


class Listener:
    """Routes events to the filter and the store. `handle(account, name, payload)` is the whole decision path, so tests drive it without a socket."""
    def __init__(self, cfg, db=None, clock=time.time):
        self.cfg = dict(DEFAULTS, **cfg)
        self.clock = clock
        self.db = db or open_db(cfg['j7_db'])
        self.roster = Roster(cfg.get('traders_file'), cfg.get('traders'))
        self.counts, self.lock, self.tracked, self.example = {}, threading.Lock(), {}, None

    def count(self, key):
        with self.lock:
            self.counts[key] = self.counts.get(key, 0) + 1

    def handle(self, account, name, payload):
        recv = self.clock()
        if name in ('tracked_fomo', 'tracked_pump'):
            users = (payload or {}).get('fomo_users' if name == 'tracked_fomo' else 'pump_users') if isinstance(payload, dict) else None
            self.tracked[(account, name)] = len(users) if isinstance(users, list) else None
            return 'tracked'
        if name not in ('fomo_event', 'pump_event'):
            return 'ignored'
        sig, why = normalize(name, payload, recv)
        if sig is None:
            self.count('drop:' + why)
            return why
        sig['account'] = account
        ok, why, trader = decide(sig, self.cfg, self.roster, recv)
        if not ok:
            self.count('reject:' + why)
            return why
        with self.lock:   # one writer connection shared by the account threads
            new = store(self.db, sig, self.cfg, trader)
        self.count('accepted' if new else 'duplicate')
        if new and self.example is None:
            self.example = {k: sig[k] for k in ('kind', 'chain', 'side', 'usd', 'symbol', 'trade_at', 'recv_at', 'j7_at') if k in sig}
        return 'accepted' if new else 'duplicate'

    # one account's connection loop
    def run_account(self, index, env_path, key, stop, status):
        sleeps, n = self.cfg['backoff_s'], 0
        while not stop.is_set():
            env = load_env(env_path)
            token = env.get(key)
            if not token:
                status[index] = 'no_token'
                return
            if self.cfg['refresh']:
                st, new = refresh_token(env_path, key)
                status[index + 10] = st
                token = new or token
            exp = token_exp(token)
            if exp is not None and exp <= self.clock():
                status[index] = 'token_expired'
                stop.wait(60)
                continue
            sio = WS.Sio(HOST, PATH, HEADERS, lambda t=token: {'token': t})
            try:
                for name, payload in sio.events():
                    if stop.is_set():
                        break
                    if name == 'connect':
                        status[index], n = 'connected', 0
                    elif name == 'connect_error':
                        status[index] = 'auth_error' if str(payload).strip().lower() == 'invalid token' else 'connect_error'
                    else:
                        self.handle(index, name, payload)
                    if exp is not None and self.cfg['refresh'] and self.clock() > exp - 3600:
                        break   # reconnect so refresh_token runs and the new token is used
            except WS.WsError as exc:
                status[index] = 'disconnected:' + str(exc)[:40]
            except Exception:
                log.exception('account %s loop failed', index)
            if status.get(index) in ('auth_error', 'token_expired') and self.cfg['refresh']:
                refresh_token(env_path, key, force=True)
            stop.wait(sleeps[min(n, len(sleeps) - 1)])
            n += 1


def accounts(env_path):
    env = load_env(env_path)
    keys = [k for k in ('J7_TOKEN', 'J7_TOKEN_2') if env.get(k)]
    return [k for i, k in enumerate(keys) if env[k] not in [env[j] for j in keys[:i]]]


def run(cfg, once=False, duration=120., no_refresh=False):
    cfg = dict(cfg, **({'refresh': False} if no_refresh else {}))
    lis = Listener(cfg)
    stop, status, threads = threading.Event(), {}, []
    for i, key in enumerate(accounts(cfg['j7_env'])):
        t = threading.Thread(target=lis.run_account, args=(i, cfg['j7_env'], key, stop, status), daemon=True)
        t.start()
        threads.append(t)
    if not threads:
        log.error('no J7_TOKEN in the env file')
        return 2
    end = time.time() + duration
    last = 0.
    try:
        while not (once and time.time() >= end):
            time.sleep(1.)
            if time.time() - last >= 60:
                last = time.time()
                log.info('status=%s counts=%s', {k: v for k, v in status.items() if k < 10}, dict(lis.counts))
    except KeyboardInterrupt:
        pass
    stop.set()
    if once:
        print(json.dumps(dict(status={k: v for k, v in status.items()}, counts=lis.counts, tracked={'%s:%s' % k: v for k, v in lis.tracked.items()}, example=lis.example), default=str, sort_keys=True))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--once', action='store_true', help='test mode: listen for --duration seconds, print a summary, exit')
    parser.add_argument('--duration', type=float, default=120.)
    parser.add_argument('--no-refresh', action='store_true', help='never rotate the refresh cookie (rotation would invalidate other holders of the same session)')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    cfg = json.loads(args.config.read_text())
    Path(cfg['j7_db']).parent.mkdir(parents=True, exist_ok=True)
    with open(str(cfg['j7_db']) + '.j7.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)   # one listener per db
        return run(cfg, args.once, args.duration, args.no_refresh)


if __name__ == '__main__':
    raise SystemExit(main())
