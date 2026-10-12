"""#106: `serve --dry-run`, the shadow mode (ref fm-wave-h-assign, job 3).

gh calls pass only when they are reads (`pr view/list/checks/diff/status`, `issue view/list`, `project
view/list/field-list/item-list`, `api` GET with no `-f/-F/--input`, graphql without `mutation`); every other call
goes to the sink, de-duplicated by a hash of its arguments. `ShadowClient(real, sink, owner=)` reads through and
sinks the writes; the sink is read back on open, so a restart repeats nothing. The shadow state is never seeded from
the real one: the shadow wave is opened with `wave open ... --state`. `wave eta` threads the CLI clock (ruling 4).
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from fridica_research import cli, contracts, shadow, wave
from fridica_research.store import Store
from support import OWNER
from test_driver import world  # noqa: F401 - the fake control socket fixture

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


# -- Xi's findings on 4ceade3 ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("argv", [
    ["gh", "api", "--method=POST", "repos/o/r/issues"], ["gh", "api", "-XPOST", "repos/o/r/issues"], ["gh", "api", "-X", "POST", "repos/o/r/issues"],
    ["gh", "api", "-Xpatch", "repos/o/r/issues/1"], ["gh", "api", "repos/o/r/issues", "-fbody=x"], ["gh", "api", "repos/o/r/issues", "-Fx=y"],
    ["gh", "api", "repos/o/r/issues", "--raw-field=body=x"], ["gh", "api", "repos/o/r/issues", "--field", "x=y"], ["gh", "api", "repos/o/r/issues", "--input=body.json"],
    ["gh", "api", "repos/o/r/issues", "--some-new-flag"], ["gh", "api", "repos/o/r/issues", "-Z"]])
def test_unknown_or_glued_write_flags_fail_closed(argv): assert not shadow.is_read(argv)


def test_known_read_flags_on_a_get_still_pass():
    assert shadow.is_read(["gh", "api", "--paginate", "repos/o/r/pulls/1/reviews?per_page=100"])
    assert shadow.is_read(["gh", "api", "-X", "GET", "repos/o/r"]) and shadow.is_read(["gh", "api", "--method=GET", "repos/o/r"])


def test_shadow_posts_get_unique_numeric_slack_ts(tmp_path):
    from fridica_research.machine import ts_key
    c = shadow.ShadowClient(Real(), shadow.Sink(tmp_path / "s.jsonl", clock=lambda: 1_791_834_000.25), owner=OWNER)
    for i in range(3): c.post_message(THREAD, contracts.PostRequest("report", f"p{i}\nref: fm-x/{i}"))
    ts = [m["ts"] for m in c.thread(THREAD).messages if m["sender"] == OWNER]
    assert len(set(ts)) == 3 and all(ts_key(t) for t in ts) and ts == sorted(ts, key=ts_key)
    again = shadow.ShadowClient(Real(), shadow.Sink(tmp_path / "s.jsonl"), owner=OWNER)
    assert [m["ts"] for m in again.thread(THREAD).messages if m["sender"] == OWNER] == ts  # stable across a restart


def test_a_half_written_last_line_is_skipped_on_reopen(tmp_path):
    path = tmp_path / "s.jsonl"
    sink = shadow.Sink(path)
    sink.record("gh", "k1", argv=["gh", "pr", "merge"])
    with path.open("a") as f: f.write('{"kind": "gh", "key": "k2", "ar')  # the process died mid-write
    again = shadow.Sink(path)
    assert again.keys == {"k1"}
    assert again.record("gh", "k2", argv=["x"]) and shadow.Sink(path).keys == {"k1", "k2"}


def gated(tmp_path):
    sink = shadow.Sink(tmp_path / "s.jsonl")
    def real(argv, stdin):
        assert shadow.is_read(argv, stdin), argv
        if argv[:3] == ["gh", "api", "graphql"]:
            return json.dumps({"data": {"repository": {"id": "R1", "issue": {"id": "I_1"}}}})
        return "[]"
    return sink, shadow.runner(real, sink)


def test_board_writes_get_answers_their_callers_can_read(tmp_path):
    from fridica_research import board, github
    import dataclasses as dc
    from support import CFG
    cfg = dc.replace(CFG, board=dc.replace(CFG.board, enabled=True, owner="o", number=1, repo="o/r"))
    sink, run = gated(tmp_path)
    api = board.Projects(cfg, runner=run)
    assert isinstance(api.graphql(board.M_SET_TEXT, project="P", item="I", field="F", v="x"), dict)  # no KeyError 'data'
    assert api.graphql(board.M_ADD_ITEM, project="P", content="C")["addProjectV2ItemById"]["item"]["id"]
    n = api.create_issue("t", "b")  # no IndexError
    assert isinstance(n, int) and n > 0 and api.create_issue("t", "b") == n  # the same write, the same answer
    gh = github.GitHub(cfg, runner=run)
    assert isinstance(gh.milestone("o/r", "R9"), int)  # no KeyError 'number'
    assert [r["kind"] for r in sink.rows] == ["gh"] * 4  # every distinct write logged once


def test_a_board_sync_with_several_mutations_logs_all_of_them(tmp_path):
    from fridica_research import board
    import dataclasses as dc
    from support import CFG
    cfg = dc.replace(CFG, board=dc.replace(CFG.board, enabled=True, owner="o", number=1, repo="o/r"))
    sink, run = gated(tmp_path)
    api = board.Projects(cfg, runner=run)
    api._project = board.ProjectIds("P", {f: {"id": f"F_{f}", "options": {"Implement": "O1", "In Progress": "O2"}} for f in ("Stage", "Status", "Started", "Projected finish", "ETA")})
    api.item_for_issue = lambda repo, n: f"ITEM_{n}"
    api.set_option(69, "Stage", "Implement")
    api.set_option(69, "Status", "In Progress")
    api.set_dates(69, started="2026-10-12", projected_finish="2026-10-13")
    api.set_text(69, "ETA", "ETA 10-12 15:35 ET")
    api.edit_issue(69, "--add-assignee", "imp1")
    rows = [r for r in sink.rows if r["kind"] == "gh"]
    assert len(rows) == 6 and sum(1 for r in rows if "mutation" in (r.get("stdin") or "")) == 5


def test_restart_through_a_claim_finds_the_shadow_post_and_its_ts_sorts(world, tmp_path):  # noqa: F811 - the imported fixture
    from fridica_research.machine import ts_key
    from test_driver import EXPLORER_REPORT, drain, make_driver, result, root_text
    server, cfg = world
    thread = "T1:C1:1700000000.000100"
    server.root(thread, OWNER, root_text())
    drv = make_driver(cfg)
    drain(drv)
    server.finish_job(thread, server.job_of(thread, "explorer")["id"], result(report=EXPLORER_REPORT))
    drain(drv)
    s = drv.store.load(thread)
    claim = server.view(thread)["messages"][-1]["text"]
    ref = s.waiting["id"].rsplit("/", 1)[0] + "/claim-shadow"
    s.claim.update(status="pending", ts=None)
    s.phase, s.waiting = "post", {"kind": "post", "id": ref, "action": {"thread": thread, "post_kind": "study_claim", "text": claim.rsplit("\nref: ", 1)[0] + f"\nref: {ref}", "details": None}}
    drv.store.save(s)
    path = tmp_path / "shadow.jsonl"
    claims_before = sum(1 for m in server.view(thread)["messages"] if m["meta"] and m["meta"]["kind"] == "study_claim")
    for _ in range(2):  # the first run sends the claim to the sink; the restart finds it there through own_post_with_ref
        d = make_driver(cfg, store=Store(cfg.state_file))
        d.client = shadow.ShadowClient(d.client, shadow.Sink(path), owner=OWNER)
        drain(d)
    s2 = d.store.load(thread)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["ref"] for r in rows if r["kind"] == "post_message"] == [ref]
    assert s2.claim["status"] == "settling" and s2.claim["ts"] == rows[0]["ts"] and ts_key(s2.claim["ts"])
    assert sum(1 for m in server.view(thread)["messages"] if m["meta"] and m["meta"]["kind"] == "study_claim") == claims_before


# -- Xi's findings on bebdf38 ------------------------------------------------------------------------------------------
class CorpusReal(Real):
    """The daemon as corpus 006 leaves it: the peer's claim at 1700000000.000700 is in the thread, ours is not."""

    def __init__(self, peer: dict):
        super().__init__()
        self.messages = [{"ts": peer["ts"], "sender": peer["sender"], "text": peer["text"]}]


