"""One JSON file names everything project-specific: roles, their tools, extra tables. Nothing is granted by default.

    config.load('office.json', expected_sha256=PINNED)   # before store.open_store()

Keys (all optional; a missing key leaves that module's fail-closed default):
  roles              {role: [tool, ...]}          capabilities.CAPS
  cut_free_tools     [tool, ...]                  tools allowed when the cut has no slot_cut
  role_aliases       {stored role: context role}  supervisor
  deferrable_reasons [reason, ...]                supervisor
  memory             {roles, quarantined, source_keys}
  tickets            {crew_ids, approver_role, queue_roles}
  handoff_kinds      [[reference kind, receiving role, event kind], ...]
  tables             {append_only, private, columns, indexes}   store.extend_schema
"""
import hashlib
import json

from . import capabilities, memory, runtime_observations, store, supervisor, tickets

KEYS = frozenset({'roles', 'cut_free_tools', 'role_aliases', 'deferrable_reasons', 'memory', 'tickets', 'handoff_kinds', 'tables'})


class ConfigMismatch(Exception):
    pass


def apply(cfg):
    if not isinstance(cfg, dict) or set(cfg) - KEYS:
        raise ValueError('config_unknown_key')      # a typo must not read as "nothing configured"
    if 'roles' in cfg:
        capabilities.configure(roles=cfg['roles'], cut_free_tools=cfg.get('cut_free_tools', ()))
    elif 'cut_free_tools' in cfg:
        raise ValueError('config_roles_required')
    if 'role_aliases' in cfg or 'deferrable_reasons' in cfg:
        supervisor.configure(role_aliases=cfg.get('role_aliases'), deferrable_reasons=cfg.get('deferrable_reasons'))
    if 'memory' in cfg:
        memory.configure(**cfg['memory'])
    if 'tickets' in cfg:
        tickets.configure(**cfg['tickets'])
    if 'handoff_kinds' in cfg:
        kinds = cfg['handoff_kinds']
        if not all(isinstance(k, list) and len(k) == 3 and all(isinstance(x, str) and x for x in k) for k in kinds):
            raise ValueError('config_handoff_kinds')
        runtime_observations.HANDOFF_KINDS.clear()
        runtime_observations.HANDOFF_KINDS.update({(a, b): c for a, b, c in kinds})
    if 'tables' in cfg:
        store.extend_schema(**cfg['tables'])
    return cfg


def load(path, expected_sha256=None):
    """Read, check the pin when one is given, apply. A changed file with a pin is refused, never applied."""
    raw = open(path, 'rb').read()
    got = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and got != expected_sha256:
        raise ConfigMismatch('config sha256 %s != pinned %s' % (got, expected_sha256))
    return apply(json.loads(raw))
