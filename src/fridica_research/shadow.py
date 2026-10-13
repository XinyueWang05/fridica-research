"""`serve --dry-run`, the shadow mode (#106): reads stay real, writes never leave the process.

`is_read(argv)` is the allow-list of gh reads: `pr view/list/checks/diff/status`, `issue view/list`,
`project view/list/field-list/item-list`, `api` GET with no `-f/-F/--input` (graphql without `mutation`).
`runner(real, sink)` passes those to the real gh runner and sends everything else to the `Sink`, de-duplicated by a
hash of the arguments. `ShadowClient(real, sink, owner=)` reads the daemon through `real` and sinks every write;
its thread view carries the sink's posts as the owner's, so the driver's `ref:` probe sees them and a durable post is
not sent again. The sink is read back on open, so a restart repeats nothing. The shadow store is the caller's: it is
never seeded from the real state (open the shadow wave with `wave open ... --state`).
"""
from __future__ import annotations

import dataclasses
import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo
import json
import logging
import re
import time
from pathlib import Path

from . import contracts
from .machine import ts_key

log = logging.getLogger("fridica_research.shadow")

GH_READS = {"pr": {"view", "list", "checks", "diff", "status"}, "issue": {"view", "list"}, "project": {"view", "list", "field-list", "item-list"}}
API_BODY_FLAGS = {"-f", "-F", "--field", "--raw-field", "--input"}
API_READ_FLAGS = {"--paginate", "--slurp", "--include", "-i", "--silent", "--verbose", "--jq", "-q", "--template", "-t", "--cache", "--header", "-H", "--hostname"}
API_VALUE_FLAGS = {"-X", "--method", "--jq", "-q", "--template", "-t", "--cache", "--header", "-H", "--hostname", *API_BODY_FLAGS}


def api_flags(args: list[str]) -> list[tuple[str, str | None]]:
    """`gh api` flags as (name, value): `--name=v`, `-Xv` glued, or the value in the next token."""
    return api_args(args)[0]


def api_args(args: list[str]) -> tuple[list[tuple[str, str | None]], list[str]]:
    """`gh api` arguments as (flags, positionals); a flag's value (glued, `=`, or the next token) is never a positional."""
    out, pos, i = [], [], 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            if not eq and name in API_VALUE_FLAGS and i + 1 < len(args): val, i = args[i + 1], i + 1
            out.append((name, val if (eq or name in API_VALUE_FLAGS) else None))
        elif a.startswith("-") and len(a) > 1:
            name, val = a[:2], a[2:] or None
            if val is not None and name not in API_VALUE_FLAGS: name, val = a, None  # a cluster (-iXPOST, -ifbody=x): unknown, so a write
            elif val is None and name in API_VALUE_FLAGS and i + 1 < len(args): val, i = args[i + 1], i + 1
            out.append((name, val))
        else: pos.append(a)
        i += 1
    return out, pos


def is_read(argv: list[str], stdin: str | None = None) -> bool:
    """True only for the listed gh reads; anything else (and anything that is not gh) is a write. It fails closed: a
    `gh api` call is a read only as a GET (`-X`/`--method`, glued or not) whose every flag is a known read flag. Its
    endpoint is the first positional argument after `api`; only when that is `graphql` is the call judged by its query, in
    argv (`-f query=...`) or on stdin (`--input -`, as `board.Projects.graphql` sends it). `--input <file>` and
    `-F key=@file` read a file the shadow cannot see, so they are writes."""
    a = [str(x) for x in argv or []]
    if len(a) < 3 or a[0] != "gh": return False
    if a[1] in GH_READS: return a[2] in GH_READS[a[1]]
    if a[1] != "api": return False
    flags, pos = api_args(a[2:])
    if not pos: return False  # no endpoint: nothing known to be a read
    if any((v or "").upper() != "GET" for n, v in flags if n in ("-X", "--method")): return False
    if pos[0] == "graphql":  # the endpoint is the first positional, never a flag's value (`-q graphql`)
        if any(n == "--input" and v != "-" for n, v in flags): return False  # a query in a file is not read here: a write
        if any(n in ("-F", "--field") and (v or "").partition("=")[2].startswith("@") for n, v in flags): return False  # `-F query=@file`: a file read
        if any(n not in API_READ_FLAGS and n not in API_VALUE_FLAGS for n, _ in flags): return False  # an unknown or clustered flag
        return not any("mutation" in x.lower() for x in [*a, stdin or ""])  # allowed unless it mutates
    return all(n in API_READ_FLAGS or n in ("-X", "--method") for n, _ in flags)