def test_corpus_006_restarted_in_shadow_loses_the_claim_as_recorded(tmp_path):
    from pathlib import Path
    from fridica_research import replay
    from fridica_research.driver import Driver
    cfg, start, events, _, final = replay.load(Path(__file__).parent / "bootstrap" / "006_peer_claim_conflict")
    i = next(k for k, e in enumerate(events) if e.kind == "peer_post")
    state, _ = replay.fold(cfg, start, events[: i + 1])  # our alpha claim is waiting to be posted; the peer claimed alpha first
    assert state.waiting and state.waiting["kind"] == "post" and state.claim["slug"] == "alpha"
    cfg = dataclasses.replace(cfg, state_path=str(tmp_path / "shadow.sqlite3"))
    store = Store(cfg.state_file)
    store.save(state)
    store.close()
    clock = lambda: 1_700_000_000.9  # noqa: E731
    path = tmp_path / "s.jsonl"
    for _ in range(2):  # the post goes to the sink; the restart's recover finds it there through own_post_with_ref
        d = Driver(cfg, shadow.ShadowClient(CorpusReal(events[i].data), shadow.Sink(path, clock=clock), owner=cfg.owner), Store(cfg.state_file), llm=lambda *a: {}, clock=clock, sleep=lambda s: None)
        d.recover()
    s = d.store.load(state.thread)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]["ts"] == "1700000000.900000"
    assert s.peer_claims["alpha"]["ts"] == final["peer_claims"]["alpha"]["ts"] and s.claim["slug"] != "alpha"  # we lose alpha, as the corpus records


