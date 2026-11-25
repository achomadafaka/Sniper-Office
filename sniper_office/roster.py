"""Office Task 10 persistence (Lambda): hire / fire / update_agent / change_org / epochs on the Office store. The ONLY writer of `agents` and `epochs` (Codex's Supervisor reads agents, never writes).
Every change is ONE write transaction: the agent row, an append-only `org_changes` row and the epoch (the previous open epoch is closed, the new one is linked to the org change seq) land together or not at all.
hire is idempotent on a key (agent_id = ag_ + sha256(key)[:16], PK existence, so a crash/restart never makes two agents and never regenerates a persona). Persona generation is an INJECTED callable (a model call routed through
the Task 2 scheduler by the caller; it runs OUTSIDE the write lock). A persona may carry no performance claims (numbers/percent/win-rate/profit): only the stored career record states performance.
generation increments on any prompt/persona/model change and on archive; accept_answer rejects an answer whose generation is not the current one or whose agent is archived. Risk and Postmortem can be restaffed, never removed.
The Head can apply persona/prompt/org changes but never limits['head_forbidden_changes']; crew-made changes of those kinds are allowed and open an epoch too. Unknown kinds and unknown clocks write nothing."""
import hashlib
import json
import math
import re
import time

from sniper_office import org as O
from sniper_office import store as S

_PERF = re.compile(r"[$%]|\d|\b(win\s*rate|profit|p\s*&\s*l|pnl|returns?|roi|record)\b", re.I)


class OrgRefused(Exception):
    pass


class BadPersona(ValueError):
    pass


ACTORS = frozenset({"head", "crew"})                  # the Head is the only Office actor allowed to change the org; "crew" is the trusted crew path. Anything else (an agent name, model text) is REFUSED, never implicitly crew.


def _actor(by):
    if by not in ACTORS:
        raise OrgRefused("unknown_actor: %r" % (by,))
    return by


def _now(clock):
    t = clock()
    if t is None or isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t):
        raise OrgRefused("clock unknown or non-finite: refusing to write")
    return float(t)


def _agent(conn, aid):
    r = conn.execute("SELECT payload_json FROM agents WHERE id=?", (aid,)).fetchone()
    return None if r is None else json.loads(r[0])


def _put_agent(conn, a, new=False):
    body = S.canonical_json(a).decode("utf-8")
    if new:
        conn.execute("INSERT INTO agents(id, payload_json) VALUES(?,?)", (a["agent_id"], body))
    else:
        conn.execute("UPDATE agents SET payload_json=? WHERE id=?", (body, a["agent_id"]))


def _active(conn):
    return [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM agents WHERE json_extract(payload_json,'$.state')='active'")]


def _check_persona(p):
    for k in ("name", "personality", "backstory"):
        if not isinstance(p.get(k), str) or not p[k].strip():
            raise BadPersona("persona.%s missing" % k)
    if _PERF.search(" ".join(p[k] for k in ("personality", "backstory"))):
        raise BadPersona("persona carries a performance claim; only the stored career record may")


def _record(conn, lim, now, *, kind, by, reason, extra, epoch_kind="discovery", epoch_extra=None):
    """Append the org change, close the open epoch, open the new one. Caller holds the write transaction. -> epoch_id.
    A material change inside an EVALUATED epoch BREAKS it: the epoch is marked broken, a BROKEN row is appended to the trial registry (alpha stays spent; rows are only ever appended) and the next epoch is a
    discovery epoch until the crew freezes a new nominee (prereg: a changed rule is a new epoch)."""
    nxt = (conn.execute("SELECT COALESCE(MAX(seq), 0) FROM org_changes").fetchone()[0]) + 1
    epoch_id = "e%d" % nxt
    S.append(conn, "org_changes", {"kind": kind, "by": by, "reason": reason, "at": now, "epoch_id": epoch_id, **extra})
    for (eid, pj) in conn.execute("SELECT id, payload_json FROM epochs WHERE json_extract(payload_json,'$.closed_at') IS NULL").fetchall():
        e = json.loads(pj)
        e["closed_at"] = now
        if e.get("kind") == "evaluated" and epoch_kind != "evaluated":
            e["status"] = "broken"
            S.append(conn, "trial_registry", {"k": e["k"], "hypothesis_id": e["hypothesis_id"], "alpha_k": e["alpha_k"], "status": "BROKEN", "nominee_config_hash": e["nominee_config_hash"],
                                              "epoch_id": e["epoch_id"], "broken_by_org_change_seq": nxt, "reason": reason})
        conn.execute("UPDATE epochs SET payload_json=? WHERE id=?", (S.canonical_json(e).decode("utf-8"), eid))
    body = {"epoch_id": epoch_id, "opened_at": now, "reason": reason, "org_change_seq": nxt, "closed_at": None, "kind": epoch_kind, **(epoch_extra or {})}
    conn.execute("INSERT INTO epochs(id, payload_json) VALUES(?,?)", (epoch_id, S.canonical_json(body).decode("utf-8")))
    return epoch_id




def hire(conn, lim, *, clock, role, pod, provider, model, family, idempotency_key, persona_fn, policy_hash, prompt_hash, lane=None, lens=None, by="head", reason="hire"):
    _actor(by)
    aid = "ag_" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:16]
    if _agent(conn, aid) is not None:
        return aid
    _now(clock)                                                      # an unknown clock refuses BEFORE a model call is spent
    if len(_active(conn)) + 1 > lim["org_size_ceiling"]:
        raise OrgRefused("org_size_over_ceiling")                    # refused BEFORE the persona model call is spent; re-checked under the lock below
    persona = persona_fn(role, pod)                                  # model call: outside the write lock
    _check_persona(persona)
    with S.write_transaction(conn):
        now = _now(clock)                                            # knowledge/commit time is sampled AFTER the callback returned, under the write lock: times follow commit order
        if _agent(conn, aid) is not None:
            return aid
        if len(_active(conn)) + 1 > lim["org_size_ceiling"]:
            raise OrgRefused("org_size_over_ceiling")
        epoch_id = _record(conn, lim, now, kind="org", by=by, reason=reason, extra={"op": "hire", "agent_id": aid, "role": role, "pod": pod})
        _put_agent(conn, {"agent_id": aid, "role": role, "pod": pod, "provider": provider, "family": family, "model": model, "state": "active", "generation": 1,
                          "policy_hash": policy_hash, "prompt_hash": prompt_hash, "persona_hash": hashlib.sha256(S.canonical_json(persona)).hexdigest(), "persona": persona,
                          "lane": lane, "lens": lens, "hired_epoch": epoch_id, "archived_epoch": None, "created_at": now, "idempotency_key": idempotency_key}, new=True)
    return aid


