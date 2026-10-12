"""#84: workload intake — parse an R50 report, ingest it into the ledger, freshness, alternatives.

Every report here is synthetic. 2026-10-12 is a Monday in EDT (UTC-4); 2026-11-02 is in EST (UTC-5).
"""
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fridica_research import ledger, workload
from fridica_research import register as reg
from fridica_research.workload import WorkloadError, parse

AT = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)  # 11:00 ET, Monday
LINE = "claude-code | session: 40% (resets 13:50 ET) | weekly: 22% (resets Mon 08:00 ET)"


def test_the_usage_line_parses_every_field():
    r = parse(LINE, AT)
    assert r.client == "claude-code" and r.session_usage == 40.0 and r.weekly_usage == 22.0
    assert r.session_reset == "2026-10-12T17:50:00Z"  # 13:50 EDT today
    assert r.weekly_reset == "2026-10-19T12:00:00Z"  # Mon 08:00 already passed today, so next Monday
    assert r.reported_at == "2026-10-12T15:00:00Z"


def test_a_reset_earlier_than_the_report_rolls_to_tomorrow_and_follows_est():
    assert parse("c | session: 1% (resets 09:00 ET) | weekly: 2%", AT).session_reset == "2026-10-13T13:00:00Z"
    nov = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)  # 10:00 EST
    assert parse("c | session: 1% (resets 13:50 ET) | weekly: 2%", nov).session_reset == "2026-11-02T18:50:00Z"


def test_unreadable_is_stored_as_none_never_estimated():
    r = parse("codex | session: unreadable | weekly: unreadable (resets unreadable)", AT)
    assert (r.client, r.session_usage, r.session_reset, r.weekly_usage, r.weekly_reset) == ("codex", None, None, None, None)


def test_slots_parse_into_free_and_total():
    r = parse("slots: 1/2\n" + LINE, AT)
    assert (r.free_slots, r.total_slots) == (1, 2)
    assert parse("slots: unreadable/2", AT).free_slots is None


@pytest.mark.parametrize("bad", [
    "claude | session: 40 (resets 13:50 ET) | weekly: 22%",
    "claude | session: 140% | weekly: 22%",
    "claude | session: 40% (resets 25:00 ET) | weekly: 22%",
    "claude | session: 40% (resets Xyz 08:00 ET) | weekly: 22%",
    "session: 40% | weekly: 22%",
    "slots: 3/2",
    "slots: two",
])
def test_a_malformed_line_is_a_finding_not_a_row(bad):
    with pytest.raises(WorkloadError):
        parse(bad, AT)


def test_free_text_around_the_fields_is_passed_over():
    assert parse("Report for wave F:\nnothing queued\n" + LINE, AT).session_usage == 40.0


# -- the ledger --------------------------------------------------------------------------
def test_the_intake_columns_follow_the_usage_columns():
    db = ledger.connect()
    cols = ledger.columns(db, "workload")
    i = cols.index("weekly_reset")
    assert cols[i:i + 4] == ["weekly_reset", "free_slots", "total_slots", "client"] and cols[-2:] == ["source", "recorded"]
    assert [c for c, _ in ledger.INTAKE_COLUMNS] == ["free_slots", "total_slots", "client"]


def test_an_old_schema_ledger_gains_the_intake_columns_in_place(tmp_path):
    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(str(path), isolation_level=None)
    old.executescript(ledger.DDL.replace("  free_slots INTEGER, total_slots INTEGER, client TEXT,\n", ""))
    old.execute("INSERT INTO workload(id, innie, slots, source) VALUES(1, 'alfa', 2, 's1')")
    old.close()
    db = ledger.connect(path)
    assert ledger.columns(db, "workload")[-3:] == ["free_slots", "total_slots", "client"]
    assert dict(db.execute("SELECT innie, slots, free_slots FROM workload").fetchone()) == dict(innie="alfa", slots=2, free_slots=None)
    db.close()
    assert ledger.columns(ledger.connect(path), "workload")[-3:] == ["free_slots", "total_slots", "client"]  # a second connect adds nothing


def test_ingest_is_idempotent_on_innie_and_source():
    db = ledger.connect()
    row = parse("slots: 1/2\n" + LINE, AT).row("alfa")
    assert ledger.ingest(db, row, "C1/1791817810.1").inserted == 1
    before = ledger.dump(db, ("workload",))
    assert ledger.ingest(db, row, "C1/1791817810.1").unchanged == 1
    assert ledger.dump(db, ("workload",)) == before
    edited = parse("slots: 0/2\n" + LINE, AT).row("alfa")
    assert ledger.ingest(db, edited, "C1/1791817810.1").updated == 1
    assert ledger.ingest(db, row, "C1/other").inserted == 1  # another post is another row
    assert db.execute("SELECT COUNT(*) FROM workload").fetchone()[0] == 2
    assert db.execute("SELECT free_slots FROM workload WHERE source='C1/1791817810.1'").fetchone()[0] == 0


