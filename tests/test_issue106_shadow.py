"""#106: `serve --dry-run`, the shadow mode (ref fm-wave-h-assign, job 3).

gh calls pass only when they are reads (`pr view/list/checks/diff/status`, `issue view/list`, `project
view/list/field-list/item-list`, `api` GET with no `-f/-F/--input`, graphql without `mutation`); every other call
goes to the sink, de-duplicated by a hash of its arguments. `ShadowClient(real, sink, owner=)` reads through and
sinks the writes; the sink is read back on open, so a restart repeats nothing. The shadow state is never seeded from
the real one: the shadow wave is opened with `wave open ... --state`. `wave eta` threads the CLI clock (ruling 4).
"""
from __future__ import annotations

import json

import pytest

from fridica_research import cli, contracts, shadow, wave
from fridica_research.store import Store
from support import OWNER

THREAD = "T1:C1:1791769427.483009"


class Real:
    """The daemon: records every call; a write reaching it is the failure the shadow exists to prevent."""

    def __init__(self):
        self.reads, self.writes = [], []
        self.messages = [{"ts": "1.000001", "sender": "U0X", "text": "hello"}]

    def events(self, after, limit=1000):
        self.reads.append(("events", after))
        return {"events": [], "next": after or 0, "scanned": 0}

    def thread(self, thread):
        self.reads.append(("thread", thread))
        return contracts.ThreadView(messages=list(self.messages))

    def post_message(self, thread, req): self.writes.append(("post_message", thread))
    def post_root(self, channel, req): self.writes.append(("post_root", channel))
    def delegate(self, thread, body): self.writes.append(("delegate", thread))
    def stop_worker(self, thread, worker_id): self.writes.append(("stop_worker", thread))
    def set_driver(self, thread, driver): self.writes.append(("set_driver", thread))
    def instruct(self, thread, text, client_id, actor="owner"): self.writes.append(("instruct", thread))


@pytest.mark.parametrize("argv", [
    ["gh", "pr", "view", "71", "-R", "o/r", "--json", "x"], ["gh", "pr", "list", "-R", "o/r"], ["gh", "pr", "checks", "71"], ["gh", "pr", "diff", "71"],
    ["gh", "pr", "status"], ["gh", "issue", "view", "106"], ["gh", "issue", "list"], ["gh", "project", "view", "1"], ["gh", "project", "list"],
    ["gh", "project", "field-list", "1"], ["gh", "project", "item-list", "1"], ["gh", "api", "repos/o/r/pulls/1/reviews"], ["gh", "api", "-X", "GET", "repos/o/r"],
    ["gh", "api", "graphql", "-f", "query=query { viewer { login } }"]])
def test_reads_pass(argv): assert shadow.is_read(argv)


@pytest.mark.parametrize("argv", [
    ["gh", "pr", "merge", "71"], ["gh", "pr", "create"], ["gh", "pr", "edit", "71"], ["gh", "pr", "review", "71"], ["gh", "issue", "edit", "1"],
    ["gh", "issue", "comment", "1"], ["gh", "project", "item-edit"], ["gh", "api", "-X", "POST", "repos/o/r/pulls/1/requested_reviewers", "--input", "-"],
    ["gh", "api", "-X", "PATCH", "repos/o/r/issues/1", "-F", "milestone=2"], ["gh", "api", "repos/o/r/issues/1/comments", "-f", "body=x"],
    ["gh", "api", "graphql", "-f", "query=mutation { addComment }"], ["gh", "api", "--method", "DELETE", "repos/o/r"], ["git", "push"]])
def test_everything_else_is_blocked(argv): assert not shadow.is_read(argv)


def test_gh_writes_go_to_the_sink_once_by_argument_hash(tmp_path):
    calls = []
    sink = shadow.Sink(tmp_path / "s.jsonl")
    run = shadow.runner(lambda argv, stdin: (calls.append(argv), '{"ok":1}')[1], sink)
    assert run(["gh", "pr", "view", "71"], None) == '{"ok":1}'
    write = ["gh", "api", "-X", "POST", "repos/o/r/pulls/1/requested_reviewers", "--input", "-"]
    for _ in range(3): run(write, '{"reviewers":["a"]}')
    run(write, '{"reviewers":["b"]}')  # other arguments: another write
    assert calls == [["gh", "pr", "view", "71"]]
    rows = [json.loads(line) for line in (tmp_path / "s.jsonl").read_text().splitlines()]
    assert [r["kind"] for r in rows] == ["gh", "gh"] and rows[0]["argv"] == write


