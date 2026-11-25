"""Research-only error evidence around the existing direct, no-retry read transport."""
import re

from ._abi import decode

from ._proofs import ReadRpc


def _revert(data, message):
    if not data or len(data) < 10:
        return False
    selector, body = data[:10].lower(), bytes.fromhex(data[10:])
    if selector in ('0x4e487b71', '0x08c379a0'):
        try:
            decode(['uint256' if selector == '0x4e487b71' else 'string'], body)
            return True
        except (ValueError, TypeError, OverflowError):
            return False
        except Exception:
            return False
    return len(body) % 32 == 0 and 'execution reverted' in message.lower()


class ResearchRpc(ReadRpc):
    """Raise typed ValueError; retain only bounded public error metadata in last_error."""
    def __init__(self, url, *, allowed, post=None, limit=1000):
        super().__init__(url, allowed=allowed, post=post, limit=limit)
        self.last_error = {}
        self._response = None
        send = self.post

        def capture(*args, **kwargs):
            self._response = send(*args, **kwargs)
            return self._response

        self.post = capture

    def __call__(self, method, params):
        self.last_error, self._response = {}, None
        try:
            return super().__call__(method, params)
        except ValueError as exc:
            kind, code, data, message = str(exc), None, None, None
            # Only these failures occur after ReadRpc validates the JSON-RPC envelope/id.
            if kind in ('rpc_rejected', 'rate_limit', 'credits_exhausted') and (
                    self._response is not None and self._response.status_code == 200):
                raw = self._response.json().get('error')
                if isinstance(raw, dict):
                    code = raw.get('code') if type(raw.get('code')) is int else None
                    text = raw.get('message') if isinstance(raw.get('message'), str) else ''
                    value = raw.get('data')
                    if isinstance(value, str) and len(value) % 2 == 0 and re.fullmatch('0x[0-9a-fA-F]*', value):
                        data = value[:2048]
                    if kind == 'rpc_rejected':
                        if code == 429:
                            kind = 'rate_limit'
                        elif code == 3 or _revert(value if data is not None else None, text):
                            kind = 'contract_revert'
                    if kind == 'contract_revert':
                        message = ('execution reverted: arithmetic underflow or overflow'
                            if text.lower().startswith('execution reverted: arithmetic underflow or overflow')
                            else 'execution reverted')
                    elif text.lower() in ('not supported', 'method not found', 'rate limit',
                            'rate limit exceeded', 'too many requests', 'credits exhausted', 'quota exceeded'):
                        message = text.lower()
            self.last_error = dict(kind=kind, code=code, data=data, message=message)
            raise ValueError(kind) from None
