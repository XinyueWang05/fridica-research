"""#91, #90, #89 leftovers (wave H job 4): a PR linked in a post becomes the adoption candidate, with one post when it
cannot be adopted; R23 hygiene on a recorded PR; the approval time tied to its head; the ETA as text for the board.

Pure folds over `arbiter.observe`: no driver, no store, no GitHub.
"""
from __future__ import annotations

from fridica_research import arbiter, config
from fridica_research.machine import Event

THREAD = "T1:C1:1700000000.000100"
REPO = "chengcli/fridica-research"
IMP, AUD = "UIMP", "UAUD"
HEAD, NEW = "a" * 40, "b" * 40
T0 = 1_791_800_000.0
GOOD = "Closes #91\nStudy thread: 1700000000.000100\nBoard: https://github.com/orgs/x/projects/8\nMilestone: R2"


def rnd(stage: str = "Implement", etas: bool = True, pr: dict | None = None) -> arbiter.Round:
    roster = {"implementer": {"holders": [IMP], "login": "imp", "delegate": False},
              "auditor": {"holders": [AUD], "login": "aud", "delegate": False}}
    return arbiter.Round(thread=THREAD, channel="C1", issue=91, repo=REPO, stage=stage, roster=roster, pr=dict(pr or {}),
                         order=["Implement", "CodeAudit", "Merge"], etas={"Implement": T0 + 3600, "CodeAudit": T0 + 7200} if etas else {})


def post(r, text, ts="1791800000.000200", sender=IMP):
    return arbiter.observe(r, Event("crew_post", T0, {"ts": ts, "sender": sender, "text": text}), config.Config())


def poll(r, **data):
    return arbiter.observe(r, Event("pr_polled", T0, data), config.Config())


def pr(n=95, login="imp", body=GOOD, state="OPEN", head=HEAD) -> dict:
    return {"number": n, "headRefOid": head, "state": state, "isDraft": False, "author": {"login": login}, "body": body}


def texts(actions) -> list[str]: return [a.data["text"] for a in actions if a.kind == "post"]


def rv(n, state, commit, at="2026-10-12T18:30:00Z", login="aud") -> dict:
    return {"id": str(n), "n": n, "state": state, "commit": commit, "login": login, "at": at}


# -- (1) a linked PR is the candidate -------------------------------------------------------
def test_a_pr_link_in_a_post_sets_the_candidate():
    r, _ = post(rnd(), f"PR is up: https://github.com/{REPO}/pull/95")
    assert r.pr["candidate"] == 95


def test_a_link_to_another_repo_or_an_issue_is_not_a_candidate():
    r, _ = post(rnd(), f"see https://github.com/other/repo/pull/95 and https://github.com/{REPO}/issues/91")
    assert "candidate" not in r.pr


def test_a_recorded_pr_is_not_replaced_by_a_link():
    r, _ = post(rnd(pr={"number": 70, "head": HEAD, "state": "OPEN"}), f"https://github.com/{REPO}/pull/95")
    assert r.pr["number"] == 70 and "candidate" not in r.pr


def test_an_adoptable_candidate_is_recorded_and_the_candidate_cleared():
    r, _ = post(rnd(), f"https://github.com/{REPO}/pull/95")
    r, actions = poll(r, prs=[pr()])
    assert r.pr["number"] == 95 and "candidate" not in r.pr and texts(actions) == []


def test_a_candidate_by_another_author_gets_one_post_saying_why():
    r, _ = post(rnd(), f"https://github.com/{REPO}/pull/95")
    r, a1 = poll(r, prs=[pr(login="someone")])
    r, a2 = poll(r, prs=[pr(login="someone")])
    assert not r.pr.get("number") and len(texts(a1)) == 1 and texts(a2) == []
    assert "#95" in texts(a1)[0] and "author" in texts(a1)[0] and "someone" in texts(a1)[0]


def test_a_candidate_without_a_closing_keyword_gets_one_post_saying_why():
    r, _ = post(rnd(), f"https://github.com/{REPO}/pull/95")
    r, a1 = poll(r, prs=[pr(body="no issue named")])
    assert not r.pr.get("number") and len(texts(a1)) == 1 and "#91" in texts(a1)[0] and "keyword" in texts(a1)[0].lower()


def test_an_adopted_round_keeps_the_reason_as_a_finding_only():
    r, _ = post(rnd(etas=False), f"https://github.com/{REPO}/pull/95")
    r, a1 = poll(r, prs=[pr(login="someone")])
    assert texts(a1) == [] and any("#95" in f for f in r.findings)


