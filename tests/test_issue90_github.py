"""github.py building blocks for #88, #90 and #91: normalized `mergeable` and CI `checks` on `view_pr`,
a typed error for a refused review request, and the PRs that reference an issue. Fake `gh`: no network."""
from __future__ import annotations

import json

import pytest

from fridica_research import github
from fridica_research.github import GitHub, ReviewRequestRefused, checks_state, list_prs_for_issue, request_reviewers, view_pr
from test_github import GCFG, HEAD, FakeGh

PR = "https://github.com/o/r/pull/9"


def run_(status, conclusion="", typ="CheckRun"):
    return {"__typename": typ, "name": "ci", "status": status, "conclusion": conclusion}


def ctx(state):
    return {"__typename": "StatusContext", "context": "ci/legacy", "state": state}


# -- view_pr: mergeable + checks, raw rollup dropped ------------------------------------------------

def test_view_pr_returns_mergeable_and_checks_without_raw_rollup():
    gh = FakeGh((PR, {"mergeable": "CONFLICTING", "statusCheckRollup": [run_("COMPLETED", "SUCCESS")]}))
    v = view_pr(gh, PR)
    assert v["mergeable"] == "CONFLICTING" and v["checks"] == "SUCCESS" and v["headRefOid"] == HEAD
    assert "statusCheckRollup" not in v
    argv = gh.calls[-1][0]
    assert {"mergeable", "statusCheckRollup"} <= set(argv[argv.index("--json") + 1].split(","))


@pytest.mark.parametrize("raw,want", [("MERGEABLE", "MERGEABLE"), ("CONFLICTING", "CONFLICTING"), ("UNKNOWN", "UNKNOWN"), (None, "UNKNOWN"), ("", "UNKNOWN"), ("weird", "UNKNOWN")])
def test_view_pr_normalizes_mergeable(raw, want):
    assert view_pr(FakeGh((PR, {"mergeable": raw})), PR)["mergeable"] == want


def test_fakegh_defaults_mergeable_with_no_checks():
    v = view_pr(FakeGh((PR, {})), PR)
    assert v["mergeable"] == "MERGEABLE" and v["checks"] == "NONE"


@pytest.mark.parametrize("conclusion", ["FAILURE", "CANCELLED", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED", "cancelled", "timed_out"])
def test_failing_conclusions_count_as_failure(conclusion):
    assert checks_state([run_("COMPLETED", conclusion)]) == "FAILURE"


@pytest.mark.parametrize("state", ["ERROR", "FAILURE"])
def test_status_context_error_counts_as_failure(state):
    assert checks_state([ctx(state)]) == "FAILURE"


@pytest.mark.parametrize("conclusion", ["SUCCESS", "NEUTRAL", "SKIPPED"])
def test_neutral_and_skipped_count_as_success(conclusion):
    assert checks_state([run_("COMPLETED", conclusion), ctx("SUCCESS")]) == "SUCCESS"


@pytest.mark.parametrize("item", [run_("IN_PROGRESS"), run_("QUEUED"), run_("PENDING"), run_("WAITING"), ctx("PENDING"), ctx("EXPECTED")])
def test_unfinished_checks_count_as_pending(item):
    assert checks_state([run_("COMPLETED", "SUCCESS"), item]) == "PENDING"


def test_failure_wins_over_pending_wins_over_success():
    assert checks_state([run_("COMPLETED", "SUCCESS"), run_("IN_PROGRESS"), run_("COMPLETED", "CANCELLED")]) == "FAILURE"
    assert checks_state([run_("COMPLETED", "SKIPPED"), ctx("PENDING"), run_("COMPLETED", "NEUTRAL")]) == "PENDING"
    assert checks_state([ctx("ERROR"), ctx("PENDING")]) == "FAILURE"


def test_no_checks_is_none():
    assert checks_state([]) == "NONE" and checks_state(None) == "NONE"


# -- request_reviewers: typed error on 422 ----------------------------------------------------------

def test_request_reviewers_raises_typed_error_on_422():
    def run(argv, stdin):
        raise RuntimeError("gh api failed: HTTP 422: Reviews may only be requested from collaborators")
    with pytest.raises(ReviewRequestRefused) as e:
        request_reviewers(run, PR, ["outsider"])
    assert e.value.logins == ["outsider"] and "422" in str(e.value)


def test_request_reviewers_passes_other_errors_through():
    def run(argv, stdin):
        raise RuntimeError("gh api failed: HTTP 502")
    with pytest.raises(RuntimeError) as e:
        request_reviewers(run, PR, ["reviewer"])
    assert not isinstance(e.value, ReviewRequestRefused)


def test_request_reviewers_posts_to_rest():
    gh = FakeGh((PR, {}))
    request_reviewers(gh, PR, ["reviewer"])
    argv, doc = gh.calls[-1]
    assert argv[:5] == ["gh", "api", "-X", "POST", "repos/o/r/pulls/9/requested_reviewers"] and doc == {"reviewers": ["reviewer"]}


def test_request_each_still_records_refused_logins():
    gh = FakeGh((PR, {}))
    gh.refuse.add("outsider")
    done, refused = [], []
    GitHub(GCFG, gh).request_each(PR, ["outsider", "reviewer"], done, refused)
    assert done == ["reviewer"] and refused == ["outsider"]


# -- list_prs_for_issue: every state, a real #N reference -------------------------------------------

def pr_(n, state, title="", body=""):
    return {"number": n, "url": f"https://github.com/o/r/pull/{n}", "title": title, "body": body, "state": state, "headRefOid": "a" * 40, "isDraft": False, "author": {"login": "x"}, "mergedAt": None}


LISTED = [
    pr_(1, "OPEN", body="Closes #12"),
    pr_(2, "MERGED", title="fix (#12)"),
    pr_(3, "CLOSED", body="Refs #12, #13"),
    pr_(4, "OPEN", body="Refs #123"),
    pr_(5, "OPEN", body="see other/repo#12"),
    pr_(6, "OPEN", body="issue 12 and #1234"),
    pr_(7, "MERGED", body="#12\nend"),
    pr_(8, "OPEN", body="abc#12"),
]


def fake_list(argv, stdin):
    assert argv[:3] == ["gh", "pr", "list"]
    fake_list.argv = argv
    return json.dumps(LISTED)


def test_list_prs_for_issue_covers_all_states_and_real_references():
    got = list_prs_for_issue(fake_list, "o/r", 12)
    assert [p["number"] for p in got] == [1, 2, 3, 7]
    argv = fake_list.argv
    assert argv[argv.index("--state") + 1] == "all" and argv[argv.index("-R") + 1] == "o/r"
    assert "#12" in argv[argv.index("--search") + 1]
    assert {"number", "title", "body", "state", "mergedAt", "url"} <= set(argv[argv.index("--json") + 1].split(","))


def test_list_prs_for_issue_does_not_match_a_longer_number():
    assert [p["number"] for p in list_prs_for_issue(fake_list, "o/r", 123)] == [4]
    assert list_prs_for_issue(fake_list, "o/r", 1) == []


def test_list_prs_for_issue_accepts_hash_string():
    assert [p["number"] for p in list_prs_for_issue(fake_list, "o/r", "#13")] == [3]


def test_module_exports():
    assert issubclass(github.ReviewRequestRefused, RuntimeError)
