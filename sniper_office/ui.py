#!/usr/bin/env python3
"""The operator's page: who is awake, who is parked and why, what each seat did, and the mail. Standard library only.

    python3 -m sniper_office.ui --office-db office.db --quota-db quota.db

Opens on http://127.0.0.1:8765. Reads are open to anyone who can reach the port, which by default is only this machine.
The two actions (write to a seat, clear a park) need a token: put at least 24 random characters in a file readable only by you
and pass --token-file. Without one the page is read-only. For another machine, put your own tunnel in front; there is no login here."""
import argparse
import hmac
import ipaddress
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import stat
import time
from urllib.parse import parse_qs, urlsplit

from . import clock as C, plugins, views as V

STATIC = Path(__file__).resolve().parent / 'ui_static'
FILES = {'/': ('index.html', 'text/html; charset=utf-8'), '/operator': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/app.css': ('app.css', 'text/css; charset=utf-8')}
TYPES = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.ttf': 'font/ttf', '.woff2': 'font/woff2', '.json': 'application/json'}


def floor_assets(floor_dir):
    """A project's animated floor: floor.html at '/', and every .html/.js/.css/.json/.ttf/.woff2 file under the directory at its own URL. Found once at start;
    a request is looked up by exact URL in this table and never joined onto a path, so there is nothing to traverse. The operator page moves to /operator."""
    root = Path(floor_dir).resolve()
    found = {'/' + p.relative_to(root).as_posix(): (p, TYPES[p.suffix]) for p in sorted(root.rglob('*')) if p.is_file() and not p.is_symlink() and p.suffix in TYPES}
    if '/floor.html' not in found:
        raise SystemExit('--floor-dir needs a floor.html')
    found['/'] = found.pop('/floor.html')
    return found


def floor_layout(floor_dir):
    """The floor's own seat-to-room table, rooms.json in its folder: {"rooms": {room: [role, ...]}, "fallback": room or null}. Absent means no seat has a room. Read once at start."""
    path = Path(floor_dir).resolve() / 'rooms.json'
    if not path.is_file():
        return V.NO_ROOMS
    doc = json.loads(path.read_text())
    rooms = doc.get('rooms') if isinstance(doc, dict) else None
    if not isinstance(rooms, dict) or not all(isinstance(r, list) and all(isinstance(x, str) for x in r) for r in rooms.values()) or doc.get('fallback') not in (None, *rooms):
        raise SystemExit('rooms.json must be {"rooms": {room: [role, ...]}, "fallback": a room or null}')
    return dict(rooms=rooms, fallback=doc.get('fallback'))


def floor_queries(floor_dir):
    """The floor's own read-only data queries, sources.json in its folder (see views.source_queries). Absent means none. Read once at start."""
    path = Path(floor_dir).resolve() / 'sources.json'
    if not path.is_file():
        return {}
    try:
        return V.source_queries(json.loads(path.read_text()))
    except ValueError as exc:
        raise SystemExit(str(exc))


MAX_BODY = 8192
HEADERS = {'Content-Security-Policy': "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'", 'X-Content-Type-Options': 'nosniff',
           'Referrer-Policy': 'no-referrer', 'Cache-Control': 'no-store', 'X-Frame-Options': 'DENY'}


def read_token(path):
    """None when no file is given. A token file others can read, or a short token, is refused rather than trusted."""
    if path is None:
        return None
    info = Path(path).stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise SystemExit('token file must be a regular file readable only by its owner (chmod 600)')
    token = Path(path).read_text().strip()
    if len(token) < 24:
        raise SystemExit('token must be at least 24 characters')
    return token


def make_handler(cfg):
    origin = 'http://%s:%d' % (cfg['host'] if ':' not in cfg['host'] else '[%s]' % cfg['host'], cfg['port'])
    hosts = {'%s:%d' % (name, cfg['port']) for name in {cfg['host'], 'localhost', '127.0.0.1'}} | {'[%s]:%d' % (cfg['host'], cfg['port'])}

    assets = floor_assets(cfg['floor_dir']) if cfg.get('floor_dir') else {}
    layout = floor_layout(cfg['floor_dir']) if cfg.get('floor_dir') else V.NO_ROOMS
    queries = floor_queries(cfg['floor_dir']) if cfg.get('floor_dir') else {}

    def now():
        return C.corrected_now(cfg['clock_file'], host_now=time.time()) if cfg.get('clock_file') else time.time()

    class Handler(BaseHTTPRequestHandler):
        server_version = 'office-ui'
        sys_version = ''

        def log_message(self, fmt, *args):                    # request lines carry seat ids; keep them out of stderr unless asked
            if cfg.get('verbose'):
                super().log_message(fmt, *args)

        def _send(self, status, body, kind='application/json; charset=utf-8'):
            raw = body if isinstance(body, bytes) else json.dumps(body, allow_nan=False, default=str).encode()
            self.send_response(status)
            for key, value in HEADERS.items():
                self.send_header(key, value)
            self.send_header('Content-Type', kind);self.send_header('Content-Length', str(len(raw)))
            self.end_headers();self.wfile.write(raw)

        def _api(self, doc):
            # Every view says whether actions are on: a page opened straight on Mail must not guess.
            self._send(200, dict(doc, writable=cfg['token'] is not None, panels=[dict(name=n, title=v[0]) for n, v in sorted(cfg['panels'].items())]))

        def _host_ok(self):
            # A page on another site can make this browser call 127.0.0.1 under its own name; the Host header then is not ours.
            return self.headers.get('Host') in hosts

        def do_GET(self):
            if not self._host_ok():
                return self._send(421, dict(error='host_refused'))
            url = urlsplit(self.path);query = parse_qs(url.query)
            if url.path == '/favicon.ico':
                return self._send(200, b'', 'image/x-icon')      # no icon, and no failed request in the console
            if url.path in assets:
                path, kind = assets[url.path]
                return self._send(200, path.read_bytes(), kind)
            if url.path in FILES:
                name, kind = FILES[url.path]
                return self._send(200, (STATIC / name).read_bytes(), kind)
            at = now()
            try:
                if url.path == '/api/overview':
                    return self._api(V.overview(cfg['office_db'], cfg.get('quota_db'), now=at))
                if url.path == '/api/seat':
                    before = query.get('before', [None])[0]
                    return self._api(V.seat(cfg['office_db'], cfg.get('quota_db'), query.get('actor', [''])[0], now=at, before=int(before) if before else None))
                if url.path == '/api/office/newsroom':       # the floor page's one read: seats, rooms, live feed, paper book
                    return self._send(200, V.newsroom(cfg['office_db'], cfg.get('quota_db'), now=at, ledgers=cfg.get('ledgers') or (), layout=layout, queries=queries))
                if url.path == '/api/office/query':           # one named, parameterised read from the floor's sources.json
                    return self._send(200, V.query(queries, query.get('name', [''])[0], {k: v[0] for k, v in query.items()}, cfg['office_db'], cfg.get('quota_db'), cfg.get('ledgers') or ()))
                if url.path == '/api/office/operator-chat':   # the floor's chat panel: seat requests and the operator's mail, read only
                    return self._send(200, V.operator_chat(cfg['office_db'], cfg.get('quota_db'), now=at, layout=layout))
                if url.path == '/api/mail':
                    return self._api(V.mail(cfg['office_db'], now=at))
                if url.path == '/api/requests':
                    return self._api(V.requests(cfg['office_db'], now=at))
                if url.path == '/api/budget':
                    return self._api(V.budget(cfg['office_db'], cfg.get('quota_db'), now=at))
                if url.path == '/api/evaluation':
                    return self._api(V.evaluation(cfg['office_db'], now=at))
                if url.path == '/api/health':
                    return self._api(V.health(cfg['office_db'], now=at, verify=query.get('verify', [''])[0] == '1'))
                if url.path == '/api/panel':
                    found = cfg['panels'].get(query.get('name', [''])[0])
                    if found is None:
                        return self._send(404, dict(error='not_found'))
                    return self._api(dict(V.panel(found[1], now=at), title=found[0], as_of_page=at))
            except ValueError:
                return self._send(400, dict(error='bad_arguments'))
            self._send(404, dict(error='not_found'))

        def do_POST(self):
            if not self._host_ok():
                return self._send(421, dict(error='host_refused'))
            if cfg['token'] is None:
                return self._send(403, dict(error='read_only'))
            if self.headers.get('Origin') != origin:
                return self._send(403, dict(error='origin_refused'))
            if not hmac.compare_digest(self.headers.get('X-Office-Token', '').encode(), cfg['token'].encode()):
                return self._send(401, dict(error='unauthorized'))
            if (self.headers.get('Content-Type') or '').split(';')[0].strip() != 'application/json':
                return self._send(415, dict(error='json_required'))
            try:
                length = int(self.headers.get('Content-Length', ''))
            except ValueError:
                return self._send(411, dict(error='length_required'))
            if not 0 < length <= MAX_BODY:
                return self._send(413, dict(error='message_too_large'))
            try:
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError
            except (ValueError, UnicodeError):
                return self._send(400, dict(error='body_invalid'))
            at = now()
            if at is None:
                return self._send(503, dict(error='clock_unknown'))
            if self.path == '/api/mail' and set(body) == {'to', 'client_id', 'text'}:
                out = V.send_operator_mail(cfg['office_db'], to_actor_id=body['to'], client_id=body['client_id'], text=body['text'], now=at)
            elif self.path == '/api/clear-park' and set(body) == {'actor', 'reason'} and cfg.get('quota_db'):
                out = V.clear_park(cfg['office_db'], cfg['quota_db'], actor_id=body['actor'], reason=body['reason'], now=at)
            else:
                return self._send(400, dict(error='body_invalid'))
            self._send(409 if out.get('state') == 'refused' else 200, out)

    return Handler


def serve(cfg):
    return ThreadingHTTPServer((cfg['host'], cfg['port']), make_handler(cfg))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--office-db', required=True);p.add_argument('--quota-db');p.add_argument('--clock-file')
    p.add_argument('--host', default='127.0.0.1');p.add_argument('--port', type=int, default=8765)
    p.add_argument('--token-file', help='enables the two write actions');p.add_argument('--verbose', action='store_true')
    p.add_argument('--ledger-db', action='append', default=[], help='a paper ledger database the floor page reads (repeatable)')
    p.add_argument('--floor-dir', help="a project's animated floor (floor.html and its files), shown at /; the operator page stays at /operator")
    p.add_argument('--plugins', default='', help='comma-separated modules whose register(host) adds panels; trusted code, as for a seat')
    args = p.parse_args(argv)
    token = read_token(args.token_file)
    try:
        loopback = ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        loopback = args.host == 'localhost'
    if not loopback and token is None:
        raise SystemExit('refusing to listen beyond this machine without --token-file; everything the seats wrote would be readable by the network')
    host = plugins.load(dict(plugins=[name for name in args.plugins.split(',') if name], office_db=args.office_db, quota_db=args.quota_db))
    cfg = dict(office_db=args.office_db, quota_db=args.quota_db, clock_file=args.clock_file, floor_dir=args.floor_dir, ledgers=args.ledger_db, host=args.host, port=args.port, token=token, verbose=args.verbose, panels=host.panels)
    server = serve(cfg)
    print('office ui on http://%s:%d (%s, %d project panels)' % (args.host, args.port, 'actions enabled' if token else 'read-only', len(host.panels)), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
