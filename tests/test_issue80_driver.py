"""#80, #88, #91: deputy wake-ups through instruct, the auditor-only GitHub review nudge, and the candidate PR
(ref fm-wave-h-assign, job 1).

A wake is one `instruct` per ref with the client id from `deputy.client_id`. A SIGN-OFF approve in Slack without
an approving GitHub review on the head nudges the job's auditor once; a sign-off from anyone else nudges nobody.
With `r.pr["candidate"]` set and no PR recorded, the driver views that PR and feeds it as `prs=[view]`.
"""
from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

from fridica_research import arbiter, deputy, wave
from fridica_research.driver import Driver
from fridica_research.machine import Action
from support import CFG
from test_issue69_wave import H, HEAD, PEOPLE, T0, THREAD
from test_issue69b_driver import approve, make, stored
from test_issue83_driver import ladder_world, refs
from test_issue88_driver import ReviewGh

MIN = 60.0


def signoff(store, n: int, sender: str, verdict: str = "approve", sha: str = HEAD[:12]):
    w = stored(store)
    pr = w.jobs[n - 1].round["pr"]
    pr["signoffs"] = (pr.get("signoffs") or []) + [{"ts": "1791809900.000001", "sender": sender, "pr": f"#{pr['number']}", "sha": sha, "verdict": verdict}]
    store.set_meta(wave.wave_key(THREAD), w.to_json())


def nudges(client) -> list[str]: return [r for r in refs(client) if r and "/gh-review/" in r]


def test_a_wake_is_one_instruct_with_the_ref_client_id(tmp_path):
    store, client, gh, clock, drv = ladder_world(tmp_path)
    gh.prs = {}
    clock.t = T0 + 2 * H
    drv.run_once()
    clock.t = T0 + 2 * H + 31 * MIN
    for _ in range(3):
        drv.run_once()
        clock.t += MIN
    ids = sorted(cid for _, cid, _ in client.instructs)
    assert ids == sorted(deputy.client_id(f"fm-wave-d-job{n}/escalate") for n in (1, 2, 3))
    assert all(th == THREAD for th, _, _ in client.instructs)


def test_the_auditors_approve_nudges_once(tmp_path):
    store, client, gh, clock, drv = ladder_world(tmp_path)
    drv.run_once()
    signoff(store, 2, PEOPLE["aud2"])
    for _ in range(3):
        clock.t += H
        drv.run_once()
    assert nudges(client) == [f"fm-wave-d-job2/gh-review/{HEAD[:12]}"]
    assert f"<@{PEOPLE['aud2']}>" in [t for _, t in client.posts if "/gh-review/" in t][0]


def test_an_approve_from_anyone_else_nudges_nobody(tmp_path):
    store, client, gh, clock, drv = ladder_world(tmp_path)
    drv.run_once()
    signoff(store, 1, PEOPLE["imp1"])
    signoff(store, 3, PEOPLE["aud1"])  # an auditor, but not job 3's
    clock.t += H
    drv.run_once()
    assert nudges(client) == []


def test_no_nudge_for_changes_or_once_github_approved(tmp_path):
    store, client, gh, clock, drv = ladder_world(tmp_path)
    drv.run_once()
    signoff(store, 1, PEOPLE["aud1"], verdict="changes")
    approve(gh, 72, "aud2", 902)
    clock.t += H
    drv.run_once()
    signoff(store, 2, PEOPLE["aud2"])
    clock.t += H
    drv.run_once()
    assert nudges(client) == []


def test_the_candidate_pr_is_viewed_directly(tmp_path):
    store, client, gh, clock, drv = ladder_world(tmp_path)
    gh.prs.pop(73)
    gh.prs[80] = {"author": "someone-else", "issue": 61, "state": "OPEN", "head": HEAD}
    w = stored(store)
    w.jobs[2].round.setdefault("pr", {})["candidate"] = 80
    store.set_meta(wave.wave_key(THREAD), w.to_json())
    seen = []
    pick = __import__("fridica_research.arbiter", fromlist=["pick_pr"])
    orig = pick.pick_pr
    pick.pick_pr = lambda r, prs, cfg=None: (seen.append((r.issue, prs)), orig(r, prs, cfg))[1]
    try: drv.run_once()
    finally: pick.pick_pr = orig
    assert any(a[:4] == ["gh", "pr", "view", "80"] for a in gh.calls)
    assert not any("--author" in a and "imp3" in a for a in gh.calls)
    fed = [prs for issue, prs in seen if issue == 61]
    assert fed and len(fed[0]) == 1 and fed[0][0]["number"] == 80 and fed[0][0]["headRefOid"] == HEAD


