"""#93 (part): the deputy can change a wave. `drop_job` (a reason, terminal), `add_job` (re-runs the checks),
`bind_pr` and `set_eta` (the first ETA is kept on `Job.first_etas` and `close()` scores against it); `close()`
counts merged or dropped jobs and lists the dropped ones apart. CLI: `wave drop|add-job|bind-pr|eta`, `list`
shows waves, `wave status` prints the finding text and has `--json`."""
from __future__ import annotations

import json

import pytest

from fridica_research import cli, wave
from fridica_research.machine import Event
from fridica_research.store import Store
from support import CFG, OWNER

THREAD = "T1:C1:1791817361.160319"
REPO = "chengcli/fridica-research"
T0 = 1_700_000_000.0
H = 3600.0


def job(issue: int, path: str, imp: str, aud: str) -> dict:
    return {"issue": issue, "files": [path], "implementer": {"slack": f"U0{imp.upper()}", "login": imp}, "auditor": {"slack": f"U0{aud.upper()}", "login": aud},
            "pr_eta": T0 + 2 * H, "review_eta": T0 + 4 * H}


def opened() -> wave.Wave:
    return wave.open_wave(THREAD, name="f", repo=REPO, jobs=[job(86, "src/wave.py", "imp1", "aud1"), job(87, "src/arbiter.py", "imp2", "aud2")], now=T0, cfg=CFG)


def merged(w: wave.Wave, n: int, pr: int, now: float):
    return wave.observe(w, Event("merged", now, {"job": n, "pr": pr, "sha": str(n) * 40}), CFG)


def test_first_etas_recorded_at_open_and_kept_by_set_eta():
    w = opened()
    assert w.jobs[0].first_etas == {"pr_eta": T0 + 2 * H, "review_eta": T0 + 4 * H}
    w = wave.set_eta(w, 1, pr_eta=T0 + 5 * H)
    assert w.jobs[0].pr_eta == T0 + 5 * H and w.jobs[0].review_eta == T0 + 4 * H
    assert w.jobs[0].first_etas == {"pr_eta": T0 + 2 * H, "review_eta": T0 + 4 * H}
    with pytest.raises(wave.WaveError): wave.set_eta(w, 3, pr_eta=T0)
    with pytest.raises(wave.WaveError): wave.set_eta(w, 1)


def test_close_scores_against_first_eta():
    w = wave.set_eta(opened(), 1, pr_eta=T0 + 10 * H, review_eta=T0 + 10 * H)
    w = wave.bind_pr(w, 1, 101, now=T0 + 3 * H)
    w = wave.bind_pr(w, 2, 102, now=T0 + 1 * H)
    w, _ = merged(w, 1, 101, T0 + 3.5 * H)
    w, actions = merged(w, 2, 102, T0 + 3.6 * H)
    close = [a for a in actions if a.kind == "wave_close"][0].data
    pr1 = next(p for p in close["etas"] if p["job"] == 1 and p["kind"] == "pr")
    assert pr1["first_eta"] == T0 + 2 * H and pr1["delivered"] == T0 + 3 * H  # late against the first ETA, not the re-estimate


def test_bind_pr_records_pr_and_routes_links():
    w = wave.bind_pr(opened(), 2, 202, head="a" * 40, now=T0 + H)
    assert w.jobs[1].round["pr"]["number"] == 202 and w.jobs[1].delivered["pr"] == T0 + H
    j, why = wave.route(w, Event("crew_post", T0 + 2 * H, {"ts": "1.1", "sender": "UX", "text": f"https://github.com/{REPO}/pull/202"}))
    assert j is w.jobs[1] and why == ""
    with pytest.raises(wave.WaveError): wave.bind_pr(w, 2, 0, now=T0)


def test_drop_is_terminal_and_closes_the_wave():
    w = opened()
    with pytest.raises(wave.WaveError, match="reason"): wave.drop_job(w, 2, "", now=T0)
    w, actions = wave.drop_job(w, 2, "descoped by the floor manager", now=T0 + H)
    assert actions == [] and w.jobs[1].dropped and w.jobs[1].drop_reason == "descoped by the floor manager" and w.closed_at is None
    with pytest.raises(wave.WaveError): wave.drop_job(w, 2, "again", now=T0 + H)
    w2, out = wave.observe(w, Event("pr_polled", T0 + 2 * H, {"job": 2, "prs": []}), CFG)
    assert out == [] and w2.jobs[1].round == w.jobs[1].round  # a dropped job's round never moves
    w = wave.bind_pr(w, 1, 101, now=T0 + H)
    w, actions = merged(w, 1, 101, T0 + 3 * H)
    close = [a for a in actions if a.kind == "wave_close"]
    assert len(close) == 1 and w.closed_at == T0 + 3 * H
    assert [j["job"] for j in close[0].data["jobs"]] == [1]
    assert close[0].data["dropped"] == [{"job": 2, "issue": 87, "reason": "descoped by the floor manager", "dropped_at": T0 + H}]
    assert all(p["job"] == 1 for p in close[0].data["etas"])
    text = w.pending[-1]["text"]
    assert "1/2 jobs merged, 1 dropped" in text and "job 2 #87 dropped: descoped by the floor manager" in text


