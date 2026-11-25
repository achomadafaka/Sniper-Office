"""Roster persistence: hire / fire / update / change_org / epochs on the real Office store. Persona generation is an injected callable (no model call here)."""
import hashlib
import json
from pathlib import Path

import pytest

from sniper_office import org as O
from sniper_office import roster as R
from sniper_office import store as S

C = Path(__file__).resolve().parents[1] / "config"


def sha(n):
    return hashlib.sha256((C / n).read_bytes()).hexdigest()


@pytest.fixture
def env(tmp_path, monkeypatch):
    conn = S.open_store(tmp_path / "office.db")
    lim = O.load_limits(C / "limits.example.json", sha("limits.example.json"))
    t = [1000.0]
    clock = lambda: t[0]                                     # noqa: E731
    yield conn, lim, clock, t
    conn.close()


def persona(role, pod):
    return {"name": "Mara Voss", "personality": "dry, exacting, hates round numbers", "backstory": "ten years of reading filings"}


def hire(env, key="k1", role="Researcher", pod="pod1", model="claude-sonnet", provider="claude_sdk", family="claude", fn=persona, **kw):
    conn, lim, clock, _ = env
    return R.hire(conn, lim, clock=clock, role=role, pod=pod, provider=provider, model=model, family=family, idempotency_key=key, persona_fn=fn, policy_hash="p" * 64, prompt_hash="q" * 64, **kw)


def agent(conn, aid):
    return json.loads(conn.execute("SELECT payload_json FROM agents WHERE id=?", (aid,)).fetchone()[0])


def test_hire_is_idempotent_after_a_crash_and_does_not_regenerate_the_persona(env):
    conn = env[0]
    calls = []
    first = hire(env, fn=lambda r, p: calls.append(1) or persona(r, p))
    second = hire(env, fn=lambda r, p: calls.append(1) or persona(r, p))
    assert first == second and len(calls) == 1
    assert conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 1 and conn.execute("SELECT COUNT(*) FROM org_changes").fetchone()[0] == 1


def test_a_persona_with_performance_claims_is_refused_and_nothing_is_written(env):
    conn = env[0]
    bad = lambda r, p: {"name": "X", "personality": "p", "backstory": "closed 400% in 3 months, 80% win rate"}   # noqa: E731
    with pytest.raises(R.BadPersona):
        hire(env, fn=bad)
    assert conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 0 and conn.execute("SELECT COUNT(*) FROM org_changes").fetchone()[0] == 0


def test_hire_over_the_org_ceiling_is_refused(env):
    conn, lim, clock, _ = env
    lim = dict(lim, org_size_ceiling=2)
    hire((conn, lim, clock, None), key="a")
    hire((conn, lim, clock, None), key="b")
    with pytest.raises(R.OrgRefused, match="org_size_over_ceiling"):
        hire((conn, lim, clock, None), key="c")


def test_the_last_risk_or_postmortem_cannot_be_fired_but_can_be_restaffed_first(env):
    conn, lim, clock, _ = env
    r1 = hire(env, key="r1", role="Risk", pod=None)
    with pytest.raises(R.OrgRefused, match="missing_fixed_seat:Risk"):
        R.fire(conn, lim, clock=clock, agent_id=r1, reason="underperforming", by="head")
    hire(env, key="r2", role="Risk", pod=None)
    R.fire(conn, lim, clock=clock, agent_id=r1, reason="restaffed", by="head")
    a = agent(conn, r1)
    assert a["state"] == "archived" and a["generation"] == 2 and a["archived_epoch"]


def test_a_late_answer_from_a_fired_or_replaced_generation_is_rejected(env):
    conn, lim, clock, _ = env
    aid = hire(env)
    assert R.accept_answer(conn, aid, 1) is True
    R.update_agent(conn, lim, clock=clock, agent_id=aid, changes={"prompt_hash": "n" * 64}, by="head", reason="rewrite prompt")
    assert agent(conn, aid)["generation"] == 2 and R.accept_answer(conn, aid, 1) is False and R.accept_answer(conn, aid, 2) is True
    hire(env, key="x", role="Risk", pod=None)
    hire(env, key="y", role="Risk", pod=None)
    R.fire(conn, lim, clock=clock, agent_id=aid, reason="x", by="head")
    assert R.accept_answer(conn, aid, 2) is False                           # archived: no answer is accepted


