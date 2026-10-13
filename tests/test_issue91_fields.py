"""#91 follow-up: PR list reads carry gh's raw milestone `{number, title}`; a PR without one reads `milestone: None`."""
from __future__ import annotations

import json

from fridica_research.github import ISSUE_FIELDS, LIST_FIELDS, list_prs, list_prs_for_issue

R2 = {"number": 2, "title": "R2"}
LISTED = [
    {"number": 1, "title": "a", "body": "Closes #91", "milestone": R2},
    {"number": 2, "title": "b", "body": "Refs #91", "milestone": None},
    {"number": 3, "title": "c", "body": "Refs #91"},
]


def fake(argv, stdin):
    assert argv[:3] == ["gh", "pr", "list"]
    fake.fields = argv[argv.index("--json") + 1].split(",")
    return json.dumps([{k: v for k, v in p.items() if k in fake.fields} for p in LISTED])


def test_fields_ask_for_milestone():
    assert "milestone" in LIST_FIELDS.split(",")
    assert "milestone" in ISSUE_FIELDS.split(",")


def test_list_prs_keeps_raw_milestone_and_none_when_missing():
    got = list_prs(fake, "o/r", "imp")
    assert "milestone" in fake.fields
    assert [p["milestone"] for p in got] == [R2, None, None]


def test_list_prs_for_issue_keeps_raw_milestone_and_none_when_missing():
    got = list_prs_for_issue(fake, "o/r", 91)
    assert "milestone" in fake.fields
    assert [(p["number"], p["milestone"]) for p in got] == [(1, R2), (2, None), (3, None)]
