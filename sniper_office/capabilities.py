"""Host-owned capabilities; persona text cannot grant them.

Which role may call which tool is configuration, loaded by sniper_office.config. With nothing configured every tool is denied."""
from dataclasses import dataclass
import copy
import math
import json

CAPS = {}                    # role -> frozenset of tool names
OFFCHAIN_TOOLS = frozenset()  # tools that may run when the cut carries no slot_cut


def configure(*, roles, cut_free_tools=()):
    """Replace the role table. Unknown shapes are refused, never read as an empty grant."""
    global OFFCHAIN_TOOLS
    if not isinstance(roles, dict) or not all(isinstance(r, str) and r and isinstance(t, (list, tuple, set, frozenset))
                                               and all(isinstance(x, str) and x for x in t) for r, t in roles.items()):
        raise ValueError("capabilities_config")
    if not all(isinstance(x, str) and x for x in cut_free_tools):
        raise ValueError("capabilities_config")
    CAPS.clear()
    CAPS.update({role: frozenset(tools) for role, tools in roles.items()})
    OFFCHAIN_TOOLS = frozenset(cut_free_tools)


@dataclass(frozen=True)
class Context:
    actor_id: str
    role: str
    book_id: str
    generation: int
    decision_id: str | None
    cut: dict

    def __post_init__(self):
        object.__setattr__(self, "cut", copy.deepcopy(self.cut))


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def cut_refusal(cut):
    """Shared live/replay corrected-UTC cut validation; no per-tool permission changes."""
    if (not isinstance(cut, dict) or cut.get("basis") != "corrected_utc" or not all(_finite(cut.get(k)) for k in ("receipt_cut", "knowledge_cut"))
            or "slot_cut" not in cut
            or (cut["slot_cut"] is not None and (type(cut["slot_cut"]) is not int or cut["slot_cut"] < 0))
            or not isinstance(cut.get("clock_sample"), dict)
            or not _finite(cut["clock_sample"].get("corrected_utc"))
            or type(cut["clock_sample"].get("offset_s")) not in (int, float)
            or not math.isfinite(cut["clock_sample"]["offset_s"])
            or max(cut["receipt_cut"], cut["knowledge_cut"]) > cut["clock_sample"]["corrected_utc"]):
        return "cut_invalid"
    # A host-time receipt clock must agree with the corrected one: corrected UTC = host - frozen offset.
    if "host_receipt_cut" in cut and (not _finite(cut["host_receipt_cut"])
            or not math.isclose(cut["host_receipt_cut"] - cut["clock_sample"]["offset_s"],
                                cut["receipt_cut"], rel_tol=0., abs_tol=1e-6)):
        return "cut_invalid"
    return None


def refusal(context, request):
    if not isinstance(request.tool, str) or not isinstance(request.actor_id, str):
        return "request_invalid"
    if request.actor_id != context.actor_id:
        return "actor_mismatch"
    if request.decision_id != context.decision_id:
        return "decision_mismatch"
    cut = context.cut
    reason = cut_refusal(cut)
    if reason:
        return reason
    try:
        same = json.dumps(request.cut, sort_keys=True, allow_nan=False) == json.dumps(cut, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        same = False
    if not same:
        return "cut_mismatch"
    if request.tool not in CAPS.get(context.role, frozenset()):
        return "tool_denied"
    if cut["slot_cut"] is None and request.tool not in OFFCHAIN_TOOLS:
        return "slot_unknown"
    if not isinstance(request.args, dict):
        return "args_invalid"
    if request.tool.startswith("book.") and request.args.get("book_id") != context.book_id:
        return "book_mismatch"
    return None
