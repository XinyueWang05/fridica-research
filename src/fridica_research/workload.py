"""R50 workload intake (#84): parse an Innie's workload report into a `workload` ledger row.

The usage line an Innie posts (R50 amendment, #54) is

    client | session: X% (resets HH:MM ET) | weekly: Y% (resets [Day] HH:MM ET)

and a report may carry a slots line, `slots: F/T` (free over total). A value the Innie cannot
read is posted as `unreadable` and stored as None (NULL), never estimated. A reset names a wall
time in ET (America/New_York, so EDT or EST as the date has it); it resolves to its next
occurrence after `reported_at` (on the named weekday when one is given) and is stored as UTC.
No LLM: a line that names a field but does not parse raises `WorkloadError`.

`intake` reads a thread's posts into rows: the Innie is the register's entry for the post's
Slack sender, `reported_at` is the post's own `ts`, and `source` is `<thread>/<ts>` (a post's own
`thread` overrides the argument). A post that carries no report line is chat and is passed
over; a report that cannot be read becomes a finding, a dict `{source, sender, reason}`.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UNREADABLE = "unreadable"
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class WorkloadError(ValueError):
    """A report line that names a field but cannot be read: a finding, not a row."""


@dataclass(frozen=True)
class Report:
    """One parsed report. Times are ISO UTC text (`...Z`), the ledger's form; None is unknown."""
    reported_at: str
    client: str | None = None
    session_usage: float | None = None
    session_reset: str | None = None
    weekly_usage: float | None = None
    weekly_reset: str | None = None
    free_slots: int | None = None
    total_slots: int | None = None

    def row(self, innie: str) -> dict:
        """The `workload` columns for `ledger.ingest`."""
        return {"innie": innie, **asdict(self)}


def utc(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_reset(text: str, reported_at: datetime) -> datetime:
    """`HH:MM ET` or `Day HH:MM ET` as the next such ET wall time strictly after `reported_at`, in UTC."""
    m = re.fullmatch(r"(?:(\w{3})\w*\s+)?(\d{1,2}):(\d{2})\s*ET", text.strip(), re.I)
    if not m or int(m[2]) > 23 or int(m[3]) > 59: raise WorkloadError(f"unreadable reset time {text!r}")
    day = m[1].lower() if m[1] else None
    if day is not None and day not in DAYS: raise WorkloadError(f"unknown weekday in reset {text!r}")
    local = reported_at.astimezone(ET)
    for ahead in range(8):  # the next week covers every weekday
        d = (local + timedelta(days=ahead)).date()
        cand = datetime(d.year, d.month, d.day, int(m[2]), int(m[3]), tzinfo=ET)
        if cand > reported_at and (day is None or DAYS[d.weekday()] == day): return cand.astimezone(timezone.utc)
    raise WorkloadError(f"no next occurrence for reset {text!r}")  # unreachable for a valid time


def _usage(field: str, segment: str, reported_at: datetime) -> tuple[float | None, str | None]:
    """`session: 40% (resets 13:50 ET)` -> (40.0, reset as UTC). `unreadable` in either place is None."""
    m = re.fullmatch(rf"{field}:\s*(?P<pct>{UNREADABLE}|\d+(?:\.\d+)?\s*%)\s*(?:\(\s*resets\s+(?P<reset>[^)]*)\))?", segment.strip(), re.I)
    if not m: raise WorkloadError(f"unreadable {field} segment {segment.strip()!r}")
    pct = None if m["pct"].lower() == UNREADABLE else float(m["pct"].rstrip("% "))
    if pct is not None and not 0 <= pct <= 100: raise WorkloadError(f"{field} usage {pct}% is outside 0-100")
    reset = m["reset"]
    if reset is None or reset.strip().lower() in (UNREADABLE, ""): return pct, None
    return pct, utc(resolve_reset(reset, reported_at))


def parse(text: str, reported_at: datetime) -> Report:
    """Parse a report's text. `reported_at` (aware) is when it was posted: resets resolve after it.

    Lines that name neither `session:`/`weekly:` nor `slots:` are passed over (a report is free
    text around its fields); a line that names one and does not parse raises `WorkloadError`."""
    if reported_at.tzinfo is None: raise ValueError("reported_at must be timezone-aware")
    fields: dict = {"reported_at": utc(reported_at)}
    for line in text.splitlines():
        line = line.strip().strip("`").strip()
        if m := re.match(r"slots:\s*(.*)$", line, re.I):
            s = re.fullmatch(rf"(\d+|{UNREADABLE})\s*/\s*(\d+|{UNREADABLE})", m[1].strip(), re.I)
            if not s: raise WorkloadError(f"unreadable slots {m[1].strip()!r}: want free/total")
            free, total = (None if v.lower() == UNREADABLE else int(v) for v in s.groups())
            if free is not None and total is not None and free > total: raise WorkloadError(f"slots {free}/{total}: more free than total")
            fields.update(free_slots=free, total_slots=total)
            continue
        if not re.search(r"\b(session|weekly):", line, re.I): continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 3 or not parts[0]: raise WorkloadError(f"usage line {line!r}: want 'client | session: X% (resets HH:MM ET) | weekly: Y% (resets ...)'")
        fields["client"] = None if parts[0].lower() == UNREADABLE else parts[0]
        fields["session_usage"], fields["session_reset"] = _usage("session", parts[1], reported_at)
        fields["weekly_usage"], fields["weekly_reset"] = _usage("weekly", parts[2], reported_at)
    return Report(**fields)


def is_report(text: str) -> bool:
    """Whether a post carries a report line (`slots:`, `session:` or `weekly:`); anything else is chat."""
    return any(re.match(r"slots:", ln.strip().strip("`").strip(), re.I) or re.search(r"\b(session|weekly):", ln, re.I) for ln in str(text).splitlines())


def intake(posts, register, now: datetime, thread: str = "") -> tuple[list[dict], list[dict]]:
    """A thread's posts as `(rows, findings)`. Each row is `parse(...).row(innie)` plus its `source`, ready for `ledger.ingest`.

    A post is a mapping with `sender` (Slack id), `text`, `ts` (Slack ts, epoch seconds) and an
    optional `thread` that overrides the argument. A report whose sender the register does not
    list, whose `ts` is unreadable or later than `now` (aware), or whose lines do not parse is a
    finding `{source, sender, reason}`, never a row."""
    if now.tzinfo is None: raise ValueError("now must be timezone-aware")
    rows: list[dict] = []
    findings: list[dict] = []
    for post in posts:
        text, sender, ts = str(post.get("text") or ""), str(post.get("sender") or ""), str(post.get("ts") or "")
        if not is_report(text): continue
        source = f"{post.get('thread') or thread}/{ts}"
        def finding(reason: str) -> None: findings.append({"source": source, "sender": sender, "reason": reason})
        innie = register.by_slack(sender) if register is not None else None
        if innie is None: finding(f"sender {sender or '(none)'} is not in the register"); continue
        try: at = datetime.fromtimestamp(float(ts), timezone.utc)
        except (TypeError, ValueError, OverflowError): finding(f"unreadable ts {ts!r}"); continue
        if at > now: finding(f"posted at {utc(at)}, after now ({utc(now)})"); continue
        try: report = parse(text, at)
        except WorkloadError as e: finding(str(e)); continue
        rows.append({**report.row(innie.name), "source": source})
    return rows, findings
