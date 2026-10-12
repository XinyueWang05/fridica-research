"""#54 (R48, R50 amendment; absorbs #42): the Innie register, the roster check, the usage columns and the quota rules.

Synthetic data only. The register is `[innie.<name>]` TOML tables; `parse_roster` refuses what the
register does not list; `workload` carries four usage columns after `offline`; the estimator moves the
input time to the session reset at >= 90% session usage (cause `quota`) and flags `capped` at >= 80%
weekly usage, from a fresh report only.
"""
from __future__ import annotations

import dataclasses
import re
import sqlite3
from datetime import datetime, timedelta

import pytest

from fridica_research import arbiter, config, ledger, register
from fridica_research.eta import InnieBuffer, TrialRun, WorkloadReport, estimate
from support import CFG

TOML = '''
[innie.jiheng]
outie = "Jiheng Hu"
slack = "U0JIHENG"
github = "jihenghu"
host = "local"
hardware = "2x RTX 1080 Ti"
client = "claude"
plan = "max"
slots = 2
hours = "09:00-23:00"
offline = "23:00-09:00"
limits = ["fork-only pushes", "no review rights"]

[innie.xinyue]
outie = "Xinyue"
slack = "U0XINYUE"
github = "xinyue-gh"
limits = "codex out until Nov 10"
'''
NOW = datetime(2026, 10, 11, 18, 0)


# -- the register ----------------------------------------------------------------------
def test_register_parses_required_and_optional_fields():
    r = register.parse(TOML)
    assert len(r) == 2 and [i.name for i in r] == ["jiheng", "xinyue"]
    j = r.get("jiheng")
    assert (j.outie, j.slack, j.github, j.host, j.hardware, j.client, j.plan, j.slots) == ("Jiheng Hu", "U0JIHENG", "jihenghu", "local", "2x RTX 1080 Ti", "claude", "max", 2)
    assert j.hours == "09:00-23:00" and j.offline == "23:00-09:00" and j.limits == ("fork-only pushes", "no review rights")
    x = r.get("xinyue")
    assert (x.host, x.hardware, x.client, x.plan, x.slots, x.hours, x.offline, x.limits) == ("", "", "", "", 1, "", "", ("codex out until Nov 10",))
    assert r.by_slack("U0XINYUE") is x and r.by_login("XINYUE-GH") is x and r.by_slack("U0NOBODY") is None and r.by_login("nobody") is None
    assert r.outie_of("U0JIHENG") == "Jiheng Hu" and r.outie_of("U0NOBODY") == ""  # #42: the escalation target comes from the register
    assert j.in_window("offline", 23 * 60 + 30) and j.in_window("offline", 8 * 60) and not j.in_window("offline", 12 * 60)  # wraps past midnight
    assert j.in_window("hours", 12 * 60) and not j.in_window("hours", 23 * 60) and x.in_window("hours", 12 * 60) is None
    assert register.window("09:00-17:30", "x") == (540, 1050)


def test_register_accepts_an_innie_with_only_the_three_required_fields():
    """Review 5486239541 on #74: `limits` is optional, so a table with only outie, slack and github is accepted."""
    r = register.parse('[innie.minimal]\noutie = "M"\nslack = "U0MIN"\ngithub = "minimal-gh"\n')
    m = r.get("minimal")
    assert (m.outie, m.slack, m.github, m.limits, m.slots, m.hours, m.offline) == ("M", "U0MIN", "minimal-gh", (), 1, "", "")
    assert register.parse('[innie.a]\noutie = "A"\nslack = "U0A"\ngithub = "a"\nlimits = []\n').get("a").limits == ()  # the same as no limits
    assert arbiter.parse_roster(["implementer=U0MIN"], ["implementer=minimal-gh"], [], "UOWNER", r)["implementer"]["login"] == "minimal-gh"
    # no limits says nothing about usage: the quota rules come from the workload report alone
    e = estimate(NOW, "minimal", workload=WorkloadReport(innie="minimal", reported_at=NOW, session_usage=95.0, session_reset=NOW + timedelta(hours=1)), trial=TrialRun(hours=1.0))
    assert e.cause == "quota" and e.input_at == NOW + timedelta(hours=1) and not e.capped
    assert estimate(NOW, "minimal", workload=WorkloadReport(innie="minimal", reported_at=NOW), trial=TrialRun(hours=1.0)).cause == ""


