"""Host-reviewed tools. Load through the existing trusted seat plug-in config."""

from . import debate_index, pm_rules
from ..services import narrative_guess_coverage

TOOLS = (debate_index, narrative_guess_coverage, pm_rules)


def register(host):
    grants = host.cfg.get('approved_tools')
    role = host.cfg.get('role')
    if not isinstance(grants, dict) or not isinstance(role, str):
        return
    names = grants.get(role)
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        return
    for module in TOOLS:
        if module.NAME in names:
            module.register(host)
