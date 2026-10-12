"""#72 (the #61 login-lookup note): a failed `gh api user` lookup never stops the board sync.

`Projects.login()` raises `LoginLookupFailed` and caches nothing on failure; `sync_comment` skips the comment step
for that pass with one finding, deduplicated like `field_setup_finding`, and the next pass retries. A post whose
id gh did not print stores `commented` before the lookup, so the post is never repeated."""
from __future__ import annotations

import json

import pytest

import test_board as tb
from fridica_research import board
from test_issue60 import THREAD, adopted_world, legacy_cards, patches, posts

REPO = "chengcli/fridica-research"
MARKER = f"Adopted as the study card of fridica-research study {THREAD}"


class NoIdGh(tb.FakeGh):
    """gh posts the comment but prints no URL, so the id comes from the marker lookup."""

    def __call__(self, argv, stdin):
        out = super().__call__(argv, stdin)
        return "" if argv[:3] == ["gh", "issue", "comment"] else out


def user_calls(gh: tb.FakeGh) -> int: return len(gh.argv("gh", "api", "user"))
def cards(meta: dict) -> dict: return json.loads(meta[f"board:{THREAD}"])


def test_login_failure_is_typed_and_not_cached():
    b, gh, _ = tb.make()
    gh.fail_once = "api user"
    with pytest.raises(board.LoginLookupFailed):
        b.api.login()
    assert b.api.login() == "driver-bot" and user_calls(gh) == 2  # the failure was not cached: the second call asked gh again
    assert b.api.login() == "driver-bot" and user_calls(gh) == 2  # the success is


def test_failed_lookup_skips_the_pass_then_the_next_pass_patches_the_one_comment():
    gh = tb.FakeGh()
    b, gh, meta, w = adopted_world(gh)
    old = gh.add_comment(REPO, 42, MARKER + " (generation 1).", gh.login)
    legacy_cards(meta)
    gh.fail_once = "api user"
    first = b.sync(w.state)
    assert posts(gh) == [] and patches(gh) == [] and len(first) == 1 and "gh api user" in first[0]
    assert cards(meta)["login_lookup_finding"] == first[0] and cards(meta).get("comment_id") is None and cards(meta)["commented"]
    assert gh.argv("gh", "issue", "create")  # the rest of the sync went on: the stage card was opened
    second = b.sync(w.state)
    assert second == [] and posts(gh) == [] and len(gh.comments[42]) == 1
    assert [a[a.index("PATCH") + 1] for a in patches(gh)] == [f"repos/{REPO}/issues/comments/{old['id']}"]
    assert cards(meta)["comment_id"] == old["id"] and "| Stage |" in old["body"]


def test_failed_lookup_before_a_first_post_posts_nothing_then_exactly_one():
    b, gh, meta, w = adopted_world(NoIdGh())
    gh.fail_once = "api user"
    assert len(b.sync(w.state)) == 1 and posts(gh) == [] and 42 not in gh.comments
    assert b.sync(w.state) == [] and len(posts(gh)) == 1
    [comment] = gh.comments[42]
    assert cards(meta)["comment_id"] == comment["id"]
    w.state.mention_misses = {"Debate/U9": 2}
    b.sync(w.state)  # changed text: the one comment is PATCHed
    assert len(posts(gh)) == 1 and len(gh.comments[42]) == 1 and len(patches(gh)) == 1 and '{"Debate/U9": 2}' in comment["body"]


def test_the_login_finding_is_recorded_once_over_failing_passes():
    gh = tb.FakeGh(fail_on="api user")
    b, gh, meta, w = adopted_world(gh)
    found = [f for _ in range(3) for f in b.sync(w.state)]
    assert len(found) == 1 and cards(meta)["login_lookup_finding"] == found[0]
    assert user_calls(gh) == 3 and posts(gh) == []  # retried each pass, never cached, nothing posted
    gh.fail_on = None
    assert b.sync(w.state) == [] and len(posts(gh)) == 1


def test_a_post_without_id_is_stored_before_the_lookup_and_never_repeated():
    b, gh, meta, w = adopted_world(NoIdGh())
    gh.fail_once = "/comments?per_page"  # the marker lookup after the post fails
    b.sync(w.state)
    assert len(posts(gh)) == 1 and cards(meta)["commented"] and cards(meta)["comment_id"] is None
    b.sync(w.state)
    [comment] = gh.comments[42]
    assert len(posts(gh)) == 1 and cards(meta)["comment_id"] == comment["id"]
