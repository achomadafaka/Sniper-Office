"""A minimal stdlib WebSocket client and Socket.IO (Engine.IO v4) event reader, enough for the J7 feed: text frames only, client frames masked, ping/pong answered, no reconnect (the caller loops).
No dependency: the source project used python-socketio; this keeps the Office stdlib-only."""
import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import urllib.parse

GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
MAX_FRAME = 4_000_000


class WsError(Exception):
    pass


class WebSocket:
    def __init__(self, url, headers=None, timeout=20.):
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ('wss', 'ws'):
            raise WsError('bad_scheme')
        port = u.port or (443 if u.scheme == 'wss' else 80)
        try:
            raw = socket.create_connection((u.hostname, port), timeout=timeout)
            self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=u.hostname) if u.scheme == 'wss' else raw
            key = base64.b64encode(os.urandom(16)).decode()
            lines = ['GET %s HTTP/1.1' % ((u.path or '/') + ('?' + u.query if u.query else '')), 'Host: %s' % u.netloc, 'Upgrade: websocket', 'Connection: Upgrade',
                     'Sec-WebSocket-Key: ' + key, 'Sec-WebSocket-Version: 13'] + ['%s: %s' % kv for kv in (headers or {}).items()]
            self.sock.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode())
            head = b''
            while b'\r\n\r\n' not in head:
                chunk = self.sock.recv(1024)
                if not chunk or len(head) > 16384:
                    raise WsError('handshake_closed')
                head += chunk
        except (OSError, ssl.SSLError) as exc:
            raise WsError('connect_failed:%s' % type(exc).__name__) from None
        head, _, self.buf = head.partition(b'\r\n\r\n')
        status = head.split(b'\r\n', 1)[0]
        want = base64.b64encode(hashlib.sha1(key.encode() + GUID).digest())
        if b' 101 ' not in status + b' ' or want not in head.replace(b' ', b''):
            raise WsError('handshake_refused:' + status.decode('latin-1')[:40])

    def settimeout(self, s):
        self.sock.settimeout(s)

    def _read(self, n):
        while len(self.buf) < n:
            try:
                chunk = self.sock.recv(65536)
            except (OSError, ssl.SSLError) as exc:
                raise WsError('read_failed:%s' % type(exc).__name__) from None
            if not chunk:
                raise WsError('closed')
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text, opcode=1):
        data = text.encode() if isinstance(text, str) else text
        n = len(data)
        head = bytes([0x80 | opcode]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack('>H', n) if n < 65536 else bytes([0x80 | 127]) + struct.pack('>Q', n))
        mask = os.urandom(4)
        try:
            self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
        except (OSError, ssl.SSLError) as exc:
            raise WsError('send_failed:%s' % type(exc).__name__) from None

    def recv(self):
        """The next text message (fragments joined); ping answered, close raises."""
        parts = []
        while True:
            b0, b1 = self._read(2)
            op, n = b0 & 15, b1 & 127
            n = struct.unpack('>H', self._read(2))[0] if n == 126 else struct.unpack('>Q', self._read(8))[0] if n == 127 else n
            if n > MAX_FRAME:
                raise WsError('frame_too_large')
            mask = self._read(4) if b1 & 128 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 8:
                raise WsError('closed_by_server')
            if op == 9:
                self.send(data, 10)
                continue
            if op == 10:
                continue
            parts.append(data)
            if b0 & 128:
                return b''.join(parts).decode('utf-8', 'replace') if op in (0, 1) else None

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Sio:
    """One Socket.IO v4 connection on the default namespace. events() yields (name, first_arg); a refused auth yields ('connect_error', message) and ends."""
    def __init__(self, url, path, headers, auth):
        u = urllib.parse.urlsplit(url)
        self.url = '%s://%s%s?EIO=4&transport=websocket' % ('wss' if u.scheme in ('https', 'wss') else 'ws', u.netloc, path)
        self.headers, self.auth, self.ws = headers, auth, None

    def events(self):
        self.ws = ws = WebSocket(self.url, self.headers)
        try:
            first = ws.recv()
            if not first or first[0] != '0':
                raise WsError('no_engine_open')
            info = json.loads(first[1:])
            ws.settimeout((info.get('pingInterval', 25000) + info.get('pingTimeout', 20000)) / 1000. + 5.)
            ws.send('40' + json.dumps(self.auth()))
            while True:
                msg = ws.recv()
                if not msg:
                    continue
                if msg == '2':
                    ws.send('3')
                elif msg.startswith('40'):
                    yield 'connect', None
                elif msg.startswith('44'):
                    try:
                        err = json.loads(msg[2:])
                    except ValueError:
                        err = None
                    yield 'connect_error', (err or {}).get('message') if isinstance(err, dict) else str(err)[:100]
                    return
                elif msg.startswith('42'):
                    body = msg[2:].lstrip('0123456789')
                    try:
                        arr = json.loads(body)
                    except ValueError:
                        continue
                    if isinstance(arr, list) and arr and isinstance(arr[0], str):
                        yield arr[0], arr[1] if len(arr) > 1 else None
                elif msg == '1':
                    raise WsError('engine_close')
        finally:
            ws.close()