def digest(*parts) -> str: return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


class SinkCorrupt(ValueError):
    """A line of the sink that is not JSON and is not the torn last line: the record of what was written is damaged."""


def ts_after(at: float, last: str | None) -> str:
    """`at` as a Slack ts (`f"{at:.6f}"`), bumped by 1 us while it is not after `last`."""
    ts = f"{at:.6f}"
    if last is None or ts_key(ts) > ts_key(last): return ts
    sec, us = ts_key(last)
    us += 1
    return f"{sec + us // 1_000_000}.{us % 1_000_000:06d}"


NO_THREAD = "(no thread)"
JOB_REF = re.compile(r"\bfm-wave-\S+?-job(\d+)\b")


def job_of(ref: str | None) -> str:
    """`job<N>` from a `fm-wave-<w>-jobN` ref (any suffix), else empty."""
    m = JOB_REF.search(ref or "")
    return f"job{m.group(1)}" if m else ""


def gh_target(argv: list[str], stdin: str | None = None) -> tuple[str, int | None]:
    """The repo (`-R`, `--repo` or a `repos/<o>/<r>/` endpoint) and the PR or issue number a gh write names, if any."""
    a = [str(x) for x in argv or []]
    repo, num = "", None
    for flag in ("-R", "--repo"):
        if flag in a and a.index(flag) + 1 < len(a): repo = a[a.index(flag) + 1]
    for x in a:
        m = re.match(r"/?repos/([^/]+/[^/]+)/(?:pulls|issues)/(\d+)", x)
        if m: return m.group(1), int(m.group(2))
    if len(a) > 3 and a[1] in ("pr", "issue") and a[3].isdigit(): num = int(a[3])
    elif len(a) > 3 and a[1] in ("pr", "issue"):
        m = re.match(r"https://github\.com/([^/]+/[^/]+)/(?:pull|issues)/(\d+)", a[3])
        if m: repo, num = m.group(1), int(m.group(2))
    return repo, num


def attribute(argv: list[str], stdin: str | None, waves) -> tuple[str, str]:
    """(thread, job) of a gh write: the wave job whose recorded PR or issue is the number the write names, in the same
    repo only (never across repos); `("", "")` when no number, no repo or no job matches (the "(no thread)" rows)."""
    repo, num = gh_target(argv, stdin)
    if not repo or num is None: return "", ""
    for thread, w in waves or []:
        for n, j in enumerate(w.jobs, 1):
            if (j.repo or w.repo) != repo: continue
            if num == j.rnd().pr.get("number") or num == j.issue: return thread, f"job{n}"
    return "", ""