@pytest.mark.parametrize("missing", ["outie", "slack", "github"])
def test_register_rejects_a_missing_required_field(missing):
    text = re.sub(rf"^{missing} = .*\n", "", TOML, count=1, flags=re.M)
    with pytest.raises(register.RegisterError, match=rf"\[innie.jiheng\]: missing {missing}"):
        register.parse(text)


def test_register_rejects_an_unknown_field():
    with pytest.raises(register.RegisterError, match=r"\[innie.jiheng\]: unknown field\(s\) backend, shoe_size"):
        register.parse(TOML.replace('host = "local"', 'host = "local"\nbackend = "claude"\nshoe_size = 42'))
    with pytest.raises(register.RegisterError, match="unknown top-level key"):
        register.parse('[people]\nx = "y"\n' + TOML)


def test_register_rejects_duplicate_ids():
    with pytest.raises(register.RegisterError, match=r"\[innie.xinyue\]: Slack id U0JIHENG is already \[innie.jiheng\]'s"):
        register.parse(TOML.replace('slack = "U0XINYUE"', 'slack = "U0JIHENG"'))
    with pytest.raises(register.RegisterError, match=r"GitHub login JIHENGHU is already \[innie.jiheng\]'s"):
        register.parse(TOML.replace('github = "xinyue-gh"', 'github = "JIHENGHU"'))
    with pytest.raises(register.RegisterError, match="register"):  # the same [innie.<name>] twice is a TOML error, reported as a register error
        register.parse(TOML + '\n[innie.jiheng]\noutie = "x"\nslack = "U0Z"\ngithub = "z"\n')


@pytest.mark.parametrize("bad,why", [
    ('slots = 0', "slots must be an integer >= 1"), ('slots = "2"', "slots must be an integer >= 1"), ('slots = true', "slots must be an integer >= 1"),
    ('hours = "9:00-17:00"', "hours: expected HH:MM-HH:MM"), ('offline = "23:00-25:00"', "offline: hour out of range"), ('hours = "09:00 to 17:00"', "hours: expected HH:MM-HH:MM"),
    ('limits = 3', "limits must be a string or a list of strings"), ('slack = "nobody"', "is not a Slack user id"), ('outie = 7', "outie must be a string"),
])
def test_register_rejects_bad_values(bad, why):
    key = bad.split(" =")[0]
    text = re.sub(rf"^{key} = .*$", bad, TOML, count=1, flags=re.M)
    with pytest.raises(register.RegisterError, match=why):
        register.parse(text)


def test_register_load_reads_a_file_and_a_missing_path_is_a_hard_error(tmp_path):
    p = tmp_path / "register.toml"
    p.write_text(TOML)
    r = register.load(p)
    assert len(r) == 2 and r.path == str(p)
    with pytest.raises(register.RegisterError, match="does not exist"):
        register.load(tmp_path / "nope.toml")
    assert register.load_for(config.Config()) is None
    with pytest.raises(register.RegisterError, match="does not exist"):
        register.load_for(dataclasses.replace(CFG, register=str(tmp_path / "nope.toml")))


# -- Config ------------------------------------------------------------------------------
def test_config_register_path_round_trips_and_defaults_to_none():
    assert config.parse("").register == "" and config.Config().to_dict()["register"] == ""
    c = config.parse('register = "~/.config/fridica-research/register.toml"\n')
    assert c.register == "~/.config/fridica-research/register.toml" and config.Config.from_dict(c.to_dict()) == c


