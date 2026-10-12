"""#92 (rest): a wave job's blocking findings become one post each; an unrouted post is answered only when it is a hand-off.

Adopted rounds (no `etas`) keep their findings silent, as before.
"""
from __future__ import annotations

from fridica_research import arbiter, wave
from fridica_research.machine import Event
from support import CFG

THREAD = "T1:C1:1791823244.369619"
REPO = "chengcli/fridica-research"
T0 = 1_791_800_000.0
H = 3600.0
HEAD = "e" * 40
P = {"imp1": "U0IMP1", "aud1": "U0AUD1", "imp2": "U0IMP2", "aud2": "U0AUD2"}


def job(issue, f, imp, aud):
    return {"issue": issue, "files": [f], "implementer": {"slack": P[imp], "login": imp}, "auditor": {"slack": P[aud], "login": aud},
            "input_at": T0, "pr_eta": T0 + 2 * H, "review_eta": T0 + 4 * H}


def opened() -> wave.Wave:
    return wave.open_wave(THREAD, name="g", repo=REPO, jobs=[job(89, "a.py", "imp1", "aud1"), job(90, "b.py", "imp2", "aud2")], now=T0, cfg=CFG)


def post(ts, sender, text, now=T0 + H) -> Event: return Event("crew_post", now, {"ts": ts, "sender": sender, "text": text})
def posts(actions) -> list: return [a for a in actions if a.kind == "post"]


def test_a_refused_hand_off_in_a_wave_job_is_one_post_to_its_author():
    w, a = wave.observe(opened(), post("1.1", P["aud1"], f"handing off to <@{P['imp1']}> https://x/y ref: fm-wave-g-job1"), CFG)
    assert len(posts(a)) == 1 and f"<@{P['aud1']}>" in posts(a)[0].data["text"] and "ref: " in posts(a)[0].data["text"]
    assert posts(a)[0].data["thread"] == THREAD
    r = w.jobs[0].rnd()
    assert r.findings and any(p["kind"] == "post" for p in r.pending)
    w, a2 = wave.observe(w, post("1.1", P["aud1"], "same ts again"), CFG)
    assert a2 == []


def test_a_held_hand_off_in_a_wave_job_is_one_post():
    w, a = wave.observe(opened(), post("1.2", P["imp1"], f"handing off to <@{P['aud1']}> https://x/y\nref: fm-wave-g-job1"), CFG)
    assert w.jobs[0].rnd().held.get("Implement") and len(posts(a)) == 1 and "no PR" in posts(a)[0].data["text"]


def test_a_pr_closed_without_merging_is_one_post():
    w, _ = wave.observe(opened(), Event("pr_polled", T0 + H, {"job": 1, "prs": [{"number": 7, "headRefOid": HEAD, "state": "OPEN", "author": {"login": "imp1"}, "body": "Refs #89"}]}), CFG)
    w, a = wave.observe(w, Event("pr_polled", T0 + 2 * H, {"job": 1, "view": {"headRefOid": HEAD, "state": "CLOSED"}}), CFG)
    assert len(posts(a)) == 1 and "#7" in posts(a)[0].data["text"] and f"<@{P['imp1']}>" in posts(a)[0].data["text"]


def test_an_unrouted_hand_off_gets_one_reply_asking_for_the_ref_line():
    w = opened()
    w.jobs[1].implementer = {"slack": P["imp1"], "login": "imp1"}  # one person implements both jobs: role routing is ambiguous
    w, a = wave.observe(w, post("2.1", P["imp1"], "PR is up, handing off to the auditor: https://example.org/z"), CFG)
    assert len(posts(a)) == 1 and "ref: fm-wave-g-job" in posts(a)[0].data["text"] and f"<@{P['imp1']}>" in posts(a)[0].data["text"]
    assert w.findings and w.pending and w.pending[0]["kind"] == "post"


def test_unrouted_plain_chat_stays_a_finding():
    w, a = wave.observe(opened(), post("3.1", "USTRANGER", "Drive-by comment with no job."), CFG)
    assert a == [] and len(w.findings) == 1 and w.pending == []


def test_an_adopted_round_keeps_its_findings_silent():
    roster = {"implementer": {"holders": ["UIMP"], "login": "imp", "delegate": False}, "auditor": {"holders": ["UAUD"], "login": "aud", "delegate": False}}
    r = arbiter.Round(thread=THREAD, channel="C1", issue=92, repo=REPO, stage="Implement", order=list(wave.JOB_ORDER), roster=roster)
    r, a = arbiter.observe(r, Event("crew_post", T0, {"ts": "4.1", "sender": "UAUD", "text": "handing off to <@UIMP> https://x/y\nDue 10-12 12:00 ET"}), CFG)
    assert a == [] and r.findings and r.pending == []