def test_client_writes_never_reach_the_daemon_and_are_recorded_once(tmp_path):
    real, path = Real(), tmp_path / "s.jsonl"
    c = shadow.ShadowClient(real, shadow.Sink(path), owner=OWNER)
    req = contracts.PostRequest("report", "checking in\nref: fm-wave-h-job1/checkin")
    for _ in range(3): c.post_message(THREAD, req)
    c.post_root("C1", contracts.PostRequest("report", "root\nref: fm-x-root"))
    c.delegate(THREAD, {"action_id": "a1"})
    c.stop_worker(THREAD, "w1")
    c.set_driver(THREAD, "fridica")
    c.instruct(THREAD, "wake\nref: fm-wave-h-job1/escalate", "wake-abc12345")
    c.events(0)
    assert real.writes == [] and ("events", 0) in real.reads
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["kind"] for r in rows] == ["post_message", "post_root", "delegate", "stop_worker", "set_driver", "instruct"]
    assert rows[0]["thread"] == THREAD and rows[0]["ref"] == "fm-wave-h-job1/checkin" and "checking in" in rows[0]["text"]


def test_the_ref_probe_sees_shadow_posts_and_a_restart_repeats_nothing(tmp_path):
    real, path = Real(), tmp_path / "s.jsonl"
    c = shadow.ShadowClient(real, shadow.Sink(path), owner=OWNER)
    c.post_message(THREAD, contracts.PostRequest("report", "x\nref: fm-wave-h-job2/checkin"))
    assert c.thread(THREAD).own_post_with_ref("fm-wave-h-job2/checkin", OWNER) is not None
    assert real.writes == []
    again = shadow.ShadowClient(real, shadow.Sink(path), owner=OWNER)  # a restart: the sink is read back on open
    assert again.thread(THREAD).own_post_with_ref("fm-wave-h-job2/checkin", OWNER) is not None
    again.post_message(THREAD, contracts.PostRequest("report", "x\nref: fm-wave-h-job2/checkin"))
    assert len(path.read_text().splitlines()) == 1


def wave_open(state, *extra):
    return cli.main(["--config", str(state.parent / "c.toml"), "wave", "open", THREAD, "--name", "h", "--repo", "o/r", "--state", str(state),
                     "--job", "issue=106;files=src/x.py;implementer=U0IMP1:imp1;auditor=U0AUD1:aud1;pr_eta=2026-10-12T19:35Z;review_eta=2026-10-12T19:55Z", *extra])


def config(tmp_path):
    (tmp_path / "c.toml").write_text(f'owner = "{OWNER}"\nsocket_path = "{tmp_path}/sock"\nstate_path = "{tmp_path}/real.sqlite3"\nchannels = ["C1"]\n')


def test_wave_open_state_opens_the_shadow_wave_and_leaves_the_real_store_alone(tmp_path):
    config(tmp_path)
    shadow_state = tmp_path / "shadow.sqlite3"
    assert wave_open(shadow_state) == 0
    assert Store(shadow_state).get_meta(wave.wave_key(THREAD)) is not None
    assert not (tmp_path / "real.sqlite3").exists() or Store(tmp_path / "real.sqlite3").get_meta(wave.wave_key(THREAD)) is None


def test_serve_dry_run_uses_its_own_state_and_never_seeds_it(tmp_path, monkeypatch):
    config(tmp_path)
    real_store = Store(tmp_path / "real.sqlite3")
    real_store.set_meta("cursor", "41")
    real_store.close()
    seen = {}

    class FakeDriver:
        def __init__(self, cfg, client, store, **kw): seen.update(state=cfg.state_file, client=client, store=store, kw=kw)
        def serve(self, once=False): seen["served"] = once

    monkeypatch.setattr(cli, "Driver", FakeDriver)
    monkeypatch.setattr(cli, "build_client", lambda cfg: Real())
    assert cli.main(["--config", str(tmp_path / "c.toml"), "serve", "--once", "--dry-run"]) == 0
    assert str(seen["state"]) == str(tmp_path / "real.sqlite3") + ".dry-run" and isinstance(seen["client"], shadow.ShadowClient)
    assert seen["served"] is True and Store(seen["state"]).get_meta("cursor") is None  # not seeded from the real state


def test_wave_eta_passes_the_cli_clock(tmp_path, monkeypatch):
    config(tmp_path)
    state = tmp_path / "shadow.sqlite3"
    assert wave_open(state) == 0
    got = {}
    real_set_eta = wave.set_eta

    def set_eta(w, n, *, pr_eta=None, review_eta=None, now=None):
        got["now"] = now
        return real_set_eta(w, n, pr_eta=pr_eta, review_eta=review_eta)

    monkeypatch.setattr(wave, "set_eta", set_eta)
    monkeypatch.setattr(cli.time, "time", lambda: 1_791_832_000.0)
    assert cli.main(["--config", str(tmp_path / "c.toml"), "wave", "eta", THREAD, "1", "--pr-eta", "2026-10-12T20:00Z", "--state", str(state)]) == 0
    assert got["now"] == 1_791_832_000.0


def test_a_graphql_query_on_stdin_is_a_read_and_a_mutation_is_not():
    argv = ["gh", "api", "graphql", "--input", "-"]
    assert shadow.is_read(argv, json.dumps({"query": "query($o:String!){ user(login:$o){ id } }"}))
    assert not shadow.is_read(argv, json.dumps({"query": "mutation($p:ID!){ updateProjectV2ItemFieldValue(input:{}) { clientMutationId } }"}))