# -- parse_roster against the register ---------------------------------------------------
def test_parse_roster_refuses_a_slack_id_or_login_not_in_the_register():
    reg = register.parse(TOML)
    ok = arbiter.parse_roster(["implementer=U0JIHENG", "auditor=U0XINYUE"], ["implementer=jihenghu"], [], "UOWNER", reg)
    assert ok["implementer"] == {"holders": ["U0JIHENG"], "login": "jihenghu", "delegate": False}
    with pytest.raises(arbiter.RosterError, match="U0STRANGER is not in the register"):
        arbiter.parse_roster(["implementer=U0JIHENG,U0STRANGER"], [], [], "UOWNER", reg)
    with pytest.raises(arbiter.RosterError, match="--login implementer=someone: 'someone' is not in the register"):
        arbiter.parse_roster(["implementer=U0JIHENG"], ["implementer=someone"], [], "UOWNER", reg)
    assert arbiter.parse_roster(["implementer=U0STRANGER"], ["implementer=someone"], [], "UOWNER")["implementer"]["login"] == "someone"  # no register: unchecked, as before


def test_adopt_loads_the_register_and_a_set_but_missing_path_is_a_hard_error(tmp_path):
    p = tmp_path / "register.toml"
    p.write_text(TOML)
    cfg = dataclasses.replace(CFG, register=str(p))
    rnd, _ = arbiter.adopt("T1:C1:1700000000.000100", issue=54, repo="o/r", roles=["implementer=U0JIHENG"], stage="Implement", now=0.0, cfg=cfg)
    assert rnd.holders("implementer") == ["U0JIHENG"]
    with pytest.raises(arbiter.RosterError, match="U0STRANGER is not in the register"):
        arbiter.adopt("T1:C1:1700000000.000100", issue=54, repo="o/r", roles=["implementer=U0STRANGER"], stage="Implement", now=0.0, cfg=cfg)
    with pytest.raises(register.RegisterError, match="does not exist"):
        arbiter.adopt("T1:C1:1700000000.000100", issue=54, repo="o/r", roles=["implementer=U0JIHENG"], stage="Implement", now=0.0, cfg=dataclasses.replace(CFG, register=str(tmp_path / "gone.toml")))


# -- the ledger's usage columns ----------------------------------------------------------
USAGE = dict(session_usage=42.5, session_reset="2026-10-12T05:50:00Z", weekly_usage=22.0, weekly_reset="2026-10-12T12:00:00Z")


def test_workload_has_the_four_usage_columns_after_offline():
    db = ledger.connect()
    cols = ledger.columns(db, "workload")
    i = cols.index("offline")
    assert cols[i:i + 5] == ["offline", "session_usage", "session_reset", "weekly_usage", "weekly_reset"] and cols[-2:] == ["source", "recorded"]
    assert ledger.import_rows(db, "workload", [dict(id=1, innie="alfa", offline="", **USAGE)]).inserted == 1
    assert dict(db.execute("SELECT session_usage, session_reset, weekly_usage, weekly_reset FROM workload").fetchone()) == USAGE
    db.close()


def test_an_older_prototype_without_the_usage_columns_still_imports(tmp_path):
    old_ddl = ledger.DDL.replace("  offline TEXT, session_usage REAL, session_reset TEXT, weekly_usage REAL, weekly_reset TEXT,\n", "  offline TEXT,\n")
    assert "session_usage" not in old_ddl
    src_path = tmp_path / "old.sqlite3"
    src = sqlite3.connect(str(src_path), isolation_level=None)
    src.executescript(old_ddl)
    src.execute("INSERT INTO workload(id, innie, offline, source) VALUES(1, 'alfa', '', 'slack')")
    src.close()
    db = ledger.connect()
    assert ledger.import_sqlite(db, src_path)["workload"].inserted == 1
    row = dict(db.execute("SELECT * FROM workload").fetchone())
    assert row["innie"] == "alfa" and all(row[c] is None for c, _ in ledger.USAGE_COLUMNS)
    db.close()
    # an older ledger *file* of ours gains the columns in place on connect
    mine = tmp_path / "ledger.sqlite3"
    own = sqlite3.connect(str(mine), isolation_level=None)
    own.executescript(old_ddl)
    own.execute("INSERT INTO workload(id, innie) VALUES(2, 'bravo')")
    own.close()
    db = ledger.connect(mine)
    assert set(c for c, _ in ledger.USAGE_COLUMNS) <= set(ledger.columns(db, "workload"))  # SQLite appends added columns: present, not re-ordered
    assert db.execute("SELECT innie FROM workload WHERE id=2").fetchone()[0] == "bravo"  # the old rows survive the migration
    assert ledger.import_rows(db, "workload", [dict(id=2, innie="bravo", **USAGE)]).updated == 1
    db.close()


