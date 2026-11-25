"""Org core. PURE seat expansion, limit validation and change classification; hire/fire/epochs persistence is roster.py.
The org file (pods x per-pod template + shared seats) and the limits file are operator-owned and pinned by sha256. Fixed seats are restaffable, never removable; the org size has a
ceiling; roles named in the limits must reach a minimum count and cover the required model families. The head role can apply persona/prompt/org changes but never a kind listed in
head_forbidden_changes. Every material change opens an epoch; a change kind that is not classified is REFUSED, never treated as immaterial."""
import hashlib
import json


class ConfigMismatch(Exception):
    pass


class BadOrg(ValueError):
    pass


class UnknownChange(ValueError):
    pass


def _load(path, sha):
    raw = open(path, "rb").read()
    got = hashlib.sha256(raw).hexdigest()
    if got != sha:
        raise ConfigMismatch("%s sha256 %s != pinned %s" % (path, got, sha))
    return json.loads(raw)


def load_org(path, sha):
    return _load(path, sha)


def load_limits(path, sha):
    return _load(path, sha)


def expand(org, limits=None):
    """-> list of seat dicts {role, pod, family, ...}. Shared seats exist once for the whole org; every pod gets the template's seats.
    A template entry is {role, per_pod, families, attribute?, values?}: families rotate across pods and seats, and an entry with an
    attribute (a lane, a lens, a beat) spreads its values round-robin across pods so every value is covered."""
    n = org["pods"]
    if limits is not None and not limits["min_pods"] <= n <= limits["max_pods"]:
        raise BadOrg("pod count %d outside [%d, %d]" % (n, limits["min_pods"], limits["max_pods"]))
    seats = []
    for s in org["shared_seats"]:
        for _ in range(s["count"]):
            seats.append({"role": s["role"], "pod": None, "family": s["family"], **({"tier": s["tier"]} if "tier" in s else {})})
    for i in range(n):
        pod = "pod%d" % (i + 1)
        for t in org["pod_template"]["seats"]:
            for j in range(t["per_pod"]):
                seat = {"role": t["role"], "pod": pod, "family": t["families"][(i + j) % len(t["families"])]}
                if "attribute" in t:
                    seat[t["attribute"]] = t["values"][(i * t["per_pod"] + j) % len(t["values"])]
                seats.append(seat)
    return seats


def validate(seats, limits, deterministic=()):
    """`deterministic` = seats that exist as deterministic services, not agents: they satisfy a fixed-seat requirement, they are never counted as agents.
    limits["role_minimums"] is {role: least count}; limits["role_families_required"] is {role: [family, ...]}, for roles whose seats must not all share one model family."""
    problems = []
    roles = {s["role"] for s in seats} | set(deterministic)
    for r in limits["fixed_seats"]:
        if r not in roles:
            problems.append("missing_fixed_seat:%s" % r)
    if len(seats) > limits["org_size_ceiling"]:
        problems.append("org_size_over_ceiling")
    for role, least in sorted(limits.get("role_minimums", {}).items()):
        if sum(s["role"] == role for s in seats) < least:
            problems.append("too_few:%s" % role)
    for role, families in sorted(limits.get("role_families_required", {}).items()):
        for f in families:
            if not any(s["role"] == role and s["family"] == f for s in seats):
                problems.append("missing_family:%s:%s" % (role, f))
    return problems


def head_may_apply(limits, change):
    return change["kind"] not in limits["head_forbidden_changes"]


def opens_epoch(limits, change):
    k = change.get("kind")
    if k in limits["material_changes"] or k in limits["head_forbidden_changes"]:
        return True                                   # changes only the operator side may make open an epoch too, each time
    if k in limits["immaterial_changes"]:
        return False
    raise UnknownChange("change kind %r is not classified" % (k,))
