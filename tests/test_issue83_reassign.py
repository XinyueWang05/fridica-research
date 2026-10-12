"""#83 #93 (ref fm-wave-h-assign, job 2): `wave.reassign`, `fm-cmd:` lines, ETA text, and ladder ids per holder and ETA.

`reassign(wave, n, role, person, eta, now=, cfg=)` moves a role to a new person: the first ETA is kept for scoring and
the new one is a re-estimate; it refuses the owner, the person who already holds that role, and the job's other role.
Owner posts with `fm-cmd: reassign|eta|drop|bind-pr` lines apply each line on its own; failures get at most one reply
per post, a fully successful post gets none, and `fm-cmd:` from anyone but the owner is ignored. A bare `HH:MM ET` is its
next occurrence; `MM-DD HH:MM ET`, ISO and epoch also parse. Ladder ids carry the holder and the ETA, so a new holder or
a new ETA runs every rung again.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from fridica_research import ladder, wave
from fridica_research.machine import Event
from support import CFG, OWNER

THREAD = "T1:C1:1791829472.420249"
REPO = "chengcli/fridica-research"
ET = ZoneInfo("America/New_York")
T0 = datetime(2026, 10, 12, 14, 30, tzinfo=ET).timestamp()  # 14:30 ET
H = 3600.0


def et(month: int, day: int, hh: int, mm: int) -> float: return datetime(2026, month, day, hh, mm, tzinfo=ET).timestamp()


def job(issue: int, path: str, imp: str, aud: str) -> dict:
    return {"issue": issue, "files": [path], "implementer": {"slack": f"U0{imp.upper()}", "login": imp}, "auditor": {"slack": f"U0{aud.upper()}", "login": aud},
            "pr_eta": T0 + H, "review_eta": T0 + 2 * H}


def opened() -> wave.Wave:
    return wave.open_wave(THREAD, name="h", repo=REPO, jobs=[job(83, "src/wave.py", "imp1", "aud1"), job(93, "src/ladder.py", "imp2", "aud2")], now=T0, cfg=CFG)


def post(text: str, ts: str, sender: str = OWNER, now: float = T0) -> Event:
    return Event("crew_post", now, {"text": text, "sender": sender, "ts": ts})


# -- ETA text -------------------------------------------------------------------------------
def test_bare_hhmm_et_is_its_next_occurrence():
    assert wave.parse_time("15:05 ET", now=T0) == et(10, 12, 15, 5)
    assert wave.parse_time("14:00 ET", now=T0) == et(10, 13, 14, 0)  # already past today: tomorrow


def test_month_day_iso_and_epoch_parse():
    assert wave.parse_time("10-13 09:15 ET", now=T0) == et(10, 13, 9, 15)
    assert wave.parse_time("2026-10-12T19:00:00Z", now=T0) == datetime.fromisoformat("2026-10-12T19:00:00+00:00").timestamp()
    assert wave.parse_time(str(int(T0)), now=T0) == float(int(T0))
    with pytest.raises(wave.WaveError): wave.parse_time("soon", now=T0)


# -- reassign -------------------------------------------------------------------------------
def test_reassign_keeps_the_first_eta_and_records_the_new_holder():
    w = wave.reassign(opened(), 1, "implementer", {"slack": "U0NEW", "login": "new"}, "16:00 ET", now=T0 + H, cfg=CFG)
    j = w.jobs[0]
    assert j.person("implementer") == {"slack": "U0NEW", "login": "new"}
    assert j.pr_eta == et(10, 12, 16, 0) and j.first_etas["pr_eta"] == T0 + H
    assert j.round["roster"]["implementer"]["holders"] == ["U0NEW"] and j.round["roster"]["implementer"]["login"] == "new"
    assert j.round["etas"]["Implement"] == et(10, 12, 16, 0) and j.round["due"] == et(10, 12, 16, 0)
    assert opened().jobs[0].person("implementer")["slack"] == "U0IMP1"  # pure


def test_reassign_of_the_auditor_moves_the_review_eta():
    w = wave.reassign(opened(), 2, "auditor", {"slack": "U0NEW", "login": "new"}, T0 + 3 * H, now=T0, cfg=CFG)
    assert w.jobs[1].review_eta == T0 + 3 * H and w.jobs[1].first_etas["review_eta"] == T0 + 2 * H and w.jobs[1].pr_eta == T0 + H


@pytest.mark.parametrize("who", [{"slack": OWNER, "login": "owner"}, {"slack": "U0IMP1", "login": "imp1"}, {"slack": "U0AUD1", "login": "aud1"}, {"slack": "U0X", "login": "AUD1"}])
def test_reassign_refuses_the_owner_the_holder_and_the_other_role(who):
    with pytest.raises(wave.WaveError): wave.reassign(opened(), 1, "implementer", who, T0 + 3 * H, now=T0, cfg=CFG)


def test_reassign_refuses_a_terminal_job_and_a_bad_role():
    w, _ = wave.drop_job(opened(), 1, "descoped", now=T0)
    with pytest.raises(wave.WaveError): wave.reassign(w, 1, "implementer", {"slack": "U0NEW", "login": "new"}, T0 + 3 * H, now=T0, cfg=CFG)
    with pytest.raises(wave.WaveError): wave.reassign(opened(), 1, "merger", {"slack": "U0NEW", "login": "new"}, T0 + 3 * H, now=T0, cfg=CFG)


# -- fm-cmd ---------------------------------------------------------------------------------
def test_owner_fm_cmd_lines_apply_and_post_nothing():
    text = "Rulings:\nfm-cmd: reassign job1 implementer U0NEW:new 16:00 ET\nfm-cmd: eta job2 review 17:30 ET\nfm-cmd: bind-pr job2 112\n"
    w, actions = wave.observe(opened(), post(text, "1.1"), CFG)
    assert actions == [] and w.pending == []
    assert w.jobs[0].person("implementer")["slack"] == "U0NEW" and w.jobs[0].first_etas["pr_eta"] == T0 + H
    assert w.jobs[1].review_eta == et(10, 12, 17, 30) and w.jobs[1].round["pr"]["number"] == 112


def test_each_line_on_its_own_and_one_reply_listing_the_failures():
    text = "fm-cmd: reassign job1 implementer U0AUD1:aud1 16:00 ET\nfm-cmd: drop job2 descoped by Cheng\nfm-cmd: eta job9 pr 16:00 ET\nfm-cmd: frobnicate"
    w, actions = wave.observe(opened(), post(text, "1.2"), CFG)
    assert w.jobs[1].dropped and w.jobs[1].drop_reason == "descoped by Cheng"
    assert w.jobs[0].person("implementer")["slack"] == "U0IMP1"
    replies = [a for a in actions if a.kind == "post"]
    assert len(replies) == 1 and len(w.pending) == 1
    body = replies[0]["text"]
    assert "reassign job1" in body and "eta job9" in body and "frobnicate" in body and "drop job2" not in body
    assert replies[0]["thread"] == THREAD and body.endswith(f"ref: {replies[0].id}")
    w2, again = wave.observe(w, post(text, "1.2"), CFG)  # the same post again: nothing
    assert again == [] and len(w2.pending) == 1


def test_drop_of_the_last_open_job_closes_the_wave():
    w, actions = wave.observe(opened(), post("fm-cmd: drop job1 gone\nfm-cmd: drop job2 gone too", "1.3"), CFG)
    assert w.closed_at == T0 and [a.kind for a in actions] == ["wave_close"]


def test_fm_cmd_from_anyone_but_the_owner_is_ignored():
    w, actions = wave.observe(opened(), post("fm-cmd: drop job1 gone\nref: fm-wave-h-job1", "1.4", sender="U0IMP1"), CFG)
    assert not w.jobs[0].dropped and not any(a.kind == "post" and "fm-cmd" in a.get("text", "") for a in actions)


# -- ladder ids per holder and ETA ----------------------------------------------------------
def test_ladder_ids_name_the_holder_and_the_eta():
    v = ladder.JobView(ref="fm-wave-h-job2", holder="UA", owner=OWNER, est=H, eta=T0 + H, alternatives=[{"id": "UB", "free": True}])
    (c,) = ladder.step(v, T0 + H)
    assert c.id == f"fm-wave-h-job2/UA@{int(T0 + H)}/checkin"
    v = dataclasses.replace(v, checkin_at=T0 + H, done={c.id})
    (r,) = ladder.step(v, T0 + 2 * H)
    assert r.id == f"fm-wave-h-job2/UA@{int(T0 + H)}/reschedule"
    later = dataclasses.replace(v, eta=T0 + 3 * H, checkin_at=None, done={c.id, r.id})  # a re-estimate: the rungs run again
    assert [a.id for a in ladder.step(later, T0 + 3 * H)] == [f"fm-wave-h-job2/UA@{int(T0 + 3 * H)}/checkin"]
    moved = dataclasses.replace(v, holder="UB", alternatives=[], checkin_at=None, done={c.id, r.id})  # a new holder: the same
    assert [a.id for a in ladder.step(moved, T0 + H)] == [f"fm-wave-h-job2/UB@{int(T0 + H)}/checkin"]


# -- review fixes (Xi on f406241) -----------------------------------------------------------
def test_month_day_before_now_rolls_to_next_year():
    assert wave.parse_time("01-02 09:00 ET", now=datetime(2026, 12, 31, 22, 0, tzinfo=ET).timestamp()) == datetime(2027, 1, 2, 9, 0, tzinfo=ET).timestamp()
    assert wave.parse_time("10-11 14:30 ET", now=T0) == datetime(2027, 10, 11, 14, 30, tzinfo=ET).timestamp()  # one day back
    assert wave.parse_time("10-12 14:31 ET", now=T0) == et(10, 12, 14, 31)  # later today stays this year


def test_bare_et_times_work_in_wave_open_and_add_job():
    spec = "issue=83;files=src/wave.py;implementer=U0IMP1:imp1;auditor=U0AUD1:aud1;pr_eta=15:05 ET;review_eta=10-13 09:00 ET"
    w = wave.open_wave(THREAD, name="h", repo=REPO, jobs=[wave.parse_job(spec)], now=T0, cfg=CFG)  # cli.py calls parse_job without now
    assert w.jobs[0].pr_eta == et(10, 12, 15, 5) and w.jobs[0].review_eta == et(10, 13, 9, 0) and w.jobs[0].first_etas["pr_eta"] == et(10, 12, 15, 5)
    assert wave.parse_job(spec, now=T0)["pr_eta"] == et(10, 12, 15, 5)
    w = wave.add_job(w, wave.parse_job("issue=93;files=src/ladder.py;implementer=U0IMP2:imp2;auditor=U0AUD2:aud2;pr_eta=14:00 ET;review_eta=16:00 ET"), now=T0, cfg=CFG)
    assert w.jobs[1].pr_eta == et(10, 13, 14, 0) and w.jobs[1].round["due"] == et(10, 13, 14, 0)


def test_an_owner_post_with_fm_cmd_and_a_ref_applies_the_command_and_moves_no_round():
    w0 = opened()
    w, actions = wave.observe(w0, post("SIGN-OFF #108 f406241 approve\nfm-cmd: eta job1 pr 16:00 ET\nref: fm-wave-h-job1", "1.5"), CFG)
    assert actions == [] and w.jobs[0].pr_eta == et(10, 12, 16, 0)
    assert w.jobs[0].round["stage"] == w0.jobs[0].round["stage"] and not w.jobs[0].round.get("pr", {}).get("signoffs")  # owner posts never move a round (arbiter)
