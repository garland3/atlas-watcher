"""Append-only JSONL ledger driving idempotency."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal

from .config import settings
from .paths import issue_dir, ledger_file, pr_dir

Kind = Literal["pr", "issue"]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append(repo: str, kind: Kind, number: int, key: str,
           status: str = "done", error: str | None = None) -> None:
    """Append one record. Caller is responsible for idempotency check."""
    ledger_file().parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": _now(),
        "repo": repo,
        "kind": kind,
        "number": number,
        "key": key,
        "status": status,
    }
    if error is not None:
        record["error"] = error
    with ledger_file().open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


# Statuses that should stop the watcher from re-processing an item.
_TERMINAL_STATUSES = {"done", "security_blocked"}


def _iter_rows():
    """Yield parsed ledger rows, tolerating blank/malformed lines."""
    if not ledger_file().is_file():
        return
    with ledger_file().open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _repo_matches(rec: dict, repo: str, default_repo: str | None) -> bool:
    """Backward-compat: rows written before the multi-repo migration have no
    ``repo`` field. Those match only the default repo (``settings.repos[0]``)
    — that's where the data originally came from."""
    rec_repo = rec.get("repo")
    return rec_repo == repo or (rec_repo is None and repo == default_repo)


def has_done(repo: str, kind: Kind, number: int, key: str) -> bool:
    """True if (repo, kind, number, key) has already reached a terminal state.

    Terminal = any status in :data:`_TERMINAL_STATUSES` (``done`` for
    successfully-posted reviews, ``security_blocked`` for items the
    security triage refused to process).
    """
    default_repo = settings.repos[0] if settings.repos else None
    return any(
        True for rec in _iter_rows()
        if rec.get("kind") == kind
        and rec.get("number") == number
        and rec.get("key") == key
        and rec.get("status") in _TERMINAL_STATUSES
        and _repo_matches(rec, repo, default_repo)
    )


def failure_count(repo: str, kind: Kind, number: int, key: str) -> int:
    """Number of failed attempts recorded for (repo, kind, number, key)."""
    default_repo = settings.repos[0] if settings.repos else None
    return sum(
        1 for rec in _iter_rows()
        if rec.get("kind") == kind
        and rec.get("number") == number
        and rec.get("key") == key
        and rec.get("status") == "failed"
        and _repo_matches(rec, repo, default_repo)
    )


def retries_exhausted(repo: str, kind: Kind, number: int, key: str) -> bool:
    """True once an item has failed ``settings.max_retries`` times at the
    same key. Exhausted items are skipped until the key changes (new PR
    commit / new issue comment) or the ``failed`` rows are removed from
    the ledger."""
    return failure_count(repo, kind, number, key) >= settings.max_retries


def _has_terminal_evidence(rec: dict) -> bool:
    """True if the row is backed by an artifact on disk proving the work
    actually completed.

    For ``done`` rows, that's ``posted.json`` (real upstream comment, or
    a deliberate NO_COMMENT outcome on issues).
    For ``security_blocked`` rows, that's ``blocked.json``.
    """
    kind = rec.get("kind")
    number = rec.get("number")
    key = rec.get("key", "")
    status = rec.get("status")
    repo = rec.get("repo") or (settings.repos[0] if settings.repos else "")
    if not repo or kind not in ("pr", "issue"):
        return False
    base = (pr_dir(repo, int(number), str(key))
            if kind == "pr"
            else issue_dir(repo, int(number), str(key)))

    if status == "security_blocked":
        return (base / "blocked.json").is_file()

    posted = base / "posted.json"
    if not posted.is_file():
        return False
    try:
        data = json.loads(posted.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if kind == "pr":
        return bool(data.get("comment_url"))
    if data.get("dry_run") is True:
        return False
    return True


def reconcile() -> tuple[int, int]:
    """Drop ``done`` rows that have no posted.json evidence on disk.

    Returns ``(kept_count, dropped_count)``. Non-done rows and malformed
    lines are kept untouched. The ledger file is rewritten in place.
    """
    if not ledger_file().is_file():
        return 0, 0
    kept: list[str] = []
    dropped = 0
    for raw in ledger_file().read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            kept.append(line)
            continue
        if (rec.get("status") in _TERMINAL_STATUSES
                and not _has_terminal_evidence(rec)):
            dropped += 1
            continue
        kept.append(line)
    ledger_file().write_text(
        ("\n".join(kept) + "\n") if kept else "",
        encoding="utf-8",
    )
    return len(kept), dropped
