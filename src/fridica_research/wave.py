"""#69 part a (R53): the wave and job model. One Slack thread holds N parallel jobs; each job wraps one
`arbiter.Round` (Implement -> CodeAudit -> Merge, #46 slice 2) and the wave routes the thread's events to it.

Like `arbiter.observe`, `observe(wave, event, cfg) -> (wave, actions)` is pure: no I/O, no clock, every
event carries `now`, and the wave is the fold of `observe` over the thread's events. Replaying the same
events from the same opened wave gives the same wave and the same actions.

What this slice fixes:
- A `Wave` holds N `Job`s. A job has its issue, its files, whether it changes `Config`, its implementer and
  auditor (Slack id and GitHub login), its input time and the floor manager's two ETAs (`pr_eta`,
  `review_eta`). The ETAs are recorded, never computed (R49): `open_wave` refuses a job missing either.
- Files are explicit paths. A path ending in `/` covers everything under it. Two jobs overlap only when a
  path is equal to or nested in another's; there is no glob matching. `open_wave` also refuses two
  `config_change` jobs (R53), an empty file list, and one person as both implementer and auditor of a job.
- Each job's round has the thread id `<thread>:jobN`, so every ref the arbiter writes is per job. The
  `thread` of every returned action is rewritten to the wave thread, where the post belongs.
- Routing of a thread post, in order: the `ref: fm-wave-<w>-jobN` line, then a PR or issue link that names
  a job's PR or issue, then the author's role in exactly one job. Anything else becomes a wave finding.
- A `merged` event from GitHub is ground truth: it is accepted whatever the job's round says, with a finding
  when the job was not merge-ready (its round neither in Merge nor approved on the current head).
- When every job is merged the fold emits one `wave_close` action with each job's (first ETA, delivered)
  pairs and their `eta.score`, and puts one close post (`ref: fm-wave-<w>-close`) on `Wave.pending`; the
  driver sends it and drops it only once the daemon took it. Writing the `waves` ledger row is slice 4.

Part b (driver wiring and nits): paths go through `posixpath.normpath` with a trailing `/` kept, and a bare
name covers what is under it; a suffixed ref (`fm-wave-d-job1-pr`) routes to its job; a merged job's round
never moves again; a `merged` event naming another PR than the job's is a finding and otherwise ignored; an
empty wave repo matches no link; two jobs on one issue and an empty path are refused at opening.

#86 and part of #93 (wave F): with `Config.register` set, each person's Slack id and login must be one
registered Innie. The deputy's edits are pure functions on a wave: `drop_job` (a reason, terminal),
`add_job` (the opening checks again), `bind_pr` and `set_eta`. A job's first ETAs stay on `Job.first_etas`
and `close()` scores against them; the wave closes when every job is merged or dropped, and the dropped
jobs are listed apart, never scored. Skipping the polls of a dropped job and the ledger row are driver work.

#89, #90, #92 (wave G): a job's round carries the floor manager's ETAs as `Round.etas` (Implement -> `pr_eta`,
CodeAudit -> `review_eta`) and takes its due times from them; `set_eta` updates them. `merge_ready` also needs
`arbiter.unblocked`. An unrouted post that carries a hand-off marker gets one durable reply asking for the job's
ref line; plain chat stays a finding.
"""
from __future__ import annotations

import copy
import json
import posixpath
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import arbiter, eta, register as register_mod
from .config import Config
from .machine import Action, Event

JOB_ORDER = ("Implement", "CodeAudit", "Merge")  # the short track (R53): the per-job round's `order`
ROLES = ("implementer", "auditor")
ETAS = ("pr_eta", "review_eta")  # the floor manager's two commitments per job (R49), scored at close (R48, R52)
SEEN_KEEP = arbiter.SEEN_KEEP
REF = re.compile(r"^ref:\s*fm-wave-(?P<w>[A-Za-z0-9_.-]+?)-job(?P<n>\d+)(?:-[A-Za-z0-9_.-]*)?\s*$", re.I | re.M)  # a suffix (`-pr`) names the same job
ET = ZoneInfo("America/New_York")
ET_TIME = re.compile(r"^(?:(?P<mo>\d{1,2})-(?P<d>\d{1,2})\s+)?(?P<h>\d{1,2}):(?P<mi>\d{2})\s*ET$", re.I)
FM_CMD = re.compile(r"^\s*fm-cmd:\s*(?P<cmd>.*?)\s*$", re.I | re.M)
GH_LINK = re.compile(r"https://github\.com/(?P<repo>[\w.-]+/[\w.-]+)/(?P<kind>pull|issues)/(?P<n>\d+)")


class WaveError(ValueError):
    """An opening or edit the driver must refuse: a missing ETA, overlapping files, two Config jobs, an empty file list, one person in both roles,
    a person the register does not hold, or an edit on a terminal job or a closed wave."""


