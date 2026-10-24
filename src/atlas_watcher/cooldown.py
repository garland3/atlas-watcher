"""Global cooldown after a detected agent rate/session limit.

A rate limit is account-wide, not per item: when one agent reports it, every
other agent will hit the same wall until it resets. The watcher runs
unattended on cron, so recovery must be automatic — this module makes the
whole pass skip itself until the limit expires, rather than launching swarms
that are guaranteed to fail and recording failures the item did not cause.

The cooldown is runtime state, not record: it lives in a gitignored file
under ``logs/`` (see :func:`paths.cooldown_file`). It is never written to the
audit repo, and it expires on its own, so no operator has to clear it.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .config import settings
from .log import log
from .paths import cooldown_file

# Phrases that mean "the account is capped", case-insensitive. Kept specific:
# a rate-limit message is the whole (short) output of a failed agent, whereas
# a successful summary or findings file may legitimately *discuss* rate
# limiting. The size guard in :func:`classify_limit` and the "only failed
# agents" rule in the swarm are what keep this from misfiring on reviewed
# code that talks about 429s.
_LIMIT_PATTERNS = (
    r"you'?ve hit your (session|usage|rate) limit",
    r"hit your (session|usage|rate) limit",
    r"(session|usage) limit (reached|exceeded|hit)",
    r"rate limit (reached|exceeded|hit)",
    r"too many requests",
    r"exceeded your current quota",
    r"quota exceeded",
)

# A real limit message is a line or two; a genuine summary/findings file is
# long. Output past this size is treated as content, not an error.
_LIMIT_OUTPUT_MAX = 2000

# Anchored matching allows this many characters before the phrase, enough
# for a prefix like "Error: " or "Claude AI " but not for a quoted mention.
_ANCHOR_SLACK = 16

# "resets 11am (America/Denver)", "resets 3:30pm (America/Los_Angeles)",
# "resets 17:00". The timezone is optional; absent, we assume local time.
_RESET_RE = re.compile(
    r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:\(([^)]+)\))?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LimitInfo:
    reason: str
    reset_ts: float | None


def _line_matches(line: str, anchored: bool) -> bool:
    for p in _LIMIT_PATTERNS:
        m = re.search(p, line, re.IGNORECASE)
        if m and (not anchored or m.start() <= _ANCHOR_SLACK):
            return True
    return False


def classify_limit(text: str, anchored: bool = False) -> LimitInfo | None:
    """Return details if ``text`` looks like an agent rate/session limit.

    ``anchored`` requires the phrase to open its line (give or take a short
    prefix such as "Error: "), which is how a CLI prints its own limit
    message. Use it for output from an agent that *succeeded*: such output
    can quote upstream text that mentions a limit — e.g. a triage verdict
    citing another bot's "usage limit reached" PR comment.

    Pure: never writes state. Callers decide whether to record a cooldown.
    """
    if not text or len(text) > _LIMIT_OUTPUT_MAX:
        return None
    for line in text.splitlines():
        if _line_matches(line, anchored):
            return LimitInfo(reason=line.strip()[:200],
                             reset_ts=_parse_reset(text))
    return None


def _parse_reset(text: str) -> float | None:
    m = _RESET_RE.search(text)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    meridiem = (m.group(3) or "").lower()
    tz_name = m.group(4)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    try:
        tz = ZoneInfo(tz_name) if tz_name else datetime.now().astimezone().tzinfo
    except (KeyError, ValueError):
        return None
    if tz is None:
        return None
    now = datetime.now(tz)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.timestamp()


def read() -> dict | None:
    path = cooldown_file()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def active() -> dict | None:
    """The cooldown if it is still in force; expired state self-clears."""
    state = read()
    if not state:
        return None
    if float(state.get("until_ts", 0)) <= time.time():
        clear()
        return None
    return state


def record(limit: LimitInfo) -> dict:
    """Persist a cooldown for a detected limit and return the new state.

    Honors the agent's stated reset time when it parses, otherwise backs off
    exponentially by consecutive limit hits (base * 2^(n-1)), capped both
    ways. The cap matters because a mis-parsed reset must not park the watcher
    for a day: retrying early only re-detects the limit and extends.
    """
    now = time.time()
    prev = read()
    consecutive = 1
    if prev and now - float(prev.get("set_at", 0)) < settings.agent_limit_backoff_max_secs:
        consecutive = int(prev.get("consecutive", 0)) + 1
    fallback = min(
        settings.agent_limit_backoff_secs * (2 ** (consecutive - 1)),
        settings.agent_limit_backoff_max_secs,
    )
    until = limit.reset_ts if (limit.reset_ts and limit.reset_ts > now) else now + fallback
    until = min(until, now + settings.agent_limit_backoff_max_secs)
    state = {
        "until_ts": until,
        "set_at": now,
        "consecutive": consecutive,
        "reason": limit.reason,
    }
    cooldown_file().parent.mkdir(parents=True, exist_ok=True)
    cooldown_file().write_text(json.dumps(state, indent=2), encoding="utf-8")
    log(f"agent limit detected ({limit.reason!r}); pausing until "
        f"{datetime.fromtimestamp(until, tz=UTC).astimezone().isoformat(timespec='seconds')} "
        f"(consecutive={consecutive})")
    return state


def clear() -> None:
    cooldown_file().unlink(missing_ok=True)
