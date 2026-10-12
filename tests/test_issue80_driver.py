"""#80, #88, #91: deputy wake-ups through instruct, the auditor-only GitHub review nudge, and the candidate PR
(ref fm-wave-h-assign, job 1).

A wake is one `instruct` per ref with the client id from `deputy.client_id`. A SIGN-OFF approve in Slack without
an approving GitHub review on the head nudges the job's auditor once; a sign-off from anyone else nudges nobody.
With `r.pr["candidate"]` set and no PR recorded, the driver views that PR and feeds it as `prs=[view]`.
"""
from __future__ import annotations

from fridica_research import deputy, wave
from test_issue69_wave import H, HEAD, PEOPLE, T0, THREAD
from test_issue69b_driver import approve, stored
from test_issue83_driver import ladder_world, refs

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