class Sink:
    """One JSON line per would-be write at `path`; each key is written once, across restarts. Each row carries a
    Slack-style ts from the clock's microseconds, strictly after every earlier row's, so shadow posts sort among the
    thread's real posts. On reopen a torn last line (no newline: the process died mid-write) is logged, cut off the
    file and its write redone; any other line that is not JSON raises `SinkCorrupt` with its line number."""

    def __init__(self, path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.rows: list[dict] = []
        if self.path.exists():
            data = self.path.read_bytes()
            lines = data.split(b"\n")
            offset = 0
            for n, raw in enumerate(lines, 1):
                torn = n == len(lines) and raw.strip() != b""  # text after the last newline: a torn row, or a whole row without its newline
                if raw.strip():
                    try: row = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        if not torn: raise SinkCorrupt(f"{self.path}: line {n} is not valid JSON; the shadow record is damaged") from None
                        log.warning("shadow sink %s: torn last line %d skipped and cut (the process died mid-write); its write is redone", self.path, n)
                        with self.path.open("r+b") as f: f.truncate(offset)
                        break
                    if not isinstance(row, dict) or "key" not in row: raise SinkCorrupt(f"{self.path}: line {n} is not a sink row")
                    self.rows.append(row)
                    if torn:  # a whole last row that only lost its newline: restore it, so the next row is not glued on
                        with self.path.open("ab") as f: f.write(b"\n")
                offset += len(raw) + 1
        self.keys = {r["key"] for r in self.rows}

    def last_ts(self) -> str | None:
        return max((r["ts"] for r in self.rows if r.get("ts")), key=ts_key, default=None)

    def record(self, kind: str, key: str, **fields) -> bool:
        """Append the action unless its key is already in the sink; True when it was new."""
        if key in self.keys: return False
        at = self.clock()
        row = {"at": at, "ts": ts_after(at, self.last_ts()), "kind": kind, "key": key, "thread": "", "job": "", **fields}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f: f.write(json.dumps(row, sort_keys=True) + "\n")
        self.rows.append(row)
        self.keys.add(key)
        return True

    def posts(self, thread: str) -> list[dict]: return [r for r in self.rows if r["kind"] == "post_message" and r.get("thread") == thread]


def runner(real, sink: Sink, waves=None):
    """A gh runner: reads go to `real`, every other call to the sink (keyed by a hash of argv and stdin) and returns `{}`.
    `waves()` gives the shadow store's `(thread, Wave)` pairs, from which each row's thread and job are inferred."""
    def run(argv, stdin):
        if is_read(argv, stdin): return real(argv, stdin)
        key = "gh:" + digest(list(argv), stdin)
        thread, job = attribute(list(argv), stdin, waves() if waves else [])
        sink.record("gh", key, argv=list(argv), stdin=stdin, thread=thread, job=job)
        return answer(list(argv), stdin, key)
    return run


def answer(argv: list[str], stdin: str | None, key: str) -> str:
    """A synthetic answer shaped for the caller of a blocked write, the same for the same write: a graphql mutation gets
    `data` with its field (`addProjectV2ItemById` an item id), `issue/pr create` a URL, `issue comment` a comment URL,
    a REST write a JSON object with `number` and `id` (a created milestone), anything else an empty string."""
    num = 900_000 + int(key[-8:], 16) % 100_000
    repo = argv[argv.index("-R") + 1] if "-R" in argv and argv.index("-R") + 1 < len(argv) else "shadow/shadow"
    if argv[:3] == ["gh", "api", "graphql"]:
        m = re.search(r"mutation[^{]*\{\s*(\w+)", stdin or " ".join(argv))
        name = m.group(1) if m else "mutation"
        return json.dumps({"data": {name: {"item": {"id": f"SHADOW_ITEM_{key[3:15]}"}} if name == "addProjectV2ItemById" else {}}})
    if argv[1:3] in (["issue", "create"], ["pr", "create"]): return f"https://github.com/{repo}/{'issues' if argv[1] == 'issue' else 'pull'}/{num}\n"
    if argv[1:3] == ["issue", "comment"]: return f"https://github.com/{repo}/issues/{argv[3]}#issuecomment-{num}\n"
    if argv[1:2] == ["api"]:
        try: title = (json.loads(stdin) or {}).get("title") if stdin else None
        except (ValueError, AttributeError): title = None
        return json.dumps({"number": num, "id": num, **({"title": title} if title else {})})
    return ""


class ShadowClient:
    """The daemon client in shadow: `events`, `thread` and other reads go to `real`; posts, roots, delegations,
    stop/driver changes and instructs go to the sink with a synthetic success."""

    def __init__(self, real, sink: Sink, owner: str):
        self.real, self.sink, self.owner = real, sink, owner

    def __getattr__(self, name): return getattr(self.real, name)  # reads (`get`, `events`, ...) pass through

    def events(self, after, limit: int = 1000) -> dict: return self.real.events(after, limit)

    def thread(self, thread: str) -> contracts.ThreadView:
        view = self.real.thread(thread)
        shadow_posts = [{"ts": r.get("ts") or f"{int(r.get('at') or 0)}.{i:06d}", "sender": self.owner, "text": r.get("text", "")} for i, r in enumerate(self.sink.posts(thread), 1)]
        return dataclasses.replace(view, messages=list(view.messages) + shadow_posts)

    def _sink(self, kind: str, thread: str, text: str = "", ref: str | None = None, **fields) -> dict:
        ref = ref or contracts.ref_of(text or "")
        key = f"{kind}:{thread}:" + (ref or digest(text, fields))
        job = job_of(contracts.ref_of(text or "")) or job_of(ref)  # a wake's job is on its text's `ref:` line, not its client id
        self.sink.record(kind, key, thread=thread, ref=ref, text=text, job=job, **fields)
        return {"ok": True, "dry_run": True}

    def post_message(self, thread: str, req: contracts.PostRequest) -> dict: return self._sink("post_message", thread, req.text, post_kind=req.kind)
    def post_root(self, channel: str, req: contracts.PostRequest) -> dict: return self._sink("post_root", channel, req.text, post_kind=req.kind)
    def delegate(self, thread: str, body: dict) -> dict:
        self._sink("delegate", thread, ref=str(body.get("action_id") or "") or None, body=body)
        return {"ok": True, "dry_run": True, "join_group": "", "jobs": []}
    def stop_worker(self, thread: str, worker_id: str) -> dict: return self._sink("stop_worker", thread, ref=f"stop:{worker_id}", worker_id=worker_id)
    def set_driver(self, thread: str, driver: str) -> dict: return self._sink("set_driver", thread, ref=f"driver:{driver}", driver=driver)
    def instruct(self, thread: str, text: str, client_id: str, actor: str = "owner") -> dict: return self._sink("instruct", thread, text, ref=client_id, actor=actor)
    def post(self, target: str, body: dict) -> dict: return self._sink("post", target, ref=digest(target, body), body=body)


ET = ZoneInfo("America/New_York")


def gh_kind(argv: list[str], stdin: str | None) -> str:
    """The report kind of a gh write: merge, review request, board write, or plain gh."""
    a, body = " ".join(str(x) for x in argv), stdin or ""
    if a.startswith("gh pr merge") or "mergePullRequest" in a + body or "/merge" in a: return "merge"
    if "requested_reviewers" in a or "requestReviews" in a + body: return "review request"
    if a.startswith(("gh project", "gh api graphql", "gh issue edit", "gh issue comment")) or "/milestones" in a or "/comments" in a: return "board"
    return "gh"


REPORT_KINDS = {"post_message": "post", "post_root": "root", "instruct": "wake", "delegate": "delegate", "stop_worker": "stop", "set_driver": "driver", "post": "post"}


def report_lines(rows: list[dict], thread: str | None = None, waves=None) -> list[str]:
    """`shadow-report`: per thread and job, the would-be actions in time order, one line each: time ET, kind, ref or
    target, and the first 80 characters of the text. Rows with no thread (a board write naming no PR or issue) are under
    "(no thread)"; a gh row without a thread or job gets them from `waves` when its PR or issue is a job's, same repo."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        t, j = r.get("thread") or "", r.get("job") or ""
        if r.get("kind") == "gh" and not t: t, j = attribute(r.get("argv") or [], r.get("stdin"), waves or [])
        t = t or NO_THREAD
        if thread is not None and t != thread: continue
        groups.setdefault((t, j), []).append(r)
    out = []
    for (t, j), rs in sorted(groups.items(), key=lambda kv: (kv[0][0] == NO_THREAD, kv[0])):
        out.append(f"{t}  {j or '(no job)'}")
        for r in sorted(rs, key=lambda r: ts_key(r["ts"]) if r.get("ts") else (int(r.get("at") or 0), 0)):
            when = datetime.fromtimestamp(float(r.get("at") or 0), ET).strftime("%Y-%m-%d %H:%M:%S ET")
            if r.get("kind") == "gh":
                argv = [str(x) for x in r.get("argv") or []]
                kind, target, text = gh_kind(argv, r.get("stdin")), " ".join(argv[1:4]), r.get("stdin") or " ".join(argv)
            else: kind, target, text = REPORT_KINDS.get(r.get("kind"), r.get("kind", "?")), r.get("ref") or r.get("thread") or "", r.get("text") or json.dumps(r.get("body") or {}, sort_keys=True)
            out.append(f"  {when}  {kind}  {target}  {' '.join(str(text).split())[:80]}")
    return out


def read_rows(path) -> list[dict]:
    """The sink's rows, read only (a torn last line is skipped, never cut: the report changes nothing)."""
    lines = [x for x in Path(path).read_text().split("\n")]
    rows = []
    for n, raw in enumerate(lines, 1):
        if not raw.strip(): continue
        try: rows.append(json.loads(raw))
        except json.JSONDecodeError:
            if n != len(lines): raise SinkCorrupt(f"{path}: line {n} is not valid JSON; the shadow record is damaged") from None
    return rows
