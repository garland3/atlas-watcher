"""Per-agent (per-vendor) cooldown after a detected rate/session limit.

A rate limit is account-wide, and accounts belong to vendors: when one
vendor's agent reports a limit, every OTHER vendor can keep working. So the
cooldown is keyed by **vendor** (``anthropic``, ``openai``, ``xai``, ...),
not held as one global pause:

- a limit on vendor X pauses only X;
- a role pauses only when *every* candidate for it is cooling down, and it
  resumes at the earliest reset among them (see
  :func:`atlas_watcher.agents.pick`);
- a scan pass stops only when no role can run at all.

The cooldown is runtime state, not record: it lives in a gitignored file
under ``logs/`` (see :func:`paths.cooldown_file`). It is never written to the
audit repo, and it expires on its own, so no operator has to clear it.

State format (v2)::

    {"agents": {"<vendor>": {"until_ts": ..., "set_at": ...,
                             "consecutive": N, "reason": "..."}}}

A legacy v1 file (a single flat state from before per-agent cooldowns) is
honored as a cooldown on the reserved vendor key ``"*"`` — i.e. the old
global pause keeps pausing everything until it expires, and new records are
per-vendor from then on.
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

# Reserved vendor key for a pre-per-agent (global) cooldown read from a
# legacy v1 state file. It cools every vendor until it expires.
LEGACY_ALL = "*"

# Phrases that mean "the account is capped", case-insensitive. Kept specific:
# a rate-limit message is the whole (short) output of a failed agent, whereas
# a successful summary or findings file may legitimately *discuss* rate
# limiting. The tail-scan in :func:`classify_limit` and the "only failed
# agents" rule in the swarm are what keep this from misfiring on reviewed
# code that talks about 429s.
# The window name is free-form because the CLIs keep renaming it: "session",
# "usage", "5-hour", "weekly", ... Missing one turns a limit into a `failed`
# row and burns the item's retry budget instead of pausing.
_LIMIT_PATTERNS = (
    r"hit your [\w -]{0,24}limit",
    r"(session|usage) limit (reached|exceeded|hit)",
    r"rate limit (reached|exceeded|hit)",
    r"too many requests",
    r"exceeded your current quota",
    r"quota exceeded",
    r"credit balance is too low",
    r"(insufficient|out of) (credits?|quota|balance)",
)

# Safety net for wordings nobody has seen yet, applied only to the short
# output of an agent that *failed* (never anchored/successful output): a
# line naming a cap together with a when-to-retry hint. A miss costs an
# item's retry budget; a false hit only pauses the pass and self-expires.
_FALLBACK_CAP_RE = re.compile(r"\b(limit|quota|credits?)\b", re.IGNORECASE)
_FALLBACK_RETRY_RE = re.compile(
    r"\b(resets?|try again|retry after|upgrade)\b", re.IGNORECASE,
)

# A real limit message is a line or two; a genuine summary/findings file is
# long. Output past this size is treated as content, not an error — except
# that a *failed* agent's log is scanned in its tail (see classify_limit),
# because some CLIs echo the whole prompt into the log before failing, which
# would otherwise bury the limit message past this guard.
LIMIT_OUTPUT_MAX = 2000

# How many lines at the END of a failed agent's log count as "the CLI's own
# error" for reset-trust purposes (see classify_limit): a stated reset is
# honored out to the weekly ceiling only when the limit line sits within
# this many lines of the log's end. A limit-shaped line quoted earlier in
# the output — tool chatter, a file the agent read — can state any reset it
# likes; downgrading that match to weak caps the pause at the ordinary
# backoff ceiling instead of parking a vendor for days on a planted claim.
_RESET_TAIL_LINES = 6

# Anchored matching allows this many characters before the phrase, enough
# for a prefix like "Error: " or "Claude AI " but not for a quoted mention.
_ANCHOR_SLACK = 16

# When the cap lifts, as the CLIs print it:
#   "resets 11am (America/Denver)", "resets 3:30pm", "resets 17:00",
#   "resets Oct 27, 5pm (America/Denver)",
#   "try again at 10:26 PM.", "try again at Oct 30th, 2026 1:34 AM."
# The date and timezone are optional; absent a timezone, local time is used.
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec")
_RESET_RE = re.compile(
    r"(?:resets?|try again)\s+(?:at\s+)?"
    r"(?:(?P<mon>[a-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s*"
    r"(?:(?P<year>\d{4}),?\s*)?)?"
    r"(?P<hour>\d{1,2})(?::(?P<min>\d{2}))?\s*(?P<ampm>am|pm)?\b\.?\s*"
    r"(?:\((?P<tz>[^)]+)\))?",
    re.IGNORECASE,
)
# "try again in 3 hours", "resets in 45 minutes".
_RESET_IN_RE = re.compile(
    r"(?:resets?|try again|retry)\s+in\s+(\d{1,4})\s*"
    r"(minutes?|mins?|m|hours?|hrs?|h|days?|d)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LimitInfo:
    reason: str
    reset_ts: float | None
    # True when the match came from the explicit limit wordings (the CLIs'
    # real messages, sitting in the last few lines of the log — see
    # _RESET_TAIL_LINES) rather than the weak cap-word+retry-hint fallback.
    # A *stated* reset is honored out to agent_limit_reset_max_secs (8
    # days, a weekly limit) only for strong matches; a weak match's reset
    # is capped at the ordinary backoff ceiling, because a guess — or a
    # line planted in the agent's output — must not be able to park a
    # vendor for days.
    strong: bool = True


def normalize_line(line: str) -> str:
    """Whitespace-normalized form of a log line, for echo comparison."""
    return re.sub(r"\s+", " ", line).strip()


def echo_lines(exclude_text: str | None) -> frozenset[str]:
    """Whitespace-normalized lines of the text we ourselves sent to the agent.

    Some CLIs echo their input (the whole prompt, diff included) into the
    log before failing. A limit-shaped line that appears inside that echo
    is upstream content, not the CLI's own error — acting on it would let a
    PR plant a cooldown (or force failover) for a vendor. The match is
    WHOLE-LINE: a log line is ignored only when its normalized form exactly
    equals one of the prompt's lines. A CLI's own message gets a prefix
    ("ERROR: …") and never equals a prompt line, so real limits still match;
    and this repo's own docs (which legitimately contain the phrase on their
    own line) suppress only the exact echoed copy, not the CLI's error.
    """
    if not exclude_text:
        return frozenset()
    return frozenset(normalize_line(line)
                     for line in exclude_text.splitlines())


def log_tail(text: str) -> str:
    """The last ``LIMIT_OUTPUT_MAX`` characters of a failed agent's log.

    Some CLIs echo the whole prompt — diff included — into their log before
    failing, so the CLI's own error sits at the end of an otherwise huge
    log, past the size guard. The cut can land mid-line: the slice's first
    line would then be a partial copy of an echoed prompt line (e.g. "…
    aise Unauthorized") whose normalized form equals no prompt line, so it
    would slip past the echo filter and let upstream text read as the
    CLI's own error. The partial line is dropped — the slice's only
    incomplete line is its first, and the CLI's own message sits at the
    end of the log.
    """
    if len(text) <= LIMIT_OUTPUT_MAX:
        return text
    cut = len(text) - LIMIT_OUTPUT_MAX
    mid_line_cut = text[cut - 1] != "\n"
    text = text[cut:]
    if mid_line_cut and "\n" in text:
        text = text[text.index("\n") + 1:]
    return text


def filtered_lines(text: str, exclude_text: str | None) -> list[str]:
    """``text``'s lines minus echoed input (see :func:`echo_lines`)."""
    echo = echo_lines(exclude_text)
    lines = text.splitlines()
    if not echo:
        return lines
    return [line for line in lines if normalize_line(line) not in echo]


def filtered_tail(text: str, exclude_text: str | None) -> str:
    """The scan window for a failed agent's log: its tail, minus echo.

    Shared by BOTH classifiers — :func:`classify_limit` (limit signatures)
    and the swarm's auth classifier (auth signatures) — so a partial line
    left by the tail cut, or an echoed prompt line, is handled identically
    no matter which signature set scans it (see :func:`log_tail` and
    :func:`echo_lines` for each half's rationale).
    """
    return "\n".join(filtered_lines(log_tail(text), exclude_text))


def _line_match(line: str, anchored: bool) -> tuple[bool, bool]:
    """(matched, strong) for one log line."""
    for p in _LIMIT_PATTERNS:
        m = re.search(p, line, re.IGNORECASE)
        if m and (not anchored or m.start() <= _ANCHOR_SLACK):
            return True, True
    if anchored:
        return False, False
    if _FALLBACK_CAP_RE.search(line) and _FALLBACK_RETRY_RE.search(line):
        return True, False
    return False, False


def classify_limit(text: str, anchored: bool = False, *,
                   exclude_text: str | None = None) -> LimitInfo | None:
    """Return details if ``text`` looks like an agent rate/session limit.

    ``anchored`` requires the phrase to open its line (give or take a short
    prefix such as "Error: "), which is how a CLI prints its own limit
    message. Use it for output from an agent that *succeeded*: such output
    can quote upstream text that mentions a limit — e.g. a triage verdict
    citing another bot's "usage limit reached" PR comment. Unanchored
    matching (failed agents only) also applies the unknown-wording fallback.

    Long text is handled differently by mode:

    - *failed* agents (unanchored): only the **tail** is scanned. Some CLIs
      echo the whole prompt into their log before failing — codex does — so
      the limit message sits at the end of an otherwise huge log, past the
      size guard. Scanning the tail finds it without exposing the scanner to
      quoted limit talk in the middle of real content.
    - *successful* agents (anchored): long output is not scanned at all. A
      successful run's output is review content; the anchored rule alone is
      not enough reason to regex thousands of characters of it.

    The tail cut and the echo filter are the shared :func:`log_tail` /
    :func:`filtered_lines` helpers, so the auth classifier (which scans the
    same window for different signatures) handles partial and echoed lines
    exactly the same way.

    ``exclude_text`` (the prompt we sent, when the caller has it) marks lines
    that are echoed input: a log line whose normalized form exactly equals a
    prompt line is skipped, and the reset time is parsed only from surviving
    match lines — so upstream text cannot plant a multi-day cooldown by
    quoting one in a diff.

    Pure: never writes state. Callers decide whether to record a cooldown.
    """
    if not text:
        return None
    if len(text) > LIMIT_OUTPUT_MAX:
        if anchored:
            return None
        text = log_tail(text)
    lines = filtered_lines(text, exclude_text)
    matched: list[str] = []
    first_match = len(lines)
    strong = False
    for i, line in enumerate(lines):
        hit, is_strong = _line_match(line, anchored)
        if not hit:
            continue
        if not matched:
            # The CLI often states the reset on the line after the limit
            # ("usage limit reached.\nResets 3pm (America/Denver)") — parse
            # from the matched line plus that one neighbor, never the whole
            # tail (the tail may otherwise be echoed prompt/diff text).
            matched = [line, lines[i + 1] if i + 1 < len(lines) else ""]
            first_match = i
        strong = strong or is_strong
    if not matched:
        return None
    # A *stated* reset is only honored out to the weekly ceiling when the
    # limit line sits in the last few lines of the log — where a CLI prints
    # its own error. A strong-wording limit quoted earlier in tool output
    # (a file the agent read, a diff it echoed) can state any reset it
    # likes; downgrading it to a weak match caps the pause at the ordinary
    # backoff ceiling instead of parking the vendor for days on a claim
    # this scanner cannot verify (see _RESET_TAIL_LINES).
    if first_match < len(lines) - _RESET_TAIL_LINES:
        strong = False
    return LimitInfo(reason=matched[0].strip()[:200],
                     reset_ts=_parse_reset("\n".join(matched)),
                     strong=strong)


def _parse_reset(text: str, now: datetime | None = None) -> float | None:
    if m := _RESET_IN_RE.search(text):
        n, unit = int(m[1]), m[2].lower()
        secs = n * (86400 if unit.startswith("d") else
                    3600 if unit.startswith("h") else 60)
        return (now or datetime.now(UTC)).timestamp() + secs
    for m in _RESET_RE.finditer(text):
        ts = _reset_from_match(m, now)
        if ts is not None:
            return ts
    return None


def _reset_from_match(m: re.Match, now: datetime | None) -> float | None:
    hour = int(m["hour"])
    minute = int(m["min"] or 0)
    meridiem = (m["ampm"] or "").lower()
    if meridiem and not 1 <= hour <= 12:
        return None
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    # No timezone → system local time. Build naive datetimes and let
    # .astimezone() apply local rules per date, so a reset on the far side of
    # a DST change isn't off by an hour (a fixed offset would be).
    try:
        tz = ZoneInfo(m["tz"]) if m["tz"] else None
    except (KeyError, ValueError):
        return None
    now = now or datetime.now(UTC)
    now = now.astimezone(tz) if tz else now.astimezone().replace(tzinfo=None)

    if m["mon"]:
        mon = m["mon"].lower()
        if mon not in _MONTHS:
            return None
        try:
            target = datetime(int(m["year"] or now.year), _MONTHS.index(mon) + 1,
                              int(m["day"]), hour, minute, tzinfo=tz)
        except ValueError:
            return None
        # "Jan 2, 5pm" printed in late December means next year.
        if not m["year"] and target < now - timedelta(days=180):
            target = target.replace(year=target.year + 1)
        return _ts(target)

    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return _ts(target)


def _ts(dt: datetime) -> float:
    return (dt if dt.tzinfo else dt.astimezone()).timestamp()


def _load() -> dict[str, dict]:
    """All stored cooldown states keyed by vendor, honoring the v1 format."""
    path = cooldown_file()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    agents = raw.get("agents")
    if isinstance(agents, dict):
        return {str(k): v for k, v in agents.items() if isinstance(v, dict)}
    # Legacy v1: one flat global state. Keep it cooling everything until it
    # expires rather than silently dropping a pause that is still in force.
    if "until_ts" in raw:
        return {LEGACY_ALL: raw}
    return {}


def _save(states: dict[str, dict]) -> None:
    path = cooldown_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"agents": states}, indent=2), encoding="utf-8")