def fire(conn, lim, *, clock, agent_id, reason, by):
    _actor(by)
    _now(clock)
    with S.write_transaction(conn):
        now = _now(clock)
        a = _agent(conn, agent_id)
        if a is None or a["state"] != "active":
            raise OrgRefused("not_active")
        rest = [x for x in _active(conn) if x["agent_id"] != agent_id]
        if a["role"] in lim["fixed_seats"] and not any(x["role"] == a["role"] for x in rest):
            raise OrgRefused("missing_fixed_seat:%s" % a["role"])         # restaff first: the seat is never left empty
        epoch_id = _record(conn, lim, now, kind="org", by=by, reason=reason, extra={"op": "fire", "agent_id": agent_id, "role": a["role"]})
        a.update(state="archived", generation=a["generation"] + 1, archived_epoch=epoch_id)
        _put_agent(conn, a)
    return epoch_id


_UPDATABLE = {"prompt_hash": "prompt", "persona": "persona", "model": "model"}


def update_agent(conn, lim, *, clock, agent_id, changes, by, reason):
    _actor(by)
    _now(clock)
    bad = set(changes) - set(_UPDATABLE)
    if bad:
        raise OrgRefused("not_updatable:%s" % ",".join(sorted(bad)))
    with S.write_transaction(conn):
        now = _now(clock)
        a = _agent(conn, agent_id)
        if a is None or a["state"] != "active":
            raise OrgRefused("not_active")
        kinds = sorted({_UPDATABLE[k] for k in changes})
        if "persona" in changes:
            _check_persona(changes["persona"])
            a["persona"] = changes["persona"]
            a["persona_hash"] = hashlib.sha256(S.canonical_json(changes["persona"])).hexdigest()
        for k in ("prompt_hash", "model"):
            if k in changes:
                a[k] = changes[k]
        a["generation"] += 1
        epoch_id = _record(conn, lim, now, kind=kinds[0], by=by, reason=reason, extra={"op": "update", "agent_id": agent_id, "fields": sorted(changes), "generation": a["generation"]})
        _put_agent(conn, a)
    return epoch_id


def change_org(conn, lim, *, clock, change, by, reason):
    _actor(by)
    _now(clock)
    opens = O.opens_epoch(lim, change)                                # UnknownChange for an unclassified kind
    if by == "head" and not O.head_may_apply(lim, change):
        raise OrgRefused("head_forbidden:%s" % change["kind"])
    with S.write_transaction(conn):
        now = _now(clock)
        if opens:
            return _record(conn, lim, now, kind=change["kind"], by=by, reason=reason, extra={"op": "change", "detail": change.get("detail")})
        S.append(conn, "org_changes", {"kind": change["kind"], "by": by, "reason": reason, "at": now, "epoch_id": None, "op": "change", "detail": change.get("detail")})
        return None


def accept_answer(conn, agent_id, generation):
    a = _agent(conn, agent_id)
    return a is not None and a["state"] == "active" and a["generation"] == generation


