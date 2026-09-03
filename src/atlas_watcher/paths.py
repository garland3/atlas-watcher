"""Filesystem layout for atlas-watcher."""
from __future__ import annotations

from pathlib import Path


def repo_root() -> Path:
    """Project root — the directory containing pyproject.toml."""
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError("could not locate atlas-watcher repo root")


ROOT = repo_root()
PROMPTS = ROOT / "prompts"
WORKDIR = ROOT / "workdir"
LOGS = ROOT / "logs"


# --- durable audit record -------------------------------------------------
# The audit record (reviews/ + ledger/) lives in its OWN git repo so this
# codebase can be published without dragging along third-party diffs and raw
# agent transcripts. Its location comes from the AUDIT_DIR setting, so these
# are functions rather than module constants.
#
# Note: config.py imports ROOT from this module at import time, so `settings`
# must be imported lazily *inside* these functions or we deadlock on a
# circular import.


def data_root() -> Path:
    """Root of the audit-record repo. Contains reviews/ and ledger/."""
    from .config import settings
    raw = settings.audit_dir.strip()
    p = Path(raw).expanduser() if raw else ROOT / "audit-data"
    if not p.is_absolute():
        p = ROOT / p
    return p.resolve()


def reviews_dir() -> Path:
    return data_root() / "reviews"


def ledger_file() -> Path:
    return data_root() / "ledger" / "ledger.jsonl"


def repo_slug(repo: str) -> tuple[str, str]:
    """Split 'owner/name' into ('owner', 'name'). Raises on malformed input."""
    if repo.count("/") != 1 or not all(repo.split("/")):
        raise ValueError(f"invalid repo (expected owner/name): {repo!r}")
    owner, name = repo.split("/", 1)
    return owner, name


def repo_reviews_dir(repo: str) -> Path:
    owner, name = repo_slug(repo)
    return reviews_dir() / owner / name


def pr_dir(repo: str, number: int, sha: str) -> Path:
    return repo_reviews_dir(repo) / "prs" / f"PR-{number}" / sha


def issue_dir(repo: str, number: int, key: str) -> Path:
    return repo_reviews_dir(repo) / "issues" / f"ISSUE-{number}" / key.replace(":", "-")


def repo_workdir(repo: str) -> Path:
    """Persistent clone path for a repo. We clone once and reuse it."""
    owner, name = repo_slug(repo)
    return WORKDIR / owner / name