def _live(state: dict) -> dict | None:
    """The state if it is still in force; expired state self-clears."""
    if float(state.get("until_ts", 0)) <= time.time():
        return None
    return state


def active_for(vendor: str) -> dict | None:
    """The cooldown state for ``vendor`` if one is in force, else None.

    Also honors a legacy global (``"*"``) cooldown.
    """
    states = _load()
    for key in (vendor, LEGACY_ALL):
        state = states.get(key)
        if state and (live := _live(state)):
            return live
    return None


def active_all() -> dict[str, dict]:
    """Every cooldown still in force, keyed by vendor (legacy one under "*")."""
    states = _load()
    live = {vendor: s for vendor, s in states.items() if _live(s)}
    if len(live) != len(states):
        _save(live)
    return live


def record(vendor: str, limit: LimitInfo) -> dict:
    """Persist a cooldown for ``vendor`` and return the new state.

    Honors the agent's stated reset time when it parses, otherwise backs off
    exponentially by consecutive limit hits (base * 2^(n-1)), capped both
    ways. The fallback cap matters because a guess must not park the vendor
    for a day: retrying early only re-detects the limit and extends. A
    stated reset from a *strong* match — a known CLI limit wording sitting
    in the last few lines of the log, where a CLI prints its own error (see
    classify_limit) — gets the longer ``agent_limit_reset_max_secs``
    ceiling, since a weekly limit really does last days; a weak
    (fallback-heuristic) match, or a strong wording quoted earlier in the
    output, is capped at the ordinary backoff ceiling — an unverifiable
    claim must not be able to park a vendor for days.

    Only ``vendor`` is paused. Roles running on other vendors keep running.
    """
    now = time.time()
    states = _load()
    prev = states.get(vendor)
    consecutive = 1
    if prev and now - float(prev.get("set_at", 0)) < settings.agent_limit_backoff_max_secs:
        consecutive = int(prev.get("consecutive", 0)) + 1
    fallback = min(
        settings.agent_limit_backoff_secs * (2 ** (consecutive - 1)),
        settings.agent_limit_backoff_max_secs,
    )
    if limit.reset_ts and limit.reset_ts > now:
        ceiling = (settings.agent_limit_reset_max_secs if limit.strong
                   else settings.agent_limit_backoff_max_secs)
        until = min(limit.reset_ts, now + ceiling)
    else:
        until = min(now + fallback, now + settings.agent_limit_backoff_max_secs)
    state = {
        "until_ts": until,
        "set_at": now,
        "consecutive": consecutive,
        "reason": limit.reason,
    }
    states[vendor] = state
    _save(states)
    log(f"agent limit detected on vendor {vendor!r} ({limit.reason!r}); "
        f"pausing {vendor} until "
        f"{datetime.fromtimestamp(until, tz=UTC).astimezone().isoformat(timespec='seconds')} "
        f"(consecutive={consecutive})")
    return state


def earliest_reset(vendors) -> float | None:
    """When the earliest of these vendors' cooldowns lifts, or None.

    A role blocked on cooldown resumes at the earliest reset among its
    candidates; a whole pass resumes when the earliest blocked role can run.

    Liveness is checked on every key exactly as ``active_for`` does: an
    expired per-vendor entry must not hide a still-live legacy ("*")
    cooldown — the error would otherwise report "earliest reset unknown"
    while a global cooldown is still pausing everything.
    """
    states = _load()
    resets = []
    for vendor in vendors:
        for key in (vendor, LEGACY_ALL):
            state = states.get(key)
            if state and (live := _live(state)):
                resets.append(float(live["until_ts"]))
                break
    return min(resets) if resets else None


def clear() -> None:
    cooldown_file().unlink(missing_ok=True)