@pytest.mark.parametrize("name", ["jobs#1.sqlite3", "what?.sqlite3", "jobs%20ledger.sqlite3", "a b#c?d%.sqlite3"])
def test_read_only_uri_survives_hash_question_mark_and_percent_in_the_path(tmp_path, name):
    path = tmp_path / name
    src = sqlite3.connect(str(path), isolation_level=None)
    src.executescript(ledger.DDL)
    src.execute("INSERT INTO workload(id, innie) VALUES(1, 'alfa')")
    src.close()
    uri = ledger.ro_uri(path)
    assert uri.startswith("file:///") and uri.endswith("?mode=ro") and "#" not in uri and uri.count("?") == 1 and " " not in uri
    before = path.read_bytes()
    db = ledger.connect()
    assert ledger.import_sqlite(db, path)["workload"].inserted == 1 and path.read_bytes() == before
    db.close()
    ro = ledger.open_existing(path)
    assert ro.execute("SELECT innie FROM workload").fetchone()[0] == "alfa"
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        ro.execute("INSERT INTO workload(id) VALUES(9)")
    ro.close()
    with pytest.raises(FileNotFoundError):
        ledger.open_existing(tmp_path / ("missing" + name))


# -- eta: quota and the weekly cap -------------------------------------------------------
def fresh(**kw) -> WorkloadReport: return WorkloadReport(innie="alfa", reported_at=NOW - timedelta(minutes=5), **kw)


def test_a_session_at_or_past_90_percent_moves_the_input_time_to_the_reset_with_cause_quota():
    reset = NOW + timedelta(hours=2)
    e = estimate(NOW, "alfa", workload=fresh(session_usage=90.0, session_reset=reset), trial=TrialRun(hours=1.0))
    assert e.input_at == reset and e.cause == "quota" and e.eta == reset + timedelta(hours=1) and not e.capped and "quota" in e.rationale
    e = estimate(NOW, "alfa", workload=fresh(session_usage=89.9, session_reset=reset), trial=TrialRun(hours=1.0))
    assert e.input_at == NOW and e.cause == "" and e.eta == NOW + timedelta(hours=1)
    e = estimate(NOW, "alfa", workload=fresh(session_usage=99.0), trial=TrialRun(hours=1.0))  # no reset time known: nothing to move to
    assert e.input_at == NOW and e.cause == ""
    e = estimate(NOW, "alfa", workload=fresh(session_usage=99.0, session_reset=NOW - timedelta(hours=1)), trial=TrialRun(hours=1.0))  # a reset already past
    assert e.input_at == NOW and e.cause == ""


def test_weekly_at_or_past_80_percent_sets_capped():
    e = estimate(NOW, "alfa", workload=fresh(weekly_usage=80.0, weekly_reset=NOW + timedelta(days=1)), trial=TrialRun(hours=1.0))
    assert e.capped and e.cause == "" and e.input_at == NOW and "short jobs only" in e.rationale
    assert not estimate(NOW, "alfa", workload=fresh(weekly_usage=79.9), trial=TrialRun(hours=1.0)).capped
    both = estimate(NOW, "alfa", workload=fresh(session_usage=95.0, session_reset=NOW + timedelta(hours=3), weekly_usage=85.0), trial=TrialRun(hours=1.0))
    assert both.capped and both.cause == "quota" and both.input_at == NOW + timedelta(hours=3)


def test_a_stale_report_ignores_both_the_quota_and_the_cap():
    stale = WorkloadReport(innie="alfa", reported_at=NOW - timedelta(hours=6), session_usage=99.0, session_reset=NOW + timedelta(hours=2), weekly_usage=99.0)
    e = estimate(NOW, "alfa", buffer=InnieBuffer(innie="alfa", queue_hours=0.5), workload=stale, trial=TrialRun(hours=1.0), fresh_within_hours=1.0)
    assert e.input_at == NOW and e.cause == "" and not e.capped and e.basis == "trial" and e.buffer["queue"] == 0.5
    assert stale.quota_exhausted and stale.capped  # the report says so; only its staleness keeps it out
