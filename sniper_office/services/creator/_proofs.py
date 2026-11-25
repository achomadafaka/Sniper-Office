"""The small pieces the creator collectors took from the source project: hex and quantity checks, the mined-transaction identity check, the corrected clock and a direct JSON-RPC read transport.
The clock fails closed: no fresh correction sample means ValueError('clock_unknown'), never the host clock."""
from datetime import datetime, timezone
import json as _json
import os
import re
import time
import urllib.error
import urllib.request

from ...clock import corrected_now

TRANSFER_TOPIC = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
CLOCK_FILE = os.environ.get('OFFICE_CLOCK_FILE')      # set by the entry points from the config's clock_file


def _hex(value, size):
    if not isinstance(value, str) or not re.fullmatch(r'0x[0-9a-fA-F]{' + str(size * 2) + '}', value):
        raise ValueError('malformed_identity')
    return value.lower()


def _quantity(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r'0x[0-9a-fA-F]+', value):
        return int(value, 16)
    raise ValueError('malformed_quantity')


def _mined(receipt, transaction, header, chain, *, allow_failed=False):
    txhash, blockhash = _hex(transaction['hash'], 32), _hex(transaction['blockHash'], 32)
    block, index = _quantity(transaction['blockNumber']), _quantity(transaction['transactionIndex'])
    sender, target = _hex(transaction['from'], 20), _hex(transaction['to'], 20)
    if (not block or _quantity(receipt['status']) not in ((0, 1) if allow_failed else (1,)) or _hex(receipt['transactionHash'], 32) != txhash
            or _hex(receipt['blockHash'], 32) != blockhash or _quantity(receipt['blockNumber']) != block
            or _quantity(receipt['transactionIndex']) != index or _hex(receipt['from'], 20) != sender
            or _hex(receipt['to'], 20) != target or _hex(header['hash'], 32) != blockhash
            or _quantity(header['number']) != block or not _quantity(header['timestamp'])
            or (transaction.get('chainId') is not None and _quantity(transaction['chainId']) != int(chain))):
        raise ValueError('mined_identity_mismatch')
    logs = receipt['logs']
    if not isinstance(logs, list):
        raise ValueError('malformed_logs')
    for log in logs:
        if (log.get('removed') is True or _hex(log['transactionHash'], 32) != txhash
                or _hex(log['blockHash'], 32) != blockhash or _quantity(log['blockNumber']) != block
                or _quantity(log['transactionIndex']) != index):
            raise ValueError('log_identity_mismatch')
        _hex(log['address'], 20)
        topics = log['topics']
        if not isinstance(topics, list):
            raise ValueError('malformed_topics')
        if topics and str(topics[0]).lower() == TRANSFER_TOPIC:
            if len(topics) != 3:
                raise ValueError('malformed_transfer')
            for topic in topics[1:]:
                if _hex(topic, 32)[2:26] != '0' * 24:
                    raise ValueError('malformed_topic_address')
            _hex(log['data'], 32)
    return sender, target, txhash, [block, index], _quantity(header['timestamp'])


def real_utc_now_iso(path=None):
    now = corrected_now(path or CLOCK_FILE or '', host_now=time.time())
    if now is None:
        raise ValueError('clock_unknown')
    return datetime.fromtimestamp(now, tz=timezone.utc).isoformat()


def credential_ids(url):
    """The credential-looking parts of an endpoint URL (query keys, long path segments), to tell that two URLs share one provider account."""
    from urllib.parse import parse_qs, urlsplit
    parsed = urlsplit(url)
    secrets = {v for key, values in parse_qs(parsed.query).items() if key.lower() in ('api-key', 'apikey', 'key', 'dkey', 'token') for v in values}
    if parsed.hostname and parsed.hostname.endswith('quiknode.pro'):
        secrets.update(p for p in parsed.path.split('/') if len(p) >= 16)
    parts = [p for p in parsed.path.split('/') if p]
    if parts and parsed.hostname and parsed.hostname.endswith(('g.alchemy.com', 'infura.io', 'ankr.com')) and len(parts) > 1:
        secrets.add(parts[-1])
    secrets.update(p for p in parts if len(p) >= 16)
    return secrets


def isolated_url(url, other_urls):
    """An https research endpoint with no credentials in the authority that neither equals nor shares an account with any URL in other_urls."""
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('invalid_research_url')
    identity = credential_ids(url)
    for other in other_urls:
        if other and (url.rstrip('/') == other.rstrip('/') or identity & credential_ids(other)):
            raise ValueError('provider_overlap')
    return url


class _Response:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return _json.loads(self._body)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _post(url, *, json, timeout, allow_redirects=False):
    request = urllib.request.Request(url, data=_json.dumps(json).encode(), headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as response:
            return _Response(response.status, response.read(8 * 1024 * 1024))
    except urllib.error.HTTPError as exc:
        return _Response(exc.code, exc.read(65536))


class ReadRpc:
    """Direct research transport: checked results, strict attempts, no hidden retries."""
    def __init__(self, url, *, limit=160, post=None, pace=None, allowed=None):
        self.url, self.limit, self.used = url, int(limit), 0
        self.post = post or _post
        self.pace = pace or (lambda: None)
        self.allowed = frozenset() if allowed is None else allowed
        self.id = 0
        self.last_request_at = None

    def __call__(self, method, params):
        if method not in self.allowed:
            raise ValueError('read_method_refused')
        if self.used >= self.limit:
            raise ValueError('budget')
        self.pace()
        self.used += 1
        self.id += 1
        self.last_request_at = real_utc_now_iso()
        try:
            response = self.post(self.url, json=dict(jsonrpc='2.0', id=self.id, method=method, params=params), timeout=12, allow_redirects=False)
            if 300 <= response.status_code < 400:
                raise ValueError('provider_redirect')
            if response.status_code in (401, 403):
                raise ValueError('provider_rejected')
            if response.status_code == 429:
                raise ValueError('rate_limit')
            if response.status_code >= 400:
                raise ValueError('provider_http_error')
            data = response.json()
        except TimeoutError:
            raise ValueError('provider_timeout') from None
        except ValueError as exc:
            if str(exc) in ('provider_rejected', 'rate_limit', 'provider_http_error', 'provider_redirect'):
                raise
            raise ValueError('malformed_response') from None
        except Exception as exc:
            raise ValueError('provider_timeout' if 'timeout' in type(exc).__name__.lower() else 'provider_transport') from None
        if not isinstance(data, dict) or data.get('id') != self.id or data.get('jsonrpc') != '2.0':
            raise ValueError('malformed_response')
        if data.get('error') is not None:
            msg = str(data['error']).lower()
            raise ValueError('rate_limit' if 'rate limit' in msg or 'limit exceeded' in msg or 'too many' in msg
                             else 'credits_exhausted' if 'credit' in msg or 'quota' in msg else 'rpc_rejected')
        if 'result' not in data or data['result'] is None:
            raise ValueError('missing_result')
        return data['result']

    def close(self):
        pass
