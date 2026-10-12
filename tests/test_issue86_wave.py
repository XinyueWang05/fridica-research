"""#86: a wave that would never be polled is refused. `wave open` refuses an empty `--repo` (`open_wave` itself
still accepts it); with `Config.register` set each person's Slack id and login must be one registered Innie;
`serve` refuses while an open wave exists and both [github] and [board] are off."""
from __future__ import annotations

import dataclasses

import pytest

from fridica_research import cli, wave
from fridica_research.store import Store
from support import CFG, OWNER

THREAD = "T1:C1:1791817361.160319"
REPO = "chengcli/fridica-research"
T0 = 1_700_000_000.0
JOB = "issue=86;files=src/fridica_research/wave.py;implementer=U0IMP1:imp1;auditor=U0AUD1:aud1;pr_eta=2026-10-12T15:35Z;review_eta=2026-10-12T16:00Z"
REGISTER = """
[innie.imp]
outie = "Imp Outie"
slack = "U0IMP1"
github = "imp1"
[innie.aud]
outie = "Aud Outie"
slack = "U0AUD1"
github = "aud1"
"""


def write_config(tmp_path, extra: str = "") -> str:
    p = tmp_path / "research.toml"
    p.write_text(f'channels = ["C1"]\nstate_path = "{tmp_path / "s.sqlite3"}"\n{extra}[fridica]\nsocket = "{tmp_path / "c.sock"}"\nowner = "{OWNER}"\n')
    return str(p)


def job(imp=("U0IMP1", "imp1"), aud=("U0AUD1", "aud1")) -> dict:
    return {"issue": 86, "files": ["src/fridica_research/wave.py"], "implementer": {"slack": imp[0], "login": imp[1]},
            "auditor": {"slack": aud[0], "login": aud[1]}, "pr_eta": T0 + 3600, "review_eta": T0 + 7200}


def with_register(tmp_path):
    reg = tmp_path / "register.toml"
    reg.write_text(REGISTER)
    return dataclasses.replace(CFG, register=str(reg))


def test_cli_open_refuses_empty_repo(tmp_path, capsys):
    cfgp = write_config(tmp_path)
    for repo in ([], ["--repo", ""], ["--repo", "  "]):
        assert cli.main(["--config", cfgp, "wave", "open", THREAD, "--name", "f", *repo, "--job", JOB]) == 1
        assert "--repo" in capsys.readouterr().err
    assert cli.main(["--config", cfgp, "wave", "open", THREAD, "--name", "f", "--repo", REPO, "--job", JOB]) == 0


def test_open_wave_itself_still_accepts_empty_repo():
    w = wave.open_wave(THREAD, name="f", repo="", jobs=[job()], now=T0, cfg=CFG)
    assert w.repo == "" and len(w.jobs) == 1


def test_register_accepts_registered_people(tmp_path):
    w = wave.open_wave(THREAD, name="f", repo=REPO, jobs=[job()], now=T0, cfg=with_register(tmp_path))
    assert w.jobs[0].person("auditor") == {"slack": "U0AUD1", "login": "aud1"}


@pytest.mark.parametrize("imp,aud,needle", [
    (("U0NOPE", "imp1"), ("U0AUD1", "aud1"), "U0NOPE"),  # unregistered Slack id
    (("U0IMP1", "nobody"), ("U0AUD1", "aud1"), "nobody"),  # unregistered login
    (("U0IMP1", "aud1"), ("U0AUD1", "imp1"), "same Innie"),  # both registered, but to different Innies
])
def test_register_refuses_mismatched_people(tmp_path, imp, aud, needle):
    with pytest.raises(wave.WaveError, match=needle):
        wave.open_wave(THREAD, name="f", repo=REPO, jobs=[job(imp, aud)], now=T0, cfg=with_register(tmp_path))


def test_register_set_but_missing_is_a_wave_error(tmp_path):
    cfg = dataclasses.replace(CFG, register=str(tmp_path / "missing.toml"))
    with pytest.raises(wave.WaveError):
        wave.open_wave(THREAD, name="f", repo=REPO, jobs=[job()], now=T0, cfg=cfg)


class StubDriver:
    served: list = []

    def __init__(self, *a, **kw): pass
    def serve(self, once=False): StubDriver.served.append(once)


def serve(tmp_path, monkeypatch, extra: str = "") -> int:
    monkeypatch.setattr(cli, "Driver", StubDriver)
    monkeypatch.setattr(cli, "claude_runner", lambda model: None)
    StubDriver.served = []
    return cli.main(["--config", write_config(tmp_path, extra), "serve", "--once"])


def store_wave(tmp_path, closed: bool):
    w = wave.open_wave(THREAD, name="f", repo=REPO, jobs=[job()], now=T0, cfg=CFG)
    if closed: w.closed_at = T0 + 1
    s = Store(tmp_path / "s.sqlite3")
    s.set_meta(wave.wave_key(THREAD), w.to_json())
    s.close()


def test_serve_refuses_open_wave_without_github_or_board(tmp_path, monkeypatch, capsys):
    store_wave(tmp_path, closed=False)
    assert serve(tmp_path, monkeypatch) == 1
    assert StubDriver.served == [] and THREAD in capsys.readouterr().err


def test_serve_runs_with_closed_wave_or_no_wave(tmp_path, monkeypatch):
    assert serve(tmp_path, monkeypatch) == 0 and StubDriver.served == [True]
    store_wave(tmp_path, closed=True)
    assert serve(tmp_path, monkeypatch) == 0 and StubDriver.served == [True]


def test_serve_runs_open_wave_with_github_or_board(tmp_path, monkeypatch):
    store_wave(tmp_path, closed=False)
    assert serve(tmp_path, monkeypatch, "[github]\nenabled = true\n") == 0 and StubDriver.served == [True]