def _ledger(*reports):
    db = ledger.connect()
    for innie, text, at in reports: ledger.ingest(db, parse(text, at).row(innie), f"src-{innie}")
    return db


def test_a_report_is_fresh_for_n_hours_or_until_a_reset_passes():
    db = _ledger(("alfa", "slots: 1/1\n" + LINE, AT))
    row = dict(db.execute("SELECT * FROM workload").fetchone())
    assert ledger.fresh(row, datetime(2026, 10, 12, 17, 0, tzinfo=timezone.utc))
    assert not ledger.fresh(row, datetime(2026, 10, 12, 17, 50, tzinfo=timezone.utc))  # the session reset passed
    assert not ledger.fresh(row, datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc))  # before it was posted
    late = dict(row, session_reset=None, weekly_reset=None)
    assert ledger.fresh(late, datetime(2026, 10, 12, 20, 59, tzinfo=timezone.utc), hours=6)
    assert not ledger.fresh(late, datetime(2026, 10, 12, 21, 1, tzinfo=timezone.utc), hours=6)


def test_alternatives_skip_capped_and_full_and_rank_unknown_usage_last():
    now = datetime(2026, 10, 12, 16, 0, tzinfo=timezone.utc)
    db = _ledger(
        ("unknown", "slots: 2/2\nc | session: unreadable | weekly: unreadable", AT),
        ("free", "slots: 1/2\nc | session: 10% | weekly: 30%", AT),
        ("freer", "slots: 2/2\nc | session: 10% | weekly: 50%", AT),
        ("capped", "slots: 2/2\nc | session: 10% | weekly: 85%", AT),
        ("quota", "slots: 2/2\nc | session: 95% | weekly: 10%", AT),
        ("full", "slots: 0/2\nc | session: 10% | weekly: 10%", AT),
        ("stale", "slots: 2/2\nc | session: 10% | weekly: 10%", datetime(2026, 10, 11, 15, 0, tzinfo=timezone.utc)),
    )
    assert ledger.alternatives(db, now) == ["freer", "free", "unknown"]
    assert ledger.alternatives(db, now, exclude={"freer"}) == ["free", "unknown"]


def test_the_module_docstring_names_the_line_format():
    assert re.search(r"client \| session: X% \(resets HH:MM ET\) \| weekly: Y%", workload.__doc__)


# -- thread posts into rows through the register (rest of #84) ----------------------------
REGISTER = reg.parse('''
[innie.alfa]
outie = "Alfa Outie"
slack = "U0ALFA"
github = "alfa"
hours = "09:00-17:00"

[innie.bravo]
outie = "Bravo Outie"
slack = "U0BRAVO"
github = "bravo"

[innie.night]
outie = "Night Outie"
slack = "U0NIGHT"
github = "night"
hours = "22:00-06:00"
''')
TS = str(AT.timestamp())  # 11:00 ET
NOW = datetime(2026, 10, 12, 16, 0, tzinfo=timezone.utc)  # 12:00 ET


def _post(sender, text, ts=TS, **kw):
    return {"sender": sender, "text": text, "ts": ts, **kw}


def test_intake_reads_a_post_into_a_row_through_the_register():
    rows, findings = workload.intake([_post("U0ALFA", "slots: 1/2\n" + LINE)], REGISTER, NOW, thread="C1/1791823244.1")
    assert findings == []
    assert len(rows) == 1
    row = rows[0]
    assert row["innie"] == "alfa" and row["source"] == f"C1/1791823244.1/{TS}"
    assert row["reported_at"] == "2026-10-12T15:00:00Z"  # the post's own ts, not `now`
    assert (row["free_slots"], row["total_slots"], row["session_usage"], row["client"]) == (1, 2, 40.0, "claude-code")


def test_a_posts_own_thread_overrides_the_argument():
    rows, _ = workload.intake([_post("U0ALFA", LINE, thread="C9/1791000000.2"), _post("U0BRAVO", LINE)], REGISTER, NOW, thread="C1/1791823244.1")
    assert [r["source"] for r in rows] == [f"C9/1791000000.2/{TS}", f"C1/1791823244.1/{TS}"]


