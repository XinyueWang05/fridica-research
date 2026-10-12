"""R48 (#54, absorbing #42): the register of Innies and their Outies, read from `[innie.<name>]` TOML tables.

One table per Innie. Required: `outie` (the escalation target), `slack` (the Innie's Slack user id)
and `github` (its GitHub login). Optional: `host`, `hardware`, `client`, `plan`, `slots` (concurrent
jobs, an integer >= 1), `hours` and `offline` (a window as `HH:MM-HH:MM`), and `limits` (a string or a
list of strings: fork-only pushes, missing token scopes, no review rights, ...). An unknown field, a
duplicate Innie name, a duplicate Slack id or a duplicate GitHub login is a `RegisterError`.

The register is the single source for the arbiter's roster (`arbiter.parse_roster` refuses a Slack id
or a `--login` it does not list, when `Config.register` is set), for the escalation path of slice 3
(Innie -> floor manager -> that Innie's Outie) and for the estimator's buffers. Pure: `parse` reads
text, `load` reads one file and a path that is set but missing is a hard error.

    [innie.jiheng]
    outie = "Jiheng Hu"
    slack = "U08P0CHTQPK"
    github = "jihenghu"
    host = "local"
    hardware = "2x RTX 1080 Ti"
    client = "claude"
    plan = "max"
    slots = 2
    hours = "09:00-23:00"
    offline = "23:00-09:00"
    limits = ["fork-only pushes"]
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REQUIRED = ("outie", "slack", "github")
OPTIONAL = ("host", "hardware", "client", "plan", "slots", "hours", "offline", "limits")
FIELDS = REQUIRED + OPTIONAL
WINDOW = re.compile(r"^(?P<a>[0-2]\d):(?P<b>[0-5]\d)-(?P<c>[0-2]\d):(?P<d>[0-5]\d)$")
SLACK_ID = re.compile(r"^[UW][A-Z0-9]{2,}$")


class RegisterError(ValueError):
    """A register the driver must refuse: a missing or unknown field, a bad value, a duplicate Innie, Slack id or login, or a missing file."""


def window(value: str, what: str) -> tuple[int, int]:
    """`HH:MM-HH:MM` -> (start, end) in minutes of the day; an end before the start wraps past midnight (`23:00-09:00`)."""
    m = WINDOW.match(str(value).strip())
    if not m: raise RegisterError(f"{what}: expected HH:MM-HH:MM, got {value!r}")
    a, b, c, d = (int(m.group(k)) for k in "abcd")
    if a > 23 or c > 23: raise RegisterError(f"{what}: hour out of range in {value!r}")
    return a * 60 + b, c * 60 + d


@dataclass(frozen=True)
class Innie:
    name: str
    outie: str
    slack: str
    github: str
    host: str = ""
    hardware: str = ""
    client: str = ""
    plan: str = ""
    slots: int = 1
    hours: str = ""  # working hours, `HH:MM-HH:MM`, empty when unknown
    offline: str = ""  # offline window, `HH:MM-HH:MM`, empty when none
    limits: tuple[str, ...] = ()

    def in_window(self, which: str, minute_of_day: int) -> bool | None:
        """Whether a minute of the day (0-1439) falls in `hours` or `offline`; None when that window is not set."""
        text = getattr(self, which)
        if not text: return None
        start, end = window(text, which)
        return start <= minute_of_day < end if start <= end else minute_of_day >= start or minute_of_day < end


@dataclass(frozen=True)
class Register:
    innies: dict[str, Innie] = field(default_factory=dict)  # name -> Innie, in file order
    path: str = ""

    def __len__(self) -> int: return len(self.innies)
    def __iter__(self): return iter(self.innies.values())
    def get(self, name: str) -> Innie | None: return self.innies.get(name)
    def by_slack(self, slack: str) -> Innie | None: return next((i for i in self if i.slack == slack), None)
    def by_login(self, login: str) -> Innie | None: return next((i for i in self if i.github.lower() == str(login).lower()), None)
    def outie_of(self, slack: str) -> str:
        """The escalation target for an Innie's Slack id (#42): its Outie, or "" when the Innie is not registered."""
        i = self.by_slack(slack)
        return i.outie if i else ""