def wave_key(thread: str) -> str: return f"wave:{thread}"
def job_thread(thread: str, n: int) -> str: return f"{thread}:job{n}"


def parse_time(value, now: float | None = None) -> float | None:
    """Epoch seconds, ISO 8601 (`2026-10-12T02:20:00Z`, naive is UTC), `MM-DD HH:MM ET` (the year of `now`), or a bare `HH:MM ET`,
    its next occurrence after `now` (#83). None stays None. Times are stored as floats like `Round.due`."""
    if value is None or value == "": return None
    if isinstance(value, (int, float)): return float(value)
    s = str(value).strip()
    try: return float(s)
    except ValueError: pass
    m = ET_TIME.match(s)
    if m:
        if now is None: raise WaveError(f"{s!r} is relative to now; no clock was given")
        base = datetime.fromtimestamp(float(now), ET)
        md = (int(m.group("mo")), int(m.group("d"))) if m.group("mo") else (base.month, base.day)
        try: t = base.replace(month=md[0], day=md[1], hour=int(m.group("h")), minute=int(m.group("mi")), second=0, microsecond=0)
        except ValueError as e: raise WaveError(f"{s!r}: {e}") from e
        if not m.group("mo") and t.timestamp() <= float(now): t = datetime.combine(t.date() + timedelta(days=1), t.time(), ET)
        return t.timestamp()
    try: d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e: raise WaveError(f"{s!r} is not a time: epoch, ISO, `HH:MM ET` or `MM-DD HH:MM ET`") from e
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def norm_path(p: str) -> str:
    """`posixpath.normpath`, keeping a trailing `/`: `./src//x/../a.py` -> `src/a.py`, `tests//` -> `tests/`."""
    p = str(p).strip()
    n = posixpath.normpath(p)
    return n + "/" if p.endswith("/") and not n.endswith("/") else n


def covers(a: str, b: str) -> bool:
    """`a` and `b` name the same path, or one contains the other. A bare name counts as that directory (`tests` covers `tests/x.py`). No globs."""
    a, b = norm_path(a).rstrip("/"), norm_path(b).rstrip("/")
    return a == b or b.startswith(a + "/") or a.startswith(b + "/")


