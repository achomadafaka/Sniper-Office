"""Shared small pieces of the services package: config and env-file reads, atomic JSON, a bounded https GET with an injectable transport, the corrected clock."""
import json
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.error
import urllib.request

from ..clock import corrected_now

MAX_BODY = 2 * 1024 * 1024


def load_config(path):
    doc = json.loads(Path(path).read_text())
    if not isinstance(doc, dict):
        raise ValueError('config_not_object')
    return doc


def env_values(path):
    """KEY=VALUE lines of an env file (comments and blanks skipped, one pair of quotes stripped). Values are never logged."""
    out = {}
    for line in Path(path).read_text().splitlines():
        m = re.fullmatch(r'\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*', line)
        if m and not line.lstrip().startswith('#'):
            v = m.group(2)
            out[m.group(1)] = v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in '"\'' else v
    return out


def write_atomic(path, doc):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.tmp-')
    try:
        with os.fdopen(fd, 'w') as fh:
            json.dump(doc, fh, sort_keys=True, allow_nan=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def fetch_json(url, *, timeout=15):
    """(status, parsed json or None). Only https, no redirects, 2 MB cap. Network failure is (None, None). Tests replace this function."""
    if not url.startswith('https://'):
        return None, None
    req = urllib.request.Request(url, headers={'User-Agent': 'sniper-office-services', 'Accept': 'application/json'})
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as r:
            raw = r.read(MAX_BODY + 1)
            return r.status, (json.loads(raw) if len(raw) <= MAX_BODY else None)
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (OSError, ValueError):
        return None, None


def clock_now(clock_file, host_now=None):
    """Corrected UTC seconds, or None when the sample is missing or stale (fail closed)."""
    return corrected_now(clock_file, host_now=time.time() if host_now is None else host_now) if clock_file else None