# -- (2) R23 hygiene on a recorded PR -------------------------------------------------------
def test_a_recorded_pr_missing_hygiene_lines_gets_one_post():
    r, a1 = poll(rnd(), prs=[pr(body="Refs #91")])
    r, a2 = poll(r, view={"headRefOid": HEAD, "state": "OPEN", "body": "Refs #91"})
    assert r.pr["number"] == 95 and len(texts(a1)) == 1 and texts(a2) == []
    assert "milestone" in texts(a1)[0] and "board link" in texts(a1)[0]


def test_a_hygienic_pr_gets_no_post():
    r, a1 = poll(rnd(), prs=[pr()])
    assert texts(a1) == []


def test_a_missing_body_field_is_no_gap():
    p = pr()
    p.pop("body")
    r, a1 = poll(rnd(pr={"number": 95, "head": HEAD, "state": "OPEN"}), view={"headRefOid": HEAD, "state": "OPEN"})
    assert texts(a1) == [] and not any("hygiene" in f for f in r.findings)


# -- (3) approved_at is the approving review's submitted_at, tied to its head ----------------
def test_approved_at_is_the_review_submitted_at_not_the_poll_time():
    r, _ = poll(rnd("CodeAudit", pr={"number": 95, "head": HEAD, "state": "OPEN"}),
                view={"headRefOid": HEAD, "state": "OPEN"}, reviews=[rv(1, "APPROVED", HEAD, "2026-10-12T18:30:00Z")])
    assert r.pr["approved_at"] == 1791829800.0 and r.pr["approved_at"] != T0


def test_approved_at_is_dropped_when_the_head_moves():
    r, _ = poll(rnd("CodeAudit", pr={"number": 95, "head": HEAD, "state": "OPEN"}),
                view={"headRefOid": HEAD, "state": "OPEN"}, reviews=[rv(1, "APPROVED", HEAD)])
    r, _ = poll(r, view={"headRefOid": NEW, "state": "OPEN"}, reviews=[rv(1, "APPROVED", HEAD)])
    assert "approved_at" not in r.pr


def test_approved_at_follows_the_approval_of_the_new_head():
    r, _ = poll(rnd("CodeAudit", pr={"number": 95, "head": NEW, "state": "OPEN"}), view={"headRefOid": NEW, "state": "OPEN"},
                reviews=[rv(1, "APPROVED", HEAD, "2026-10-12T18:00:00Z"), rv(2, "APPROVED", NEW, "2026-10-12T18:30:00Z")])
    assert r.pr["approved_at"] == 1791829800.0


def test_a_review_without_submitted_at_sets_no_approved_at():
    review = rv(1, "APPROVED", HEAD)
    review.pop("at")
    review["at"] = ""
    r, _ = poll(rnd("CodeAudit", pr={"number": 95, "head": HEAD, "state": "OPEN"}), view={"headRefOid": HEAD, "state": "OPEN"}, reviews=[review])
    assert "approved_at" not in r.pr


# -- (4) eta_text in the board's dates, for wave jobs only -----------------------------------
def board(actions) -> dict: return next(a.data for a in actions if a.kind == "board_update")


def test_a_wave_job_board_write_carries_eta_text():
    r, actions = poll(rnd(pr={"number": 95, "head": HEAD, "state": "OPEN"}), view={"headRefOid": HEAD, "state": "OPEN"},
                      reviews=[rv(1, "APPROVED", HEAD)])
    assert r.stage == "Merge" and "eta_text" in board(actions)["dates"]


def test_the_eta_text_is_the_round_due_in_et():
    r = rnd(pr={"number": 95, "head": HEAD, "state": "OPEN"})
    r.held["Implement"] = {"ts": "1.1", "sender": IMP, "text": f"handing off to the auditor https://github.com/{REPO}/pull/95"}
    r, actions = poll(r, view={"headRefOid": HEAD, "state": "OPEN"})
    assert r.stage == "CodeAudit" and board(actions)["dates"]["eta_text"] == arbiter.eta_line(T0 + 7200)


def test_an_adopted_round_board_write_has_no_eta_text():
    r = rnd(etas=False, pr={"number": 95, "head": HEAD, "state": "OPEN"})
    r, actions = poll(r, view={"headRefOid": HEAD, "state": "OPEN"}, reviews=[rv(1, "APPROVED", HEAD)])
    assert "eta_text" not in board(actions)["dates"]
