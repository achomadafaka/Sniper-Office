"""The ABI and hash pieces the creator collectors need, in the standard library: Keccak-256, and encode/decode for uint256, address, string and a tuple of those.
Stands in for eth_abi / eth_utils. Decode is lenient about nothing it can check; callers re-encode and compare (linked._abi), so a non-canonical input is refused there."""
import re

_RC = (0x1, 0x8082, 0x800000000000808a, 0x8000000080008000, 0x808b, 0x80000001, 0x8000000080008081, 0x8000000000008009, 0x8a, 0x88, 0x80008009, 0x8000000a, 0x8000808b,
       0x800000000000008b, 0x8000000000008089, 0x8000000000008003, 0x8000000000008002, 0x8000000000000080, 0x800a, 0x800000008000000a, 0x8000000080008081, 0x8000000000008080,
       0x80000001, 0x8000000080008008)
_ROT = (1, 3, 6, 10, 15, 21, 28, 36, 45, 55, 2, 14, 27, 41, 56, 8, 25, 43, 62, 18, 39, 61, 20, 44)
_PI = (10, 7, 11, 17, 18, 3, 5, 16, 8, 21, 24, 4, 15, 23, 19, 13, 12, 2, 20, 14, 22, 9, 6, 1)
_M = (1 << 64) - 1


def _f(a):
    for rc in _RC:
        c = [a[x] ^ a[x + 5] ^ a[x + 10] ^ a[x + 15] ^ a[x + 20] for x in range(5)]
        for x in range(5):
            d = c[(x + 4) % 5] ^ (((c[(x + 1) % 5] << 1) | (c[(x + 1) % 5] >> 63)) & _M)
            for y in range(0, 25, 5):
                a[y + x] ^= d
        t = a[1]
        for i in range(24):
            j = _PI[i]
            a[j], t = (((t << _ROT[i]) | (t >> (64 - _ROT[i]))) & _M), a[j]
        for y in range(0, 25, 5):
            row = a[y:y + 5]
            for x in range(5):
                a[y + x] = row[x] ^ (~row[(x + 1) % 5] & row[(x + 2) % 5])
        a[0] ^= rc


def keccak(primitive=None, *, text=None):
    """Keccak-256 (the original padding, not NIST SHA3-256). keccak(b'..') or keccak(text='..')."""
    data = text.encode() if text is not None else bytes(primitive)
    rate, a = 136, [0] * 25
    data += b'\x01' + bytes(-(len(data) + 1) % rate)
    data = data[:-1] + bytes([data[-1] | 0x80])
    for off in range(0, len(data), rate):
        block = data[off:off + rate]
        for i in range(rate // 8):
            a[i] ^= int.from_bytes(block[i * 8:i * 8 + 8], 'little')
        _f(a)
    return b''.join(a[i].to_bytes(8, 'little') for i in range(4))


class DecodingError(Exception):
    pass


def _split(t):
    parts, depth, cur = [], 0, ''
    for ch in t[1:-1]:
        depth += ch == '('
        depth -= ch == ')'
        if ch == ',' and not depth:
            parts.append(cur)
            cur = ''
        else:
            cur += ch
    return [p for p in parts + [cur] if p]


def _dynamic(t):
    return t in ('string', 'bytes') or (t.startswith('(') and any(_dynamic(p) for p in _split(t)))


def _head(t):
    return (sum(_head(p) for p in _split(t)) if t.startswith('(') and not _dynamic(t) else 32)


def _enc(t, v):
    """(is_dynamic, bytes) of one value."""
    if t == 'uint256':
        if type(v) is not int or not 0 <= v < 1 << 256:
            raise ValueError('bad_uint256')
        return v.to_bytes(32, 'big')
    if t == 'address':
        if not isinstance(v, str) or not re.fullmatch(r'0x[0-9a-fA-F]{40}', v):
            raise ValueError('bad_address')
        return bytes(12) + bytes.fromhex(v[2:])
    if t in ('string', 'bytes'):
        raw = v.encode() if t == 'string' else bytes(v)
        return len(raw).to_bytes(32, 'big') + raw + bytes(-len(raw) % 32)
    if t.startswith('('):
        parts = _split(t)
        if len(parts) != len(v):
            raise ValueError('bad_tuple')
        return encode(parts, v)
    raise ValueError('unsupported_type')


def encode(types, values):
    if len(types) != len(values):
        raise ValueError('bad_arity')
    heads, tails, base = [], [], sum(_head(t) for t in types)
    for t, v in zip(types, values):
        body = _enc(t, v)
        if _dynamic(t):
            heads.append((base + sum(map(len, tails))).to_bytes(32, 'big'))
            tails.append(body)
        else:
            heads.append(body)
    return b''.join(heads) + b''.join(tails)


def _word(raw, at):
    if at < 0 or at + 32 > len(raw):
        raise DecodingError('short')
    return raw[at:at + 32]


def _dec(t, raw, at):
    if t == 'uint256':
        return int.from_bytes(_word(raw, at), 'big')
    if t == 'address':
        word = _word(raw, at)
        if any(word[:12]):
            raise DecodingError('address_padding')
        return '0x' + word[12:].hex()
    if t in ('string', 'bytes'):
        n = int.from_bytes(_word(raw, at), 'big')
        body = raw[at + 32:at + 32 + n]
        if len(body) != n:
            raise DecodingError('length')
        if t == 'bytes':
            return bytes(body)
        try:
            return body.decode()
        except UnicodeDecodeError:
            raise DecodingError('utf8') from None
    if t.startswith('('):
        return _dec_tuple(_split(t), raw, at)
    raise DecodingError('unsupported_type')


def _dec_tuple(types, raw, base):
    out, at = [], base
    for t in types:
        if _dynamic(t):
            off = int.from_bytes(_word(raw, at), 'big')
            out.append(_dec(t, raw, base + off))
            at += 32
        else:
            out.append(_dec(t, raw, at))
            at += _head(t)
    return tuple(out)


def decode(types, raw):
    return _dec_tuple(list(types), bytes(raw), 0)