def test_drop_last_open_job_closes_and_merged_job_cannot_be_dropped():
    w = wave.bind_pr(opened(), 1, 101, now=T0 + H)
    w, _ = merged(w, 1, 101, T0 + 2 * H)
    with pytest.raises(wave.WaveError): wave.drop_job(w, 1, "late", now=T0 + 3 * H)
    w, actions = wave.drop_job(w, 2, "superseded", now=T0 + 3 * H)
    assert [a.kind for a in actions] == ["wave_close"] and w.closed_at == T0 + 3 * H


def test_add_job_rechecks_and_refuses_closed_wave():
    w = opened()
    with pytest.raises(wave.WaveError, match="overlap"): wave.add_job(w, job(93, "src/wave.py", "imp3", "aud3"), now=T0, cfg=CFG)
    with pytest.raises(wave.WaveError, match="issue"): wave.add_job(w, job(86, "src/cli.py", "imp3", "aud3"), now=T0, cfg=CFG)
    with pytest.raises(wave.WaveError, match="ETA"): wave.add_job(w, {**job(93, "src/cli.py", "imp3", "aud3"), "pr_eta": None}, now=T0, cfg=CFG)
    w = wave.add_job(w, job(93, "src/cli.py", "imp3", "aud3"), now=T0 + H, cfg=CFG)
    j = w.jobs[2]
    assert j.repo == REPO and j.round["thread"] == wave.job_thread(THREAD, 3) and j.input_at == T0 + H and j.first_etas["pr_eta"] == T0 + 2 * H
    w.closed_at = T0 + 5 * H
    with pytest.raises(wave.WaveError, match="closed"): wave.add_job(w, job(94, "docs/", "imp4", "aud4"), now=T0, cfg=CFG)


def test_old_blob_without_first_etas_scores_against_pr_eta():
    d = opened().to_dict()
    for j in d["jobs"]: j.pop("first_etas", None)
    w = wave.Wave.from_dict(d)
    w = wave.bind_pr(wave.bind_pr(w, 1, 101, now=T0 + H), 2, 102, now=T0 + H)
    w, _ = merged(w, 1, 101, T0 + 2 * H)
    w, actions = merged(w, 2, 102, T0 + 2 * H)
    assert all(p["first_eta"] is not None for p in actions[-1].data["etas"])


# -- CLI --------------------------------------------------------------------------------
def write_config(tmp_path) -> str:
    p = tmp_path / "research.toml"
    p.write_text(f'channels = ["C1"]\nstate_path = "{tmp_path / "s.sqlite3"}"\n[fridica]\nsocket = "{tmp_path / "c.sock"}"\nowner = "{OWNER}"\n')
    return str(p)


def spec(issue: int, path: str, imp: str, aud: str) -> str:
    return f"issue={issue};files={path};implementer=U0{imp.upper()}:{imp};auditor=U0{aud.upper()}:{aud};pr_eta=2026-10-12T15:35Z;review_eta=2026-10-12T16:00Z"


def stored(tmp_path) -> wave.Wave:
    s = Store(tmp_path / "s.sqlite3")
    try: return wave.Wave.from_json(s.get_meta(wave.wave_key(THREAD)))
    finally: s.close()


def test_cli_wave_commands(tmp_path, capsys):
    cfgp = write_config(tmp_path)
    run = lambda *a: cli.main(["--config", cfgp, "wave", *a])  # noqa: E731
    assert run("open", THREAD, "--name", "f", "--repo", REPO, "--job", spec(86, "src/wave.py", "imp1", "aud1"), "--job", spec(87, "src/arbiter.py", "imp2", "aud2")) == 0
    assert run("add-job", THREAD, "--job", spec(93, "src/cli.py", "imp3", "aud3")) == 0
    assert run("add-job", THREAD, "--job", spec(94, "src/cli.py", "imp4", "aud4")) == 1 and "overlap" in capsys.readouterr().err
    assert run("bind-pr", THREAD, "1", "--pr", "101") == 0
    assert run("eta", THREAD, "1", "--pr-eta", "2026-10-12T18:00Z") == 0
    assert run("drop", THREAD, "3", "--reason", "folded into job 1") == 0
    assert run("drop", THREAD, "3", "--reason", "again") == 1
    w = stored(tmp_path)
    assert len(w.jobs) == 3 and w.jobs[0].round["pr"]["number"] == 101 and w.jobs[2].dropped
    assert w.jobs[0].pr_eta != w.jobs[0].first_etas["pr_eta"]
    w.findings.append("crew_post 9.9 not routed: UX holds no role and the post names no job")
    s = Store(tmp_path / "s.sqlite3")
    s.set_meta(wave.wave_key(THREAD), w.to_json())
    s.close()
    capsys.readouterr()
    assert run("status", THREAD) == 0
    out = capsys.readouterr().out
    assert "UX holds no role and the post names no job" in out and "dropped" in out
    assert run("status", THREAD, "--json") == 0
    d = json.loads(capsys.readouterr().out)
    assert d["thread"] == THREAD and len(d["jobs"]) == 3 and d["jobs"][2]["drop_reason"] == "folded into job 1"
    assert cli.main(["--config", cfgp, "list"]) == 0
    out = capsys.readouterr().out
    assert THREAD in out and "wave f" in out
    assert run("drop", "T1:C1:9.9", "1", "--reason", "x") == 1  # no such wave


def test_cli_drop_needs_reason(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["--config", write_config(tmp_path), "wave", "drop", THREAD, "1"])
