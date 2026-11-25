"""Host health watchdog: the producer of the guard file that host_guard.external_guard() and the supervisor read. One host process, stdlib, read-only on the host.

It replaces the source project's host monitor + watchdog + wake timer in one loop. Each second it samples CPU and IO pressure (/proc/pressure, `some avg10`), MemAvailable, the age of the
heartbeat files named in the config (the seat hosts' own heartbeats, in place of the source's funded-runner heartbeat) and, if asked, whether the office cgroup caps are applied. It writes
    {"state":"ready","reason":null,"at":T,...}   or   {"state":"paused","reason":"cpu_pressure",...}
atomically. Unknown telemetry pauses at once. A pressure pause lifts only after `resume_after_s` (900) of continuous health; a pause for any other reason (caps, telemetry) never lifts by itself
(an operator removes the file or restarts with --initialize). With `kill_file` set (an office.slice cgroup.kill) a new pause also writes 1 there: opt-in, off by default.

    python3 -m sniper_office.services.host_watchdog --config host_watchdog.json [--once]"""
import argparse
import json
from pathlib import Path
import sys
import time

from .. import host_guard
from . import common

POLICY = 'office-host-guard-v1'
AUTO_RESUME = frozenset({'cpu_pressure', 'io_pressure', 'memory_available', 'heartbeat'})
DEFAULTS = dict(psi_max=0.20, mem_min_bytes=4 * 1024 ** 3, heartbeat_max_age_s=30., heartbeat_breaches=2, resume_after_s=900., interval_s=1.)


def read_psi(kind, proc='/proc/pressure'):
    for line in Path(proc, kind).read_text().splitlines():
        if line.startswith('some '):
            return float(dict(v.split('=') for v in line.split()[1:])['avg10']) / 100.
    raise ValueError('pressure_unknown')


def read_mem(path='/proc/meminfo'):
    for line in Path(path).read_text().splitlines():
        if line.startswith('MemAvailable:'):
            return int(line.split()[1]) * 1024
    raise ValueError('memory_unknown')


def heartbeat_age(path, now):
    try:
        beat = json.loads(Path(path).read_text())
        ts = beat.get('ts', beat.get('at'))
        return now - ts if host_guard.finite(ts) and ts <= now else None
    except (OSError, ValueError, AttributeError):
        return None


def sample(cfg, now, *, psi=read_psi, mem=read_mem):
    out = dict(ts=now, cpu=None, io=None, mem=None, heartbeats={}, caps=None, telemetry_error=False)
    try:
        out['cpu'], out['io'], out['mem'] = psi('cpu'), psi('io'), mem()
    except (OSError, ValueError, KeyError):
        out['telemetry_error'] = True
    for name in cfg.get('heartbeat_files', []):
        out['heartbeats'][name] = heartbeat_age(name, now)
    if cfg.get('office_cgroup'):
        out['caps'] = host_guard.caps_applied(cfg['office_cgroup'], cfg.get('caps'))
    return out


class Guard:
    """State machine over samples. check() returns a pause reason or None."""
    def __init__(self, cfg):
        self.c = {**DEFAULTS, **{k: cfg[k] for k in DEFAULTS if k in cfg}}
        self.breaches, self.healthy_since = {}, None

    def check(self, s):
        c = self.c
        if s['telemetry_error'] or any(not host_guard.finite(s[k]) for k in ('cpu', 'io', 'mem')):
            self.healthy_since = None
            return 'telemetry_unknown'
        if s['caps'] is False:
            self.healthy_since = None
            return 'caps_unapplied'
        for name, age in s['heartbeats'].items():
            self.breaches[name] = self.breaches.get(name, 0) + 1 if age is None or age > c['heartbeat_max_age_s'] else 0
            if self.breaches[name] >= c['heartbeat_breaches']:
                self.healthy_since = None
                return 'heartbeat'
        reason = ('cpu_pressure' if s['cpu'] > c['psi_max'] else 'io_pressure' if s['io'] > c['psi_max'] else 'memory_available' if s['mem'] < c['mem_min_bytes'] else None)
        if reason:
            self.healthy_since = None
            return reason
        if self.healthy_since is None:
            self.healthy_since = s['ts']
        return None

    def resume_eligible(self, now):
        return self.healthy_since is not None and now - self.healthy_since >= self.c['resume_after_s']


def step(cfg, guard, now, *, psi=read_psi, mem=read_mem, kill=None):
    """One tick: sample, decide, write the guard file. Returns the document written."""
    path = Path(cfg['guard_file'])
    s = sample(cfg, now, psi=psi, mem=mem)
    reason = guard.check(s)
    try:
        prior = json.loads(path.read_text())
    except (OSError, ValueError):
        prior = None
    prior = prior if isinstance(prior, dict) else {}
    paused_before = prior.get('state') == 'paused' and isinstance(prior.get('reason'), str)
    if reason is None and paused_before:
        if prior['reason'] in AUTO_RESUME and guard.resume_eligible(now) and prior.get('latched') is not True:
            doc = dict(state='ready', reason=None, at=now, guard_policy_version=POLICY, previous_pause_reason=prior['reason'])
        else:
            doc = dict(prior, at=now, healthy_since=guard.healthy_since)        # stays paused; the file stays fresh so a stale guard is never mistaken for it
    elif reason is None:
        doc = dict(state='ready', reason=None, at=now, guard_policy_version=POLICY)
    elif paused_before and (prior.get('latched') is True or prior['reason'] not in AUTO_RESUME):
        doc = dict(prior, at=now, last_reason=reason)        # a latched pause keeps its reason: a later pressure reading must not downgrade it to one that lifts by itself
    else:
        doc = dict(state='paused', reason=reason, at=now, guard_policy_version=POLICY, latched=reason not in AUTO_RESUME)
        if not paused_before and cfg.get('kill_file') and kill is not None:
            kill(cfg['kill_file'])
    doc['sample'] = {k: s[k] for k in ('cpu', 'io', 'mem')}
    common.write_atomic(path, doc)
    return doc


def kill_slice(path):
    with open(path, 'w') as fh:      # office.slice/cgroup.kill: writing 1 kills every process in the slice
        fh.write('1\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--initialize', action='store_true', help='clear a latched pause after the operator has looked at it')
    args = parser.parse_args(argv)
    cfg = common.load_config(args.config)
    if args.initialize:
        Path(cfg['guard_file']).unlink(missing_ok=True)
    guard = Guard(cfg)
    while True:
        doc = step(cfg, guard, time.time(), kill=kill_slice)
        if args.once:
            print(json.dumps({k: doc[k] for k in ('state', 'reason', 'sample')}))
            return 0
        time.sleep(guard.c['interval_s'])


if __name__ == '__main__':
    sys.exit(main())
