"""#89 (rest): a wave job's round takes its due times from the floor manager's ETAs (`Round.etas`), never from the crew budget.

Pure folds over `wave.observe` and `arbiter.observe`: no driver, no store, no GitHub.
"""
from __future__ import annotations

from fridica_research import arbiter, wave
from fridica_research.machine import Event
from support import CFG

THREAD = "T1:C1:1791823244.369619"
REPO = "chengcli/fridica-research"
T0 = 1_791_800_000.0
H = 3600.0
HEAD = "d" * 40
IMP, AUD = "U0IMP1", "U0AUD1"


def opened() -> wave.Wave:
    job = {"issue": 89, "files": ["src/fridica_research/arbiter.py"], "implementer": {"slack": IMP, "login": "imp1"},
           "auditor": {"slack": AUD, "login": "aud1"}, "input_at": T0, "pr_eta": T0 + 2 * H, "review_eta": T0 + 5 * H}
    return wave.open_wave(THREAD, name="g", repo=REPO, jobs=[job], now=T0, cfg=CFG)


def polled(now: float, **data) -> Event: return Event("pr_polled", now, {"job": 1, **data})
def pr() -> dict: return {"number": 120, "headRefOid": HEAD, "state": "OPEN", "isDraft": False, "author": {"login": "imp1"}, "body": "Refs #89"}
def review(rid: int, state: str, commit: str = HEAD) -> dict:
    return {"id": str(rid), "n": rid, "login": "aud1", "type": "User", "state": state, "commit": commit, "at": f"2026-10-12T1{rid % 10}:00:00Z", "body": ""}


def test_the_round_carries_the_fm_etas_and_its_due_is_the_pr_eta():
    r = opened().jobs[0].rnd()
    assert r.etas == {"Implement": T0 + 2 * H, "CodeAudit": T0 + 5 * H}
    assert r.due == T0 + 2 * H and r.due != T0 + arbiter.budget(CFG, "Implement")


def test_moving_to_code_audit_takes_the_review_eta_not_the_budget():
    w, _ = wave.observe(opened(), polled(T0 + H, prs=[pr()]), CFG)
    w, _ = wave.observe(w, Event("crew_post", T0 + 1.5 * H, {"ts": "1.1", "sender": IMP, "text": f"PR open, handing off to <@{AUD}>.\nhttps://github.com/{REPO}/pull/120"}), CFG)
    r = w.jobs[0].rnd()
    assert r.stage == "CodeAudit" and r.due == T0 + 5 * H


def test_a_short_track_hand_off_needs_no_due_line():
    w, _ = wave.observe(opened(), polled(T0 + H, prs=[pr()]), CFG)
    w, _ = wave.observe(w, Event("crew_post", T0 + 1.5 * H, {"ts": "1.1", "sender": IMP, "text": f"handing off to <@{AUD}>: https://github.com/{REPO}/pull/120"}), CFG)
    assert w.jobs[0].rnd().stage == "CodeAudit"


def test_changes_requested_rebases_on_the_pr_eta():
    w, _ = wave.observe(opened(), polled(T0 + H, prs=[pr()]), CFG)
    w.jobs[0].round["stage"] = "CodeAudit"
    w, _ = wave.observe(w, polled(T0 + 3 * H, view={"headRefOid": HEAD, "state": "OPEN"}, reviews=[review(1, "CHANGES_REQUESTED")]), CFG)
    r = w.jobs[0].rnd()
    assert r.stage == "Implement" and r.due == T0 + 2 * H


def test_set_eta_reaches_the_round():
    w = wave.set_eta(opened(), 1, pr_eta=T0 + 3 * H, review_eta=T0 + 6 * H)
    r = w.jobs[0].rnd()
    assert r.etas == {"Implement": T0 + 3 * H, "CodeAudit": T0 + 6 * H} and r.due == T0 + 3 * H


def test_an_adopted_round_without_etas_keeps_the_crew_budget():
    roster = {"implementer": {"holders": [IMP], "login": "imp1", "delegate": False}, "auditor": {"holders": [AUD], "login": "aud1", "delegate": False}}
    r = arbiter.Round(thread=THREAD, channel="C1", issue=89, repo=REPO, stage="Implement", roster=roster, pr={"number": 1, "head": HEAD})
    arbiter.move(r, "Implement", "CodeAudit", T0, "k", CFG)
    assert r.etas == {} and r.due == T0 + arbiter.budget(CFG, "CodeAudit")