def test_a_recorded_pr_wins_over_the_candidate(tmp_path):
    store, client, gh, clock, drv = ladder_world(tmp_path)
    drv.run_once()
    w = stored(store)
    w.jobs[0].round["pr"]["candidate"] = 99
    store.set_meta(wave.wave_key(THREAD), w.to_json())
    clock.t += H
    drv.run_once()
    assert not any(a[:4] == ["gh", "pr", "view", "99"] for a in gh.calls)


# -- job 1 / job 4 integration (#110): the candidate view carries the adoption fields, eta_text is a text field --------
class CandidateGh(ReviewGh):
    """ReviewGh whose `gh pr view` answers only the `--json` fields asked for, as gh does."""

    def run(self, argv, stdin):
        if argv[:3] == ["gh", "pr", "view"] and "--json" in argv:
            n = int(argv[3])
            p = self.prs[n]
            full = {"number": n, "headRefOid": p["head"], "state": p["state"], "isDraft": False, "author": {"login": p["author"]}, "body": f"Refs #{p['issue']}",
                    "mergedAt": None, "mergeCommit": None, "milestone": None, "mergeable": "MERGEABLE", "statusCheckRollup": []}
            self.calls.append(argv)
            return json.dumps({k: full[k] for k in argv[argv.index("--json") + 1].split(",") if k in full})
        return super().run(argv, stdin)


def candidate_world(tmp_path, author: str):
    store, client, _, clock, _ = ladder_world(tmp_path)
    gh = CandidateGh()
    gh.prs = {71: {"author": "imp1", "issue": 69, "state": "OPEN", "head": HEAD}, 72: {"author": "imp2", "issue": 54, "state": "OPEN", "head": HEAD},
              80: {"author": author, "issue": 61, "state": "OPEN", "head": HEAD}}
    w = stored(store)
    w.jobs[2].round.setdefault("pr", {})["candidate"] = 80
    store.set_meta(wave.wave_key(THREAD), w.to_json())
    return store, client, gh, clock, make(store, client, gh, clock)


def test_the_candidate_view_carries_number_author_and_body(tmp_path):
    store, client, gh, clock, drv = candidate_world(tmp_path, "someone-else")
    ev = drv.read_pr(stored(store).jobs[2].rnd(), gh.run)
    (view,) = ev.get("prs")
    assert view["number"] == 80 and view["author"] == {"login": "someone-else"} and view["body"] == "Refs #61" and view["headRefOid"] == HEAD


def test_the_implementers_own_candidate_is_adopted(tmp_path):
    store, client, gh, clock, drv = candidate_world(tmp_path, "imp3")
    drv.run_once()
    assert stored(store).jobs[2].rnd().pr.get("number") == 80


class DatesApi:
    """The board api with `Board.set_dates`'s own signature: an unknown keyword raises TypeError, as the real one does."""

    def __init__(self): self.writes: list[tuple] = []
    def set_dates(self, issue, started=None, projected_finish=None, finished=None): self.writes.append(("dates", issue, started, projected_finish, finished))
    def set_text(self, issue, field, value): self.writes.append(("text", issue, field, value))
    def set_option(self, issue, field, option): self.writes.append(("option", issue, field, option))
    def edit_issue(self, issue, *args): self.writes.append(("edit", issue, *args))


def board_driver(tmp_path):
    store, client, gh, clock, _ = ladder_world(tmp_path)
    api = DatesApi()
    cfg = dataclasses.replace(CFG, board=dataclasses.replace(CFG.board, enabled=True))
    drv = Driver(cfg, client, store, llm=lambda *a: {}, clock=clock, sleep=lambda s: None, github=SimpleNamespace(run=gh.run, poll=lambda *a: None), board=SimpleNamespace(api=api))
    return api, drv