def test_ts_is_the_clock_microsecond_and_strictly_increasing(tmp_path):
    from fridica_research.machine import ts_key
    t = [1_700_000_000.0000001, 1_700_000_000.0000004, 1_699_999_999.0]  # same microsecond twice, then a clock step back
    sink = shadow.Sink(tmp_path / "s.jsonl", clock=lambda: t.pop(0))
    for k in ("k1", "k2", "k3"): sink.record("post_message", k, thread=THREAD, text=f"{k}\nref: {k}")
    ts = [r["ts"] for r in sink.rows]
    assert ts[0] == "1700000000.000000" and ts[1] == "1700000000.000001" and ts[2] == "1700000000.000002"
    assert [ts_key(x) for x in ts] == sorted(ts_key(x) for x in ts)
    with (tmp_path / "s.jsonl").open("a") as f: f.write('{"kind": "post_mes')  # a torn last line
    again = shadow.Sink(tmp_path / "s.jsonl", clock=lambda: 1_600_000_000.0)
    again.record("post_message", "k4", thread=THREAD, text="k4\nref: k4")
    assert again.rows[-1]["ts"] == "1700000000.000003"  # after the skipped line, still after the last kept row


@pytest.mark.parametrize("argv", [
    ["gh", "api", "-iXPOST", "repos/o/r/issues"], ["gh", "api", "-ifbody=x", "repos/o/r/issues"], ["gh", "api", "-iX", "POST", "repos/o/r/issues"],
    ["gh", "api", "-XPOST", "repos/o/r/issues"], ["gh", "api", "-ipaginate", "repos/o/r"], ["gh", "api", "graphql", "--input", "q.json"],
    ["gh", "api", "graphql", "--input=q.json"]])