@dataclass
class Job:
    """One job of a wave. `round` is the job's `arbiter.Round` as a dict (`Round.to_dict`), rebuilt on every fold."""
    issue: int
    files: list[str]
    implementer: dict  # {slack, login}
    auditor: dict  # {slack, login}
    input_at: float | None = None
    pr_eta: float | None = None  # the floor manager's ETA for the PR to be open (R49)
    review_eta: float | None = None  # the floor manager's ETA for the auditor's approval (R49)
    config_change: bool = False
    repo: str = ""
    round: dict = field(default_factory=dict)
    delivered: dict = field(default_factory=dict)  # {"pr": now, "review": now}: the first time each ETA's target was observed
    merged_at: float | None = None
    merge_sha: str = ""
    first_etas: dict = field(default_factory=dict)  # {"pr_eta", "review_eta"} as opened (or added); `set_eta` never changes it, `close()` scores it
    dropped_at: float | None = None
    drop_reason: str = ""
    merged_after_drop: float | None = None  # the first `merged` event seen for a dropped job: its finding is written once, then never again

    def rnd(self) -> arbiter.Round: return arbiter.Round.from_dict(self.round)
    def person(self, role: str) -> dict: return dict(getattr(self, role) or {})
    def holds(self, slack: str) -> list[str]: return [role for role in ROLES if self.person(role).get("slack") == slack]

    @property
    def merged(self) -> bool: return self.merged_at is not None

    @property
    def dropped(self) -> bool: return self.dropped_at is not None

    @property
    def terminal(self) -> bool: return self.merged or self.dropped

    def first_eta(self, name: str) -> float | None: return self.first_etas.get(name, getattr(self, name))  # a blob from before #93 has none

    def to_dict(self) -> dict: return {k: getattr(self, k) for k in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        known = {k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__}
        return cls(**{**{"issue": 0, "files": [], "implementer": {}, "auditor": {}}, **known})


@dataclass
class Wave:
    """One wave thread. Its own store namespace (`wave:<thread>`), never in `studies` and never a `round:` key."""
    thread: str
    name: str = ""  # the <w> of `ref: fm-wave-<w>-jobN`
    channel: str = ""
    repo: str = ""
    opened_at: float = 0.0
    jobs: list[Job] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    seen: list[str] = field(default_factory=list)
    closed_at: float | None = None
    pending: list[dict] = field(default_factory=list)  # the close post until the daemon takes it (`Round.pending`'s rule)

    def job(self, n: int) -> Job | None: return self.jobs[n - 1] if 1 <= n <= len(self.jobs) else None
    def number(self, job: Job) -> int: return self.jobs.index(job) + 1

    def to_dict(self) -> dict: return {**{k: getattr(self, k) for k in self.__dataclass_fields__ if k != "jobs"}, "jobs": [j.to_dict() for j in self.jobs]}
    def to_json(self) -> str: return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def from_dict(cls, d: dict) -> "Wave":
        """Tolerant of absent and unknown keys, like `Round.from_dict`."""
        known = {k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__ and k != "jobs"}
        return cls(**{**{"thread": ""}, **known}, jobs=[Job.from_dict(j) for j in (d or {}).get("jobs") or []])

    @classmethod
    def from_json(cls, text: str) -> "Wave": return cls.from_dict(json.loads(text))


# -- opening (driver-only) --------------------------------------------------------------
def parse_job(spec: str) -> dict:
    """`issue=69;files=src/a.py,tests/;implementer=U1:login1;auditor=U2:login2;input_at=…;pr_eta=…;review_eta=…;config_change=no` -> a Job's fields."""
    out: dict = {}
    for part in str(spec).split(";"):
        if not part.strip(): continue
        key, sep, value = part.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or key not in Job.__dataclass_fields__ or key in ("round", "delivered", "merged_at", "merge_sha", "first_etas", "dropped_at", "drop_reason", "merged_after_drop"):
            raise WaveError(f"--job: {part.strip()!r} is not one of issue, files, implementer, auditor, input_at, pr_eta, review_eta, config_change, repo")
        if key == "issue": out[key] = int(value)
        elif key == "files": out[key] = [f.strip() for f in value.split(",") if f.strip()]
        elif key in ROLES:
            slack, _, login = value.partition(":")
            out[key] = {"slack": slack.strip(), "login": login.strip()}
        elif key == "config_change": out[key] = value.lower() in ("1", "true", "yes", "y")
        elif key in ("input_at", *ETAS): out[key] = parse_time(value)
        else: out[key] = value
    return out


def check_job(n: int, j: Job, owner: str):
    if not j.issue or int(j.issue) <= 0: raise WaveError(f"job {n}: needs an issue")
    if not j.files: raise WaveError(f"job {n} (#{j.issue}): the file list is empty; a job declares the explicit paths it touches (R53)")
    if any(not str(f).strip() for f in j.files): raise WaveError(f"job {n} (#{j.issue}): an empty path in the file list; a job declares explicit paths (R53)")
    for role in ROLES:
        p = j.person(role)
        if not arbiter.SLACK_ID.match(str(p.get("slack") or "")): raise WaveError(f"job {n} (#{j.issue}): {role} needs a Slack user id (U… or W…), got {p.get('slack')!r}")
        if not p.get("login"): raise WaveError(f"job {n} (#{j.issue}): {role} needs a GitHub login (slack:login)")
        if p["slack"] == owner: raise WaveError(f"job {n} (#{j.issue}): the owner {owner} cannot hold {role}")
    imp, aud = j.person("implementer"), j.person("auditor")
    if imp["slack"] == aud["slack"] or imp["login"].lower() == aud["login"].lower():
        raise WaveError(f"job {n} (#{j.issue}): {imp['slack']} ({imp['login']}) is both implementer and auditor; a job's auditor is another person (R53)")
    for name in ETAS:
        if getattr(j, name) is None: raise WaveError(f"job {n} (#{j.issue}): no {name}; the floor manager's ETA is recorded, never computed (R49)")


def check_register(n: int, j: Job, reg: register_mod.Register | None):
    """#86 (R48): with a register, each person's Slack id and GitHub login belong to one and the same registered Innie."""
    if reg is None: return
    for role in ROLES:
        p = j.person(role)
        by_slack, by_login = reg.by_slack(p["slack"]), reg.by_login(p["login"])
        if by_slack is None: raise WaveError(f"job {n} (#{j.issue}): {role} {p['slack']} is not in the register ({reg.path or 'no path'})")
        if by_login is None: raise WaveError(f"job {n} (#{j.issue}): {role} login {p['login']!r} is not in the register ({reg.path or 'no path'})")
        if by_slack.name != by_login.name:
            raise WaveError(f"job {n} (#{j.issue}): {role} {p['slack']} is Innie {by_slack.name!r} but login {p['login']!r} is {by_login.name!r}; both must name the same Innie")


def load_register(cfg: Config) -> register_mod.Register | None:
    try: return register_mod.load_for(cfg)  # a register path that is set but missing raises here
    except register_mod.RegisterError as e: raise WaveError(str(e)) from e


def build_job(n: int, spec: dict, repo: str, now: float, cfg: Config, reg: register_mod.Register | None) -> Job:
    spec = {k: v for k, v in spec.items() if not (k == "repo" and not str(v or "").strip())}  # an empty `repo=` is unset: the wave's repo
    j = Job.from_dict({**{"repo": repo or ""}, **spec})
    j.input_at = parse_time(j.input_at) if j.input_at is not None else now
    j.pr_eta, j.review_eta = parse_time(j.pr_eta), parse_time(j.review_eta)
    check_job(n, j, cfg.owner)
    check_register(n, j, reg)
    j.files = [norm_path(f) for f in j.files]
    j.first_etas = {name: getattr(j, name) for name in ETAS}
    return j


def check_disjoint(jobs: list[Job]):
    """R53: one job per issue, files across jobs are disjoint (equal or nested paths overlap) and at most one job changes Config."""
    for i, a in enumerate(jobs):
        same = next((k for k, b in enumerate(jobs[i + 1:], i + 2) if int(b.issue) == int(a.issue)), None)
        if same: raise WaveError(f"jobs {i + 1} and {same} are both on issue #{a.issue}; one job per issue in a wave")
        for b in jobs[i + 1:]:
            hit = next(((p, q) for p in a.files for q in b.files if covers(p, q)), None)
            if hit: raise WaveError(f"jobs {i + 1} (#{a.issue}) and {jobs.index(b) + 1} (#{b.issue}) overlap: {hit[0]!r} and {hit[1]!r} (R53)")
    cfgs = [i + 1 for i, j in enumerate(jobs) if j.config_change]
    if len(cfgs) > 1: raise WaveError(f"jobs {', '.join(map(str, cfgs))} all change Config; jobs that change Config never share a wave (R53)")


def new_round(thread: str, n: int, j: Job, now: float, cfg: Config) -> arbiter.Round:
    """The job's `arbiter.Round` on the short track, keyed `<thread>:jobN`. No `arbiter.adopt`: that is one issue per thread."""
    roster = {role: {"holders": [j.person(role)["slack"]], "login": j.person(role)["login"], "delegate": False} for role in ROLES}
    etas = round_etas(j)
    return arbiter.Round(thread=job_thread(thread, n), channel=thread.split(":")[1], issue=int(j.issue), repo=j.repo, stage=JOB_ORDER[0],
                         stage_started=now, adopted_at=now, due=etas.get(JOB_ORDER[0]), order=list(JOB_ORDER), roster=roster, etas=etas)


def round_etas(j: Job) -> dict[str, float]:
    """The job's ETAs as the round's due times (#89): `pr_eta` for Implement, `review_eta` for CodeAudit."""
    return {stage: float(t) for stage, t in (("Implement", j.pr_eta), ("CodeAudit", j.review_eta)) if t is not None}


def open_wave(thread: str, *, name: str, repo: str, jobs: list[dict], now: float, cfg: Config) -> Wave:
    """The driver opens one wave thread with its N jobs, or refuses. Opening posts nothing and writes no board field."""
    if len(str(thread).split(":")) != 3: raise WaveError(f"{thread!r} is not a thread id (workspace:channel:root_ts)")
    if not jobs: raise WaveError("a wave needs at least one job: --job issue=…;files=…;implementer=…;auditor=…;pr_eta=…;review_eta=…")
    reg = load_register(cfg)
    built = [build_job(n, j, repo, now, cfg, reg) for n, j in enumerate(jobs, 1)]
    check_disjoint(built)
    for n, j in enumerate(built, 1): j.round = new_round(thread, n, j, now, cfg).to_dict()
    return Wave(thread=thread, name=str(name or ""), channel=str(thread).split(":")[1], repo=repo or "", opened_at=now, jobs=built)


# -- the deputy's edits (#93; pure, the CLI saves them) ---------------------------------
def editable(w: Wave, n: int) -> Job:
    if w.closed_at is not None: raise WaveError(f"wave {w.name or '-'} is closed")
    j = w.job(int(n))
    if j is None: raise WaveError(f"no job {n} in this wave ({len(w.jobs)} jobs)")
    if j.terminal: raise WaveError(f"job {n} (#{j.issue}) is {'merged' if j.merged else 'dropped'}; a terminal job does not change")
    return j


def add_job(wave: Wave, spec: dict, *, now: float, cfg: Config) -> Wave:
    """One more job, under the opening's checks: ETAs, people, register, one job per issue, disjoint files, one Config job."""
    w = copy.deepcopy(wave)
    if w.closed_at is not None: raise WaveError(f"wave {w.name or '-'} is closed; open a new wave")
    n = len(w.jobs) + 1
    j = build_job(n, spec, w.repo, now, cfg, load_register(cfg))
    check_disjoint(w.jobs + [j])
    j.round = new_round(w.thread, n, j, now, cfg).to_dict()
    w.jobs.append(j)
    return w


def bind_pr(wave: Wave, n: int, pr: int, *, head: str = "", now: float) -> Wave:
    """Record a job's PR by hand (one the poll did not adopt): links to it route to the job, and the PR counts as delivered now."""
    if int(pr or 0) <= 0: raise WaveError(f"PR number must be positive, got {pr!r}")
    w = copy.deepcopy(wave)
    j = editable(w, n)
    j.round["pr"] = {**(j.round.get("pr") or {}), "number": int(pr), **({"head": head} if head else {})}
    j.delivered.setdefault("pr", now)
    return w


def set_eta(wave: Wave, n: int, *, pr_eta=None, review_eta=None, now: float | None = None) -> Wave:
    """The floor manager's re-estimate: the current ETA changes, `first_etas` stays and is what `close()` scores (R49)."""
    if pr_eta is None and review_eta is None: raise WaveError("give a new pr_eta, review_eta or both")
    w = copy.deepcopy(wave)
    j = editable(w, n)
    j.first_etas = {name: j.first_eta(name) for name in ETAS}
    if pr_eta is not None: j.pr_eta = parse_time(pr_eta, now)
    if review_eta is not None: j.review_eta = parse_time(review_eta, now)
    retime(j)
    return w


def retime(j: Job):
    j.round["etas"] = round_etas(j)  # the round's due follows the re-estimate (#89)
    if j.round.get("stage") in j.round["etas"]: j.round["due"] = j.round["etas"][j.round["stage"]]


ROLE_ETA = {"implementer": "pr_eta", "auditor": "review_eta"}


def reassign(wave: Wave, n: int, role: str, person: dict, new_eta, *, now: float, cfg: Config) -> Wave:
    """#83: the role moves to `person` ({slack, login}) and `new_eta` becomes the role's re-estimate; `first_etas` is kept for scoring (R52).
    Refuses the owner, the person who already holds the role and the job's other role. The ladder's ids carry the holder and ETA, so its rungs
    start again for the new holder; the old holder is released by no longer being on the roster."""
    if role not in ROLES: raise WaveError(f"role must be one of {', '.join(ROLES)}, got {role!r}")
    w = copy.deepcopy(wave)
    j = editable(w, n)
    p = {"slack": str((person or {}).get("slack") or "").strip(), "login": str((person or {}).get("login") or "").strip()}
    if not arbiter.SLACK_ID.match(p["slack"]): raise WaveError(f"job {n}: {role} needs a Slack user id (U… or W…), got {p['slack']!r}")
    if not p["login"]: raise WaveError(f"job {n}: {role} needs a GitHub login (slack:login)")
    if p["slack"] == cfg.owner: raise WaveError(f"job {n} (#{j.issue}): the owner {cfg.owner} cannot hold {role}")
    cur, other = j.person(role), j.person(ROLES[1 - ROLES.index(role)])
    if p["slack"] == cur.get("slack") or p["login"].lower() == str(cur.get("login") or "").lower():
        raise WaveError(f"job {n} (#{j.issue}): {p['slack']} ({p['login']}) already holds {role}")
    if p["slack"] == other.get("slack") or p["login"].lower() == str(other.get("login") or "").lower():
        raise WaveError(f"job {n} (#{j.issue}): {p['slack']} ({p['login']}) holds the other role; a job's auditor is another person (R53)")
    t = parse_time(new_eta, now)
    if t is None: raise WaveError(f"job {n}: reassign needs a new ETA (R49)")
    check_register(n, Job.from_dict({**j.to_dict(), role: p}), load_register(cfg))
    j.first_etas = {name: j.first_eta(name) for name in ETAS}
    setattr(j, role, p)
    setattr(j, ROLE_ETA[role], t)
    j.round["roster"] = {**(j.round.get("roster") or {}), role: {**((j.round.get("roster") or {}).get(role) or {}), "holders": [p["slack"]], "login": p["login"]}}
    retime(j)
    return w


# -- fm-cmd lines (#93): the owner's typed commands, one per line ------------------------
def job_number(token: str) -> int:
    m = re.fullmatch(r"(?:job)?(\d+)", str(token).strip(), re.I)
    if not m: raise WaveError(f"{token!r} is not a job (jobN)")
    return int(m.group(1))


def command(w: Wave, line: str, now: float, cfg: Config) -> tuple[Wave, list[Action]]:
    """`reassign jobN implementer|auditor Uslack:login <eta>`, `eta jobN pr|review <eta>`, `drop jobN <reason>`, `bind-pr jobN <pr> [head]`."""
    words = line.split()
    if not words: raise WaveError("empty command")
    verb, args = words[0].lower(), words[1:]
    if verb == "reassign":
        if len(args) < 4: raise WaveError("usage: reassign jobN implementer|auditor Uslack:login <eta>")
        slack, _, login = args[2].partition(":")
        return reassign(w, job_number(args[0]), args[1].lower(), {"slack": slack, "login": login}, " ".join(args[3:]), now=now, cfg=cfg), []
    if verb == "eta":
        if len(args) < 3 or args[1].lower() not in ("pr", "review"): raise WaveError("usage: eta jobN pr|review <eta>")
        return set_eta(w, job_number(args[0]), **{f"{args[1].lower()}_eta": " ".join(args[2:])}, now=now), []
    if verb == "drop":
        if len(args) < 2: raise WaveError("usage: drop jobN <reason>")
        return drop_job(w, job_number(args[0]), " ".join(args[1:]), now=now)
    if verb == "bind-pr":
        if len(args) not in (2, 3) or not args[1].lstrip("#").isdigit(): raise WaveError("usage: bind-pr jobN <pr> [head]")
        return bind_pr(w, job_number(args[0]), int(args[1].lstrip("#")), head=args[2] if len(args) == 3 else "", now=now), []
    raise WaveError(f"unknown command {verb!r}; one of reassign, eta, drop, bind-pr")


def commands(w: Wave, ev: Event, ts: str, cfg: Config) -> tuple[Wave, list[Action]]:
    """Each `fm-cmd:` line applied on its own; a failed line leaves the wave as it was. At most one reply per post, listing the failures."""
    out: list[Action] = []
    failed: list[str] = []
    for m in FM_CMD.finditer(str(ev.get("text") or "")):
        try: w, acts = command(w, m.group("cmd"), ev.now, cfg)
        except WaveError as e:
            failed.append(f"`fm-cmd: {m.group('cmd')}`: {e}")
            continue
        out += acts
    if failed:
        ref = f"{w.thread}/wave/fm-cmd/{ts or int(ev.now)}"
        if not any(p.get("id") == ref for p in w.pending):
            a = Action("post", ref, {"thread": w.thread, "post_kind": "report", "text": "\n".join([
                f"{len(failed)} fm-cmd line{'s' if len(failed) > 1 else ''} not applied:", *(f"• {f}" for f in failed), f"ref: {ref}"])})
            w.pending = w.pending + [a.to_dict()]
            out.append(a)
    return w, out


def drop_job(wave: Wave, n: int, reason: str, *, now: float) -> tuple[Wave, list[Action]]:
    """Drop a job with a reason. Terminal: its round never moves again. The last open job going terminal closes the wave."""
    if not str(reason or "").strip(): raise WaveError("drop needs a reason")
    w = copy.deepcopy(wave)
    j = editable(w, n)
    j.dropped_at, j.drop_reason = now, str(reason).strip()
    return w, close(w, now) if all(x.terminal for x in w.jobs) else []


# -- routing ----------------------------------------------------------------------------
def route(w: Wave, ev: Event) -> tuple[Job | None, str]:
    """The job an event belongs to, or `(None, why)`. Posts: the ref line, then a PR or issue link, then the author's role in exactly one job."""
    n = ev.get("job")
    if n is not None:  # a per-job event the driver feeds (pr_polled, merged with a job number)
        j = w.job(int(n))
        return (j, "") if j else (None, f"no job {n} in this wave")
    if ev.kind == "merged":
        hits = [j for j in w.jobs if ev.get("pr") and int(ev.get("pr")) == int((j.round.get("pr") or {}).get("number") or 0)]
        return (hits[0], "") if len(hits) == 1 else (None, f"merged PR #{ev.get('pr')} is not a job's recorded PR")
    text = str(ev.get("text") or "")
    m = REF.search(text)
    if m:
        if w.name and m.group("w").lower() != w.name.lower(): return None, f"ref names wave {m.group('w')!r}, this is wave {w.name!r}"
        j = w.job(int(m.group("n")))
        return (j, "") if j else (None, f"ref names job {m.group('n')}, the wave has {len(w.jobs)}")
    hits: list[Job] = []
    for lm in GH_LINK.finditer(text):
        for j in w.jobs:
            repo = j.repo or w.repo
            if not repo or lm.group("repo").lower() != repo.lower(): continue  # an empty repo matches no link
            n_ = int(lm.group("n"))
            if (lm.group("kind") == "pull" and n_ == int((j.round.get("pr") or {}).get("number") or 0)) or (lm.group("kind") == "issues" and n_ == int(j.issue)):
                if j not in hits: hits.append(j)
    if len(hits) == 1: return hits[0], ""
    if len(hits) > 1: return None, "links name jobs " + ", ".join(str(w.number(j)) for j in hits)
    sender = str(ev.get("sender") or "")
    held = [j for j in w.jobs if j.holds(sender)]
    if len(held) == 1: return held[0], ""
    return None, (f"{sender} holds a role in jobs " + ", ".join(str(w.number(j)) for j in held)) if held else f"{sender} holds no role and the post names no job"


def unrouted(w: Wave, ev: Event, ts: str) -> list[Action]:
    """#92: an unrouted post that carries a hand-off marker gets one durable reply asking for the job's ref line; plain chat stays a finding."""
    text, sender = str(ev.get("text") or ""), str(ev.get("sender") or "")
    if ev.kind != "crew_post" or not ts or not arbiter.HANDOFF_MARK.search(text): return []
    ref = f"{w.thread}/wave/unrouted/{ts}"
    if any(p.get("id") == ref for p in w.pending): return []
    jobs = ", ".join(f"`ref: fm-wave-{w.name or '<w>'}-job{n}` (#{j.issue})" for n, j in enumerate(w.jobs, 1) if not j.terminal)
    a = Action("post", ref, {"thread": w.thread, "post_kind": "report", "text": "\n".join([
        f"<@{sender}> this hand-off names no single job, so it did not clear anything. Post it again with the job's line: {jobs}.", f"ref: {ref}"])})
    w.pending = w.pending + [a.to_dict()]
    return [a]


def merge_ready(j: Job) -> bool:
    """In Merge or approved on the current head, and `unblocked` (#90): not conflicting, checks green or none, not draft."""
    r = j.rnd()
    return (r.stage == "Merge" or arbiter.approved(r)) and arbiter.unblocked(r)


def rewrite(w: Wave, a: Action) -> Action:
    """The job's round posts under `<thread>:jobN`; the post belongs in the wave thread. Ids and refs keep the job suffix, so they never collide."""
    return Action(a.kind, a.id, {**a.data, "thread": w.thread}) if "thread" in a.data else a


# -- the fold ---------------------------------------------------------------------------
def observe(wave: Wave, ev: Event, cfg: Config) -> tuple[Wave, list[Action]]:
    """One thread event -> the next wave and the actions the driver executes. Pure; `ev.now` is the only clock."""
    w = copy.deepcopy(wave)
    if ev.kind not in ("crew_post", "pr_polled", "merged"): return w, []  # a time event never advances or closes anything (L3.1)
    ts = str(ev.get("ts") or "")
    if ev.kind == "crew_post":
        if ts and ts in w.seen: return w, []
        if ts: w.seen = (w.seen + [ts])[-SEEN_KEEP:]
        if str(ev.get("sender") or "") == cfg.owner and FM_CMD.search(str(ev.get("text") or "")): return commands(w, ev, ts, cfg)  # #93: owner only
        if str(ev.get("sender") or "") == cfg.owner and not REF.search(str(ev.get("text") or "")): return w, []  # the arbiter's own posts, and the floor manager's prose
    j, why = route(w, ev)
    if j is None:
        w.findings.append(f"{ev.kind} {ts or ''}".rstrip() + f" not routed: {why}")
        return w, unrouted(w, ev, ts)
    n = w.number(j)
    if ev.kind == "merged":
        recorded = int((j.round.get("pr") or {}).get("number") or 0)
        if ev.get("pr") and recorded and int(ev["pr"]) != recorded:
            w.findings.append(f"job {n} (#{j.issue}): merged event for PR #{ev['pr']}, but the job's PR is #{recorded}; ignored")
            return w, []
        if j.merged: return w, []
        if j.dropped:  # the driver polls a dropped job until Wave G: one finding per job, before and after the close
            if j.merged_after_drop is None:
                j.merged_after_drop = ev.now
                w.findings.append(f"job {n} (#{j.issue}): merged event for PR #{ev.get('pr') or '-'} after the job was dropped; ignored")
            return w, []
        if not merge_ready(j): w.findings.append(f"job {n} (#{j.issue}): merged on GitHub before it was merge-ready (round at {j.rnd().stage}); the merge is ground truth")
        j.merged_at, j.merge_sha = ev.now, str(ev.get("sha") or "")
        j.round["pr"] = {**(j.round.get("pr") or {}), "state": "MERGED", **({"number": int(ev["pr"])} if ev.get("pr") else {})}
        for k in ("pr", "review"): j.delivered.setdefault(k, ev.now)  # a merged PR was delivered and reviewed by then, at the latest
        return w, close(w, ev.now) if all(x.terminal for x in w.jobs) else []
    if j.terminal: return w, []  # a merged or dropped job's round is final: no later post or poll moves it
    r, actions = arbiter.observe(j.rnd(), ev, cfg)
    if r.pr.get("number") and "pr" not in j.delivered: j.delivered["pr"] = ev.now
    if arbiter.approved(r) and "review" not in j.delivered: j.delivered["review"] = ev.now
    r.pending = [{**p, "thread": w.thread} if "thread" in p else p for p in r.pending]
    j.round = r.to_dict()
    return w, [rewrite(w, a) for a in actions]


def close(w: Wave, now: float) -> list[Action]:
    """Every job merged or dropped: one `wave_close` with the merged jobs' (first ETA, delivered) pairs and their `eta.score` (R48, R52),
    and the dropped jobs apart, never scored. Slice 4 writes and posts it."""
    w.closed_at = now
    pairs, scored = [], []
    for n, j in enumerate(w.jobs, 1):
        if j.dropped: continue
        for kind, name in (("pr", "pr_eta"), ("review", "review_eta")):
            first, delivered = j.first_eta(name), j.delivered.get(kind)
            pairs.append({"job": n, "issue": j.issue, "kind": kind, "first_eta": first, "delivered": delivered})
            if first is not None and delivered is not None: scored.append(eta.Scored(f"job{n}/{kind}", _dt(first), _dt(delivered)))
    ref = close_ref(w)
    w.pending = w.pending + [Action("post", ref, {"thread": w.thread, "post_kind": "report", "text": close_text(w, pairs, ref)}).to_dict()]
    merged = [{"job": n, "issue": j.issue, "pr": (j.round.get("pr") or {}).get("number"), "sha": j.merge_sha, "merged_at": j.merged_at} for n, j in enumerate(w.jobs, 1) if j.merged]
    dropped = [{"job": n, "issue": j.issue, "reason": j.drop_reason, "dropped_at": j.dropped_at} for n, j in enumerate(w.jobs, 1) if j.dropped]
    return [Action("wave_close", f"{w.thread}/wave/close", {"thread": w.thread, "wave": w.name, "jobs": merged, "dropped": dropped, "etas": pairs, "score": asdict(eta.score(scored)), "findings": list(w.findings)})]


def close_ref(w: Wave) -> str: return f"fm-wave-{w.name or 'wave'}-close"


def close_text(w: Wave, pairs: list[dict], ref: str) -> str:
    """The one close post: each job's PR and merge, and its ETAs against delivery. The `waves` row is slice 4."""
    n_merged, n_dropped = sum(j.merged for j in w.jobs), sum(j.dropped for j in w.jobs)
    lines = [f"wave {w.name or '-'} closed: {n_merged}/{len(w.jobs)} jobs merged" + (f", {n_dropped} dropped." if n_dropped else ".")]
    for n, j in enumerate(w.jobs, 1):
        if j.dropped: continue
        got = {p["kind"]: p for p in pairs if p["job"] == n}
        lines.append(f"job {n} #{j.issue}: PR #{(j.round.get('pr') or {}).get('number') or '-'} merged {str(j.merge_sha)[:7] or '-'}; "
                     f"PR {arbiter.eta_line(got['pr']['first_eta'])}, delivered {_at(got['pr']['delivered'])}; review {arbiter.eta_line(got['review']['first_eta'])}, delivered {_at(got['review']['delivered'])}")
    lines += [f"job {n} #{j.issue} dropped: {j.drop_reason}" for n, j in enumerate(w.jobs, 1) if j.dropped]
    if w.findings: lines.append(f"{len(w.findings)} wave findings.")
    return "\n".join(lines + [f"ref: {ref}"])


def _at(ts: float | None) -> str: return arbiter.et_time(ts) if ts is not None else "-"


def _dt(ts: float) -> datetime: return datetime.fromtimestamp(ts, timezone.utc)


def status_lines(w: Wave) -> list[str]:
    """`wave status`: one line per job, then the wave, then every finding's text (the wave's, then each job round's)."""
    out = []
    for n, j in enumerate(w.jobs, 1):
        r = j.rnd()
        pr = r.pr.get("number")
        out.append(f"job {n}\t#{j.issue}\t{'merged' if j.merged else ('dropped: ' + j.drop_reason) if j.dropped else r.stage}\tPR {('#%d@%s' % (pr, str(r.pr.get('head', ''))[:7])) if pr else '-'}\t"
                   f"{arbiter.eta_line(j.pr_eta).replace('ETA', 'PR ETA')}\t{arbiter.eta_line(j.review_eta).replace('ETA', 'review ETA')}\t"
                   f"{j.person('implementer').get('login', '?')} -> {j.person('auditor').get('login', '?')}\t{len(j.files)} paths{' Config' if j.config_change else ''}\t{len(r.findings)} findings")
    out.append(f"wave {w.name or '-'}\t{w.thread}\t{'closed ' + arbiter.et_time(w.closed_at) if w.closed_at else 'open'}\t{sum(j.merged for j in w.jobs)}/{len(w.jobs)} merged\t{len(w.findings)} findings")
    out += [f"finding (wave): {f}" for f in w.findings]
    out += [f"finding (job {n}): {f}" for n, j in enumerate(w.jobs, 1) for f in j.rnd().findings]
    return out
