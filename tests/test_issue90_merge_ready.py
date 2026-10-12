"""#90 (rest): "ready to merge" waits for an unblocked PR, and a head move in Merge demotes only an unapproved head.

`unblocked`: not CONFLICTING, checks SUCCESS or NONE, not draft; a view missing `mergeable`/`checks` does not block.
"""
from __future__ import annotations

import pytest

from fridica_research import arbiter, config, wave
from fridica_research.machine import Event

THREAD = "T1:C1:1700000000.000100"
REPO = "chengcli/fridica-research"
IMP, AUD = "UIMP", "UAUD"
HEAD, NEW = "a" * 40, "b" * 40
T0 = 1_791_800_000.0
CFG = config.Config()
ETAS = {"Implement": T0 - 3600, "CodeAudit": T0 + 7200}  # a wave job's round: its ready post is gated on `unblocked`


def rnd(stage: str, etas: dict | None = None) -> arbiter.Round:
    roster = {"implementer": {"holders": [IMP], "login": "imp", "delegate": False}, "auditor": {"holders": [AUD], "login": "aud", "delegate": False}}
    return arbiter.Round(thread=THREAD, channel="C1", issue=90, repo=REPO, stage=stage, roster=roster, order=list(wave.JOB_ORDER),
                         etas=dict(etas or {}), pr={"number": 70, "head": HEAD, "state": "OPEN", "draft": False})


def rv(n: int, state: str, commit: str) -> dict:
    return {"id": str(n), "n": n, "state": state, "commit": commit, "login": "aud", "at": f"2026-10-12T1{n % 10}:00:00Z"}


def poll(r, reviews, head=HEAD, now=T0, **view):
    return arbiter.observe(r, Event("pr_polled", now, {"view": {"headRefOid": head, "state": "OPEN", **view}, "reviews": reviews}), CFG)


def ready(actions) -> list[str]: return [a.data["text"] for a in actions if a.kind == "post" and "ready to merge" in a.data["text"]]


@pytest.mark.parametrize("view,ok", [
    ({}, True), ({"mergeable": "MERGEABLE", "checks": "SUCCESS"}, True), ({"mergeable": "UNKNOWN", "checks": "NONE"}, True),
    ({"mergeable": "CONFLICTING", "checks": "SUCCESS"}, False), ({"mergeable": "MERGEABLE", "checks": "FAILURE"}, False),
    ({"mergeable": "MERGEABLE", "checks": "PENDING"}, False), ({"isDraft": True}, False)])
def test_unblocked(view, ok):
    r, _ = poll(rnd("CodeAudit"), [], **view)
    assert arbiter.unblocked(r) is ok


@pytest.mark.parametrize("view", [{"mergeable": "CONFLICTING"}, {"checks": "FAILURE"}, {"checks": "PENDING"}, {"isDraft": True}])
def test_ready_to_merge_is_held_while_blocked_and_sent_once_unblocked(view):
    r, a1 = poll(rnd("CodeAudit", etas=ETAS), [rv(1, "APPROVED", HEAD)], **view)
    assert r.stage == "Merge" and ready(a1) == []
    r, a2 = poll(r, [rv(1, "APPROVED", HEAD)], now=T0 + 60, **view)
    assert ready(a2) == []
    r, a3 = poll(r, [rv(1, "APPROVED", HEAD)], now=T0 + 120, mergeable="MERGEABLE", checks="SUCCESS", isDraft=False)
    assert len(ready(a3)) == 1 and HEAD[:12] in ready(a3)[0]
    r, a4 = poll(r, [rv(1, "APPROVED", HEAD)], now=T0 + 180, mergeable="MERGEABLE", checks="SUCCESS", isDraft=False)
    assert ready(a4) == []


def test_a_view_without_the_fields_clears_an_earlier_block():
    r, _ = poll(rnd("CodeAudit", etas=ETAS), [rv(1, "APPROVED", HEAD)], mergeable="CONFLICTING", checks="FAILURE")
    r, a = poll(r, [rv(1, "APPROVED", HEAD)], now=T0 + 60)
    assert arbiter.unblocked(r) and len(ready(a)) == 1


@pytest.mark.parametrize("view", [{"mergeable": "CONFLICTING"}, {"checks": "FAILURE"}, {"isDraft": True}])
def test_a_blocked_adopted_round_entering_merge_gets_its_ready_post_at_once(view):
    # an adopted round (no `etas`) is polled only in Implement and CodeAudit, so its post goes out on entering Merge, as on main
    r, a1 = poll(rnd("CodeAudit"), [rv(1, "APPROVED", HEAD)], **view)
    assert r.stage == "Merge" and len(ready(a1)) == 1 and HEAD[:12] in ready(a1)[0]
    assert any(p["kind"] == "post" and "ready to merge" in p["text"] for p in r.pending)
    r, a2 = poll(r, [rv(1, "APPROVED", HEAD)], now=T0 + 60, **view)
    assert ready(a2) == []  # once per head, also after a restart reloads the round
    r2 = arbiter.Round.from_json(r.to_json())
    assert ready(arbiter.move(r2, "CodeAudit", "Merge", T0 + 120, "k", CFG)) == []


def test_a_view_without_mergeable_or_checks_posts_at_once():
    r, a = poll(rnd("CodeAudit"), [rv(1, "APPROVED", HEAD)])
    assert len(ready(a)) == 1


def test_head_move_in_merge_without_approval_demotes_to_the_auditor_keeping_the_old_code_audit_eta():
    r, _ = poll(rnd("CodeAudit", etas={"Implement": T0 - 3600, "CodeAudit": T0 + 7200}), [rv(1, "APPROVED", HEAD)])
    assert r.stage == "Merge"
    r, a = poll(r, [rv(1, "APPROVED", HEAD)], head=NEW, now=T0 + 600)
    assert r.stage == "CodeAudit" and r.due == T0 + 7200 and ready(a) == []
    board = [x for x in a if x.kind == "board_update"]
    assert board and board[0].data["assignee"] == "aud"
    assert any(x.kind == "post" and f"<@{AUD}>" in x.data["text"] for x in a)


def test_head_move_in_merge_with_the_new_head_approved_stays_in_merge():
    r, _ = poll(rnd("CodeAudit"), [rv(1, "APPROVED", HEAD)])
    r, a = poll(r, [rv(1, "APPROVED", HEAD), rv(2, "APPROVED", NEW)], head=NEW, now=T0 + 600)
    assert r.stage == "Merge" and len(ready(a)) == 1 and NEW[:12] in ready(a)[0]


def test_merge_ready_in_the_wave_needs_unblocked():
    w = wave.Wave(thread=THREAD, jobs=[wave.Job(issue=90, files=["a"], implementer={}, auditor={})])
    r, _ = poll(rnd("CodeAudit"), [rv(1, "APPROVED", HEAD)], mergeable="CONFLICTING")
    w.jobs[0].round = r.to_dict()
    assert wave.merge_ready(w.jobs[0]) is False
    r, _ = poll(r, [rv(1, "APPROVED", HEAD)], mergeable="MERGEABLE")
    w.jobs[0].round = r.to_dict()
    assert wave.merge_ready(w.jobs[0]) is True
