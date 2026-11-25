"""Bounded local nominee resolution. A nominee is the exact configuration under evaluation: the design file, every prompt, the roster's models and one sealed memory snapshot.

Inputs are operator-owned, read-only pinned bytes. Each named file is read once through the launcher authority's directory handle and compared by hash;
the live roster and the stored snapshot are compared to what the nominee states. A project adds its own rules with `check` (for example that a cost
rule or a proof file says what the nominee claims): it receives the parsed JSON documents and raises Invalid."""
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
import time

from . import memory as M, memory_store as MS, store as S

REQUIRED = frozenset({'design_sha256', 'memory_snapshot_id', 'prompt_hashes', 'model_ids'})
MAX_AGENTS = 64


class Invalid(ValueError):
    pass


def _finite(v):
    return type(v) in (int, float) and math.isfinite(v)


def _check_time(deadline):
    if time.monotonic() > deadline:
        raise Invalid('nominee_resolution_budget')


def _read(root, ref, deadline, *, authority=None):
    _check_time(deadline)
    if not isinstance(ref, dict) or set(ref) != {'path','sha256'}:
        raise Invalid('unresolved_file')
    rel = PurePosixPath(ref['path'])
    if rel.is_absolute() or '..' in rel.parts or not rel.parts:
        raise Invalid('nominee_path_denied')
    fd = os.dup(root) if type(root) is int else os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in rel.parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        file_fd = os.open(rel.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o222:
                raise Invalid('nominee_file_not_readonly')
            if authority is not None:
                from . import launcher_policy as LP
                if str(rel) not in authority.probes or (authority.readonly and not LP.readonly_mount(stream.fileno())):
                    raise Invalid('nominee_file_readonly_authority_unknown')
                uid = authority.policy['office_uid']
                if info.st_uid == uid:
                    if uid == os.geteuid():
                        safe = not os.access('/proc/self/fd/'+str(fd), os.W_OK, effective_ids=True)
                    else:
                        safe = authority.probes.get(str(rel.parent)) is False
                    if not safe:
                        raise Invalid('office_can_modify_owned_configuration')
            raw = stream.read(262145)
        if len(raw) > 262144 or hashlib.sha256(raw).hexdigest() != ref['sha256']:
            raise Invalid('nominee_file_changed')
        _check_time(deadline)
        return raw
    finally:
        os.close(fd)


def resolve(conn, nominee, refs, *, root, k, opened_at, deadline, authority, check=None):
    """Resolve once per file; compare bytes and live roster/snapshot, then return canonical copies."""
    try:
        if not isinstance(nominee, dict) or not REQUIRED <= set(nominee):
            raise Invalid('nominee_missing_key')
        if any(nominee[key] in (None, '', 'unmeasured') for key in nominee):
            raise Invalid('nominee_unmeasured')            # every stated value must be known, the project's extra ones too
        if not isinstance(refs, dict) or len(refs) > 40 or 'design' not in refs:
            raise Invalid('nominee_refs_invalid')
        Path(root).resolve(strict=True)
        raw = {key: _read(authority.root_fd, ref, deadline, authority=authority) for key, ref in refs.items()}
        if nominee['design_sha256'] != refs['design']['sha256'] or nominee['design_sha256'] != authority.policy['approved_design_sha256']:
            raise Invalid('design_not_launcher_approved')
        docs = {key: json.loads(value) for key, value in raw.items() if not key.startswith('prompt:')}
        active = [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM agents WHERE json_extract(payload_json,'$.state')='active' LIMIT ?", (MAX_AGENTS + 1,))]
        if not 1 <= len(active) <= MAX_AGENTS:
            raise Invalid('nominee_roster_invalid')
        prompts = {a['agent_id']: a['prompt_hash'] for a in active}
        models = {a['agent_id']: {key: a[key] for key in ('model', 'provider', 'family')} for a in active}
        if nominee['prompt_hashes'] != prompts or nominee['model_ids'] != models:
            raise Invalid('nominee_roster_changed')
        if any(refs['prompt:' + aid]['sha256'] != value for aid, value in prompts.items()):
            raise Invalid('nominee_prompt_changed')
        mem = MS.OfficeMemory(conn)
        size = conn.execute('SELECT length(payload_json) FROM memory_snapshots WHERE snapshot_id=?', (nominee['memory_snapshot_id'],)).fetchone()
        if size is None or size[0] > 262144:
            raise Invalid('nominee_snapshot_unbounded')
        snap = mem.get_snapshot(nominee['memory_snapshot_id'])
        if snap is None or snap['cut'] >= opened_at or M._snapshot_id(snap) != nominee['memory_snapshot_id']:
            raise Invalid('nominee_snapshot_invalid')
        if len(snap['members']) + len(snap['supersessions']) > 1024:
            raise Invalid('nominee_snapshot_unbounded')
        # Re-verifies every member's content hash: an edited or deleted member refuses here.
        M.verify_snapshot(mem, snap, nominee['memory_snapshot_id'])
        if check is not None:
            check(nominee, docs, k=k, opened_at=opened_at)
        _check_time(deadline)
        return json.loads(S.canonical_json(nominee)), json.loads(S.canonical_json(refs))
    except (OSError, KeyError, TypeError, ValueError, M.Refused) as exc:
        raise Invalid(str(exc)) from None
