"""Two checks the launcher makes before any model child runs: the resource caps are really applied, and an outside guard says the host is ready.

The guard file is written by a process outside the capped group (a watchdog you run): {"state": "ready", "reason": null, "at": <unix seconds>} or
{"state": "paused", "reason": "<why>"}. Stale, unreadable or malformed reads as not ready."""
import json
import math
from pathlib import Path
import time

DEFAULT_CAPS = {'slice': 'office.slice', 'cpu_cores': 2, 'memory_max': 4294967296, 'pids_max': 256}


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def caps_applied(path, caps=None):
    """True only when the cgroup directory is the named slice and its CPU, memory and task limits equal the expected ones exactly."""
    caps = {**DEFAULT_CAPS, **(caps or {})}
    path = Path(path)
    if path.name != caps['slice']:
        return False
    try:
        quota, period = (int(x) for x in (path / "cpu.max").read_text().split())
        return (period > 0 and quota == caps['cpu_cores'] * period
                and (path / "memory.max").read_text().strip() == str(caps['memory_max'])
                and (path / "pids.max").read_text().strip() == str(caps['pids_max']))
    except (OSError, ValueError):
        return False


def external_guard(path, *, now=None, max_age_s=5.):
    """None when ready and fresh; otherwise the reason."""
    now = time.time() if now is None else now
    try:
        data = json.loads(Path(path).read_text())
        if data.get("state") == "paused" and isinstance(data.get("reason"), str) and data["reason"]:
            return data["reason"]
        if data.get("state") == "ready" and data.get("reason") is None and finite(data.get("at")):
            if not finite(now) or not finite(max_age_s) or not 0 <= now - data["at"] <= max_age_s:
                return "guard_stale"
            return None
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return "guard_unknown"
