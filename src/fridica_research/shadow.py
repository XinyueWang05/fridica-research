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
import json
import re
import time
from pathlib import Path

from . import contracts

GH_READS = {"pr": {"view", "list", "checks", "diff", "status"}, "issue": {"view", "list"}, "project": {"view", "list", "field-list", "item-list"}}
API_BODY_FLAGS = {"-f", "-F", "--field", "--raw-field", "--input"}
API_READ_FLAGS = {"--paginate", "--slurp", "--include", "-i", "--silent", "--verbose", "--jq", "-q", "--template", "-t", "--cache", "--header", "-H", "--hostname"}
API_VALUE_FLAGS = {"-X", "--method", "--jq", "-q", "--template", "-t", "--cache", "--header", "-H", "--hostname", *API_BODY_FLAGS}


def api_flags(args: list[str]) -> list[tuple[str, str | None]]:
    """`gh api` flags as (name, value): `--name=v`, `-Xv` glued, or the value in the next token."""
    out, i = [], 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            if not eq and name in API_VALUE_FLAGS and i + 1 < len(args): val, i = args[i + 1], i + 1
            out.append((name, val if (eq or name in API_VALUE_FLAGS) else None))
        elif a.startswith("-") and len(a) > 1:
            name, val = a[:2], a[2:] or None
            if val is None and name in API_VALUE_FLAGS and i + 1 < len(args): val, i = args[i + 1], i + 1
            out.append((name, val))
        i += 1
    return out


def is_read(argv: list[str], stdin: str | None = None) -> bool:
    """True only for the listed gh reads; anything else (and anything that is not gh) is a write. It fails closed: a
    `gh api` call is a read only as a GET (`-X`/`--method`, glued or not) whose every flag is a known read flag. A graphql
    call is judged by its query, in argv (`-f query=...`) or on stdin (`--input -`, as `board.Projects.graphql` sends it)."""
    a = [str(x) for x in argv or []]
    if len(a) < 3 or a[0] != "gh": return False
    if a[1] in GH_READS: return a[2] in GH_READS[a[1]]
    if a[1] != "api": return False
    flags = api_flags(a[2:])
    if any((v or "").upper() != "GET" for n, v in flags if n in ("-X", "--method")): return False
    if "graphql" in a[2:]:
        return not any("mutation" in x.lower() for x in [*a, stdin or ""])  # allowed unless it mutates
    return all(n in API_READ_FLAGS or n in ("-X", "--method") for n, _ in flags)


def digest(*parts) -> str: return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


class Sink:
    """One JSON line per would-be write at `path`; each key is written once, across restarts."""

    def __init__(self, path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.rows: list[dict] = []
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                try: row = json.loads(line) if line.strip() else None
                except json.JSONDecodeError: row = None  # a line half-written when the process died: skipped, its write is redone
                if isinstance(row, dict) and "key" in row: self.rows.append(row)
        self.keys = {r["key"] for r in self.rows}

    def record(self, kind: str, key: str, **fields) -> bool:
        """Append the action unless its key is already in the sink; True when it was new."""
        if key in self.keys: return False
        at = self.clock()
        row = {"at": at, "ts": f"{int(at)}.{len(self.rows) + 1:06d}", "kind": kind, "key": key, **fields}  # a Slack-style ts, unique per row
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            if f.tell() and not self.path.read_bytes().endswith(b"\n"): f.write("\n")  # end a torn line before appending
            f.write(json.dumps(row, sort_keys=True) + "\n")
        self.rows.append(row)
        self.keys.add(key)
        return True

    def posts(self, thread: str) -> list[dict]: return [r for r in self.rows if r["kind"] == "post_message" and r.get("thread") == thread]


def runner(real, sink: Sink):
    """A gh runner: reads go to `real`, every other call to the sink (keyed by a hash of argv and stdin) and returns `{}`."""
    def run(argv, stdin):
        if is_read(argv, stdin): return real(argv, stdin)
        key = "gh:" + digest(list(argv), stdin)
        sink.record("gh", key, argv=list(argv), stdin=stdin)
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
        self.sink.record(kind, key, thread=thread, ref=ref, text=text, **fields)
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
