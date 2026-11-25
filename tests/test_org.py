import hashlib
import json
from pathlib import Path

import pytest

from sniper_office import org as O

C = Path(__file__).resolve().parents[1] / "config"
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
ORG = lambda: O.load_org(C / "org.example.json", sha(C / "org.example.json"))
LIM = lambda: O.load_limits(C / "limits.example.json", sha(C / "limits.example.json"))


def test_configs_are_pinned_by_hash(tmp_path):
    other = tmp_path / "org.json"
    other.write_text(json.dumps(dict(ORG(), pods=3)))
    with pytest.raises(O.ConfigMismatch):
        O.load_org(other, sha(C / "org.example.json"))


def test_expand_gives_every_pod_the_template_and_covers_every_attribute_value_and_family():
    seats = O.expand(ORG(), LIM())
    assert len(seats) == 3 + 2 * (2 + 3) and O.validate(seats, LIM()) == []
    researchers = [s for s in seats if s["role"] == "Researcher"]
    assert {s["lane"] for s in researchers} == {"news", "filings", "forums", "data"}
    for pod in ("pod1", "pod2"):
        assert {s["family"] for s in seats if s["pod"] == pod and s["role"] == "Reviewer"} == {"claude", "gpt"}
    assert all("lane" not in s for s in seats if s["role"] != "Researcher")
    assert [s for s in seats if s["role"] == "Head"] == [{"role": "Head", "pod": None, "family": "claude", "tier": "large"}]


def test_pod_count_outside_limits_is_refused():
    for pods in (1, 5):
        with pytest.raises(O.BadOrg):
            O.expand(dict(ORG(), pods=pods), LIM())


def test_fixed_seats_ceiling_role_minimum_and_family_cover_are_each_reported():
    seats = O.expand(ORG(), LIM())
    no_risk = [s for s in seats if s["role"] != "Risk"]
    assert O.validate(no_risk, LIM()) == ["missing_fixed_seat:Risk"]
    assert O.validate(no_risk, LIM(), deterministic=("Risk",)) == []          # a deterministic service fills the seat and is not counted as an agent
    assert O.validate(seats, dict(LIM(), org_size_ceiling=12)) == ["org_size_over_ceiling"]
    few = [s for s in seats if s["role"] != "Reviewer"] + [s for s in seats if s["role"] == "Reviewer"][:2]
    assert "too_few:Reviewer" in O.validate(few, LIM())
    one_family = [dict(s, family="claude") if s["role"] == "Reviewer" else s for s in seats]
    assert O.validate(one_family, LIM()) == ["missing_family:Reviewer:gpt"]


def test_the_head_cannot_apply_a_forbidden_kind_and_an_unclassified_kind_is_refused():
    lim = LIM()
    assert O.head_may_apply(lim, {"kind": "persona"}) and not O.head_may_apply(lim, {"kind": "tools"})
    assert O.opens_epoch(lim, {"kind": "hire"}) and O.opens_epoch(lim, {"kind": "tools"}) and not O.opens_epoch(lim, {"kind": "heartbeat"})
    with pytest.raises(O.UnknownChange):
        O.opens_epoch(lim, {"kind": "something_new"})