def test_the_head_cannot_change_guards_or_tools_and_nothing_is_written(env):
    conn, lim, clock, _ = env
    before = conn.execute("SELECT COUNT(*) FROM org_changes").fetchone()[0]
    for kind in ("guards", "tools", "scoring", "live_authority"):
        with pytest.raises(R.OrgRefused, match="head_forbidden"):
            R.change_org(conn, lim, clock=clock, change={"kind": kind, "detail": "x"}, by="head", reason="r")
    assert conn.execute("SELECT COUNT(*) FROM org_changes").fetchone()[0] == before
    R.change_org(conn, lim, clock=clock, change={"kind": "scoring", "detail": "crew edit"}, by="crew", reason="crew-only change")   # the crew can; it opens an epoch too


def test_every_material_change_closes_the_previous_epoch_and_links_to_its_org_change(env):
    conn, lim, clock, t = env
    hire(env, key="a")
    t[0] += 10
    R.change_org(conn, lim, clock=clock, change={"kind": "persona", "detail": "d"}, by="head", reason="r")
    eps = [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM epochs ORDER BY id")]
    assert len(eps) == 2 and sum(1 for e in eps if e["closed_at"] is None) == 1 and eps[0]["closed_at"] == 1010.0
    assert [e["org_change_seq"] for e in eps] == [1, 2]


def test_an_unknown_clock_or_change_kind_refuses_and_writes_nothing(env):
    conn, lim, _, _ = env
    with pytest.raises(R.OrgRefused, match="clock"):
        R.change_org(conn, lim, clock=lambda: None, change={"kind": "persona"}, by="head", reason="r")
    with pytest.raises(O.UnknownChange):
        R.change_org(conn, lim, clock=lambda: 5.0, change={"kind": "mystery"}, by="head", reason="r")
    assert conn.execute("SELECT COUNT(*) FROM org_changes").fetchone()[0] == 0


def registry(conn):
    return [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM trial_registry ORDER BY seq")]


def epochs(conn):
    return [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM epochs ORDER BY CAST(substr(id,2) AS INTEGER)")]


def open_eval(env, k=1):
    conn, lim, clock, _ = env
    from .epoch_fixture import open_epoch
    root = Path(conn.execute('PRAGMA database_list').fetchone()[2]).parent/'nominee'
    return open_epoch(conn, lim, clock, root, k=k)


def untouched(conn):
    return all(conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] == 0 for t in ("trial_registry", "epochs", "org_changes"))


def test_hire_stamps_created_at_and_epoch_AFTER_the_persona_callback_returns(env):
    conn, lim, clock, t = env

    def slow_persona(role, pod):
        t[0] = 1010.0                                                        # the model answered 10 s later
        return persona(role, pod)

    aid = hire(env, fn=slow_persona)
    assert agent(conn, aid)["created_at"] == 1010.0
    assert json.loads(conn.execute("SELECT payload_json FROM epochs").fetchone()[0])["opened_at"] == 1010.0
    assert json.loads(conn.execute("SELECT payload_json FROM org_changes").fetchone()[0])["at"] == 1010.0


def test_an_unknown_clock_is_refused_before_the_persona_model_call_is_spent(env):
    conn, lim, _, _ = env
    calls = []
    with pytest.raises(R.OrgRefused, match="clock"):
        R.hire(conn, lim, clock=lambda: None, role="Researcher", pod="pod1", provider="claude_sdk", model="m", family="claude", idempotency_key="k9",
               persona_fn=lambda r, p: calls.append(1) or persona(r, p), policy_hash="p" * 64, prompt_hash="q" * 64)
    assert calls == []


def test_org_change_times_follow_commit_order(env):
    conn, lim, clock, t = env
    hire(env, key="a")
    t[0] = 1005.0
    R.change_org(conn, lim, clock=clock, change={"kind": "persona", "detail": "d"}, by="head", reason="r")
    ats = [json.loads(r[0])["at"] for r in conn.execute("SELECT payload_json FROM org_changes ORDER BY seq")]
    assert ats == sorted(ats)