def wave_board_action(eta_text: bool = True) -> Action:
    """A board write as #110's `board_action` builds it for a wave job: `eta_text` = `eta_line(due)` inside `dates`."""
    dates = {"started": "2026-10-12", "projected_finish": "2026-10-12", **({"eta_text": arbiter.eta_line(T0 + 2 * H)} if eta_text else {})}
    return Action("board_update", f"{THREAD}/round/Implement-CodeAudit/board/1", {"thread": THREAD, "issue": 69, "repo": "chengcli/fridica-research", "options": {}, "dates": dates})


def test_eta_text_is_written_as_a_text_field_and_the_dates_still_go_in(tmp_path, caplog):
    api, drv = board_driver(tmp_path)
    a = wave_board_action()
    drv.board_fields(a)
    assert ("dates", 69, "2026-10-12", "2026-10-12", None) in api.writes
    assert ("text", 69, "ETA", arbiter.eta_line(T0 + 2 * H)) in api.writes
    assert "eta_text" in a["dates"] and "failed" not in caplog.text  # the action itself is not changed: a retry writes the same fields


def test_dates_without_eta_text_write_no_eta_field(tmp_path):
    api, drv = board_driver(tmp_path)
    drv.board_fields(wave_board_action(eta_text=False))
    assert [w for w in api.writes if w[0] == "text"] == [] and ("dates", 69, "2026-10-12", "2026-10-12", None) in api.writes


# -- Xi's finding on 1ddd7d7: a candidate whose view fails must not block adoption ------------------------------------
class FailingCandidateGh(CandidateGh):
    """CandidateGh where viewing a PR it does not have fails as gh does; `view_error` replaces that message when set."""

    def __init__(self):
        super().__init__()
        self.view_error = ""

    def run(self, argv, stdin):
        if argv[:3] == ["gh", "pr", "view"] and int(argv[3]) not in self.prs:
            self.calls.append(argv)
            raise RuntimeError(self.view_error or f"GraphQL: Could not resolve to a PullRequest with the number of {argv[3]}. (repository.pullRequest)")
        return super().run(argv, stdin)


def failing_candidate_world(tmp_path, view_error: str = ""):
    store, client, _, clock, _ = ladder_world(tmp_path)
    gh = FailingCandidateGh()
    gh.view_error = view_error
    gh.prs = {71: {"author": "imp1", "issue": 69, "state": "OPEN", "head": HEAD}, 72: {"author": "imp2", "issue": 54, "state": "OPEN", "head": HEAD},
              73: {"author": "imp3", "issue": 61, "state": "OPEN", "head": HEAD}}
    w = stored(store)
    w.jobs[2].round.setdefault("pr", {})["candidate"] = 999
    store.set_meta(wave.wave_key(THREAD), w.to_json())
    return store, client, gh, clock, make(store, client, gh, clock)


def author_listings(gh, login: str) -> int: return sum(1 for a in gh.calls if a[:3] == ["gh", "pr", "list"] and "--author" in a and login in a)


def test_a_missing_candidate_is_dropped_and_the_implementers_pr_adopted(tmp_path, caplog):
    store, client, gh, clock, drv = failing_candidate_world(tmp_path)
    for _ in range(4):
        drv.run_once()
        clock.t += H
    pr = stored(store).jobs[2].rnd().pr
    assert pr.get("number") == 73 and "candidate" not in pr and author_listings(gh, "imp3") >= 1
    assert sum(1 for a in gh.calls if a[:4] == ["gh", "pr", "view", "999"]) == 1  # dropped after the first not-found
    assert drv.gh_failures.get(THREAD) is None and not any("GitHub reads failing" in t for _, t in client.posts)
    assert "candidate" in caplog.text and "999" in caplog.text


def test_another_candidate_error_is_logged_and_list_prs_runs_that_pass(tmp_path, caplog):
    store, client, gh, clock, drv = failing_candidate_world(tmp_path, view_error="HTTP 502: Bad Gateway")
    drv.run_once()
    pr = stored(store).jobs[2].rnd().pr
    assert author_listings(gh, "imp3") == 1 and pr.get("number") == 73
    assert "HTTP 502" in caplog.text and "999" in caplog.text