def test_clustered_short_flags_and_graphql_input_files_are_writes(argv): assert not shadow.is_read(argv)


def test_real_reads_still_pass_after_the_cluster_rule():
    assert shadow.is_read(["gh", "api", "-XGET", "repos/o/r"]) and shadow.is_read(["gh", "api", "-q", ".x", "repos/o/r"]) and shadow.is_read(["gh", "api", "-i", "repos/o/r"])
    assert shadow.is_read(["gh", "api", "-H", "Accept: x", "repos/o/r"]) and shadow.is_read(["gh", "api", "-q.x", "repos/o/r"])
    assert shadow.is_read(["gh", "api", "graphql", "--input", "-"], json.dumps({"query": "query { viewer { login } }"}))
    assert shadow.is_read(["gh", "api", "graphql", "-f", "query=query { viewer { login } }"])


def test_a_corrupt_middle_line_raises_with_its_line_number(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text('{"kind": "gh", "key": "k1"}\n{"kind": "gh", "ke\n{"kind": "gh", "key": "k3"}\n')
    with pytest.raises(shadow.SinkCorrupt, match="line 2"): shadow.Sink(path)


def test_a_torn_last_line_reopens_with_a_warning(tmp_path, caplog):
    path = tmp_path / "s.jsonl"
    path.write_text('{"kind": "gh", "key": "k1"}\n{"kind": "gh", "ke')
    assert shadow.Sink(path).keys == {"k1"} and "line 2" in caplog.text and "torn" in caplog.text


# -- Xi's findings on 6a3feb9 ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("argv", [
    ["gh", "api", "repos/o/r/issues", "-F", "title=x", "-q", "graphql"], ["gh", "api", "-X", "POST", "repos/o/r/issues", "-q", "graphql"],
    ["gh", "api", "graphql", "-F", "query=@q.graphql"], ["gh", "api", "graphql", "--field", "query=@q.graphql"], ["gh", "api", "graphql", "--field=query=@q.graphql"],
    ["gh", "api", "graphql", "-Fquery=@q.graphql"], ["gh", "api", "-q", ".x", "--jq", "graphql", "repos/o/r/issues", "-f", "a=b"], ["gh", "api", "-q", "graphql"]])
def test_the_endpoint_is_the_first_positional_and_field_files_are_writes(argv): assert not shadow.is_read(argv)


def test_graphql_as_the_endpoint_still_reads_after_flags():
    assert shadow.is_read(["gh", "api", "-H", "Accept: x", "graphql", "-f", "query=query { viewer { login } }"])
    assert shadow.is_read(["gh", "api", "graphql", "-F", "owner=o", "-f", "query=query($owner:String!){ user(login:$owner){ id } }"])
    assert shadow.is_read(["gh", "api", "-q", "graphql", "repos/o/r"])  # graphql as a --jq value, on a GET endpoint: a read


def test_a_last_row_without_its_newline_is_kept_and_the_next_row_is_not_glued(tmp_path, caplog):
    path = tmp_path / "s.jsonl"
    sink = shadow.Sink(path)
    for k in ("k1", "k2"): sink.record("gh", k, argv=["gh", "pr", "merge", k])
    path.write_bytes(path.read_bytes()[:-1])  # only the final newline is lost
    again = shadow.Sink(path)
    assert again.keys == {"k1", "k2"}
    again.record("gh", "k3", argv=["gh", "pr", "merge", "k3"])
    assert shadow.Sink(path).keys == {"k1", "k2", "k3"} and path.read_bytes().endswith(b"\n")
    assert "torn" not in caplog.text