def _innie(name: str, raw) -> Innie:
    if not isinstance(raw, dict): raise RegisterError(f"[innie.{name}] must be a table")
    if unknown := sorted(set(raw) - set(FIELDS)): raise RegisterError(f"[innie.{name}]: unknown field(s) {', '.join(unknown)}; fields are {', '.join(FIELDS)}")
    if missing := [k for k in REQUIRED if not str(raw.get(k, "")).strip()]: raise RegisterError(f"[innie.{name}]: missing {', '.join(missing)}; outie, slack and github are required")
    for k in ("outie", "slack", "github", "host", "hardware", "client", "plan", "hours", "offline"):
        if k in raw and not isinstance(raw[k], str): raise RegisterError(f"[innie.{name}].{k} must be a string")
    slack = raw["slack"].strip()
    if not SLACK_ID.match(slack): raise RegisterError(f"[innie.{name}].slack: {slack!r} is not a Slack user id (U… or W…)")
    slots = raw.get("slots", 1)
    if isinstance(slots, bool) or not isinstance(slots, int) or slots < 1: raise RegisterError(f"[innie.{name}].slots must be an integer >= 1, got {slots!r}")
    for k in ("hours", "offline"):
        if raw.get(k): window(raw[k], f"[innie.{name}].{k}")
    limits = raw.get("limits", ())
    if isinstance(limits, str): limits = (limits,) if limits.strip() else ()
    elif isinstance(limits, list) and all(isinstance(x, str) for x in limits): limits = tuple(limits)
    else: raise RegisterError(f"[innie.{name}].limits must be a string or a list of strings")
    return Innie(name=name, outie=raw["outie"].strip(), slack=slack, github=raw["github"].strip(), host=raw.get("host", "").strip(), hardware=raw.get("hardware", "").strip(),
                 client=raw.get("client", "").strip(), plan=raw.get("plan", "").strip(), slots=slots, hours=raw.get("hours", "").strip(), offline=raw.get("offline", "").strip(), limits=limits)


def parse(text: str, path: str = "") -> Register:
    """The register from TOML text. Only `[innie.<name>]` tables are read; any other top-level key is unknown."""
    try: raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e: raise RegisterError(f"register{': ' + path if path else ''}: {e}") from e  # a duplicate `[innie.<name>]` lands here
    if unknown := sorted(set(raw) - {"innie"}): raise RegisterError(f"register: unknown top-level key(s) {', '.join(unknown)}; the register holds [innie.<name>] tables only")
    tables = raw.get("innie", {})
    if not isinstance(tables, dict): raise RegisterError("register: `innie` must hold [innie.<name>] tables")
    innies: dict[str, Innie] = {}
    for name, entry in tables.items():
        i = _innie(str(name), entry)
        if dup := next((o for o in innies.values() if o.slack == i.slack), None): raise RegisterError(f"[innie.{name}]: Slack id {i.slack} is already [innie.{dup.name}]'s")
        if dup := next((o for o in innies.values() if o.github.lower() == i.github.lower()), None): raise RegisterError(f"[innie.{name}]: GitHub login {i.github} is already [innie.{dup.name}]'s")
        innies[i.name] = i
    return Register(innies=innies, path=path)


def load(path: str | Path) -> Register:
    """The register at `path`. A path that is set but missing is a hard error: a roster is never checked against an empty register by accident."""
    p = Path(os.path.expanduser(str(path)))
    if not p.is_file(): raise RegisterError(f"register {p} does not exist")
    return parse(p.read_text(), str(p))


def load_for(cfg) -> Register | None:
    """`Config.register` as a Register, or None when no register is configured."""
    return load(cfg.register) if getattr(cfg, "register", "") else None