def test_findings_are_dicts_of_source_sender_and_reason():
    posts = [
        _post("U0ALFA", "claude | session: 140% | weekly: 22%"),  # malformed
        _post("U0STRANGER", LINE),  # a report from someone the register does not list
        _post("U0BRAVO", LINE, ts=str(NOW.timestamp() + 60)),  # dated after `now`
        _post("U0BRAVO", LINE, ts="not-a-ts"),
    ]
    rows, findings = workload.intake(posts, REGISTER, NOW, thread="C1/1.1")
    assert rows == []
    assert all(set(f) == {"source", "sender", "reason"} for f in findings)
    assert [(f["source"], f["sender"]) for f in findings] == [
        (f"C1/1.1/{TS}", "U0ALFA"), (f"C1/1.1/{TS}", "U0STRANGER"), (f"C1/1.1/{NOW.timestamp() + 60}", "U0BRAVO"), ("C1/1.1/not-a-ts", "U0BRAVO")]
    assert "140" in findings[0]["reason"] and "register" in findings[1]["reason"]
    assert all(f["reason"] for f in findings)


def test_plain_chat_is_neither_a_row_nor_a_finding():
    rows, findings = workload.intake([_post("U0ALFA", "on it, PR soon"), _post("U0STRANGER", "thanks all")], REGISTER, NOW, thread="C1/1.1")
    assert (rows, findings) == ([], [])


def test_ingest_posts_writes_the_rows_and_is_idempotent():
    db = ledger.connect()
    posts = [_post("U0ALFA", "slots: 1/2\n" + LINE), _post("U0BRAVO", "slots: bad")]
    result, findings = ledger.ingest_posts(db, posts, REGISTER, NOW, thread="C1/1.1")
    assert result.inserted == 1 and [f["sender"] for f in findings] == ["U0BRAVO"]
    before = ledger.dump(db, ("workload",))
    again, _ = ledger.ingest_posts(db, posts, REGISTER, NOW, thread="C1/1.1")
    assert again.unchanged == 1 and ledger.dump(db, ("workload",)) == before
    assert db.execute("SELECT innie, source FROM workload").fetchone()[:] == ("alfa", f"C1/1.1/{TS}")


# -- working hours, in ET ----------------------------------------------------------------
def _free(*names, at=AT):
    return _ledger(*((n, "slots: 1/1\nc | session: 10% | weekly: 10%", at) for n in names))


def test_alternatives_drop_an_innie_outside_its_working_hours_in_et():
    db = _free("alfa", "bravo", "night")
    noon = datetime(2026, 10, 12, 16, 0, tzinfo=timezone.utc)  # 12:00 EDT: alfa in, night out
    assert ledger.alternatives(db, noon, register=REGISTER) == ["alfa", "bravo"]
    assert ledger.alternatives(db, noon) == ["alfa", "bravo", "night"]  # no register: no hours filter
    evening = datetime(2026, 10, 12, 21, 30, tzinfo=timezone.utc)  # 17:30 EDT: alfa just out (17:30 UTC would be in)
    db = _free("alfa", "bravo", "night", at=datetime(2026, 10, 12, 21, 0, tzinfo=timezone.utc))
    assert ledger.alternatives(db, evening, register=REGISTER) == ["bravo"]


def test_working_hours_follow_est_and_wrap_past_midnight():
    nov = datetime(2026, 11, 2, 13, 30, tzinfo=timezone.utc)  # 08:30 EST (09:30 if it were read as EDT)
    db = _free("alfa", "night", at=datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc))
    assert ledger.alternatives(db, nov, register=REGISTER) == []  # alfa before 09:00, night after 06:00
    late = datetime(2026, 11, 3, 4, 0, tzinfo=timezone.utc)  # 23:00 EST on the 2nd: night in, alfa out
    db = _free("alfa", "night", at=datetime(2026, 11, 3, 3, 0, tzinfo=timezone.utc))
    assert ledger.alternatives(db, late, register=REGISTER) == ["night"]


def test_an_unregistered_innie_or_unset_hours_keeps_the_innie():
    late = datetime(2026, 10, 13, 3, 0, tzinfo=timezone.utc)  # 23:00 EDT
    db = _free("bravo", "ghost", "alfa", at=datetime(2026, 10, 13, 2, 0, tzinfo=timezone.utc))
    assert ledger.alternatives(db, late, register=REGISTER) == ["bravo", "ghost"]
    assert ledger.working(REGISTER, "ghost", late) and ledger.working(REGISTER, "bravo", late) and not ledger.working(REGISTER, "alfa", late)
    assert ledger.working(None, "alfa", late)


# -- the canonical line under R50 ----------------------------------------------------------
def test_protocol_documents_the_canonical_report_lines_under_r50():
    text = (Path(__file__).resolve().parents[1] / "docs" / "protocol.md").read_text()
    r50 = text[text.index("**R50."):text.index("**R51.")]
    assert "client | session: X% (resets HH:MM ET) | weekly: Y% (resets [Day] HH:MM ET)" in r50
    assert "slots: F/T" in r50 and "unreadable" in r50 and "workload.py" in r50
    sample = re.search(r"`([^`\n]+\| session:[^`\n]+)`\n", r50)
    assert sample and parse(sample[1], AT).session_usage is not None  # the documented example parses
