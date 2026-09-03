"""Commit and push the durable audit record (reviews/, ledger/).

The record lives in its own git repo — see :func:`atlas_watcher.paths.data_root`
and the ``AUDIT_DIR`` setting — so this codebase can be published without
dragging along third-party diffs and raw agent transcripts. Everything here
operates on that repo, never on the code repo.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .config import settings
from .log import log
from .paths import data_root

# Runtime debris that lives under reviews/ but is NOT part of the durable
# record: per-agent shell runners, sentinels, generated prompts, and raw
# stdout/stderr transcripts. The transcripts in particular are never passed
# through scrub.py, so they can carry model/vendor identity. They stay on
# disk for debugging; they are not committed.
DATA_GITIGNORE = """\
# Runtime debris — kept on disk for debugging, never committed.
# The durable record is metadata.json, context.md, diff.patch,
# perspectives/*.md, security.{json,md}, summary.md, analysis.md,
# comment.md, posted.json, blocked.json and the ledger.
**/perspectives/.logs/
**/perspectives/.runners/
**/perspectives/.sentinels/
**/perspectives/.*.prompt.md
*.log
*.raw.txt
"""

README = """\
# ATLAS-WATCHER-LOGS

Durable audit record produced by [atlas-watcher](https://github.com/garland3/atlas-watcher).
Written by the bot, not by hand.

**This repo is private and must stay private.** It contains verbatim diffs
from the upstream repositories under review.

- `reviews/<owner>/<name>/prs/PR-<n>/<sha>/` — one folder per PR head SHA.
- `reviews/<owner>/<name>/issues/ISSUE-<n>/comments-<n>/` — one per triage snapshot.
- `ledger/ledger.jsonl` — append-only log; the watcher's idempotency source.

Runtime transcripts (`.logs/`, `.runners/`, `.sentinels/`) are deliberately
gitignored: they are unscrubbed debug output, not part of the record.
"""


def _git(*args: str, check: bool = True,
         cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd or data_root()), *args],
        check=check, capture_output=True, text=True,
    )


def is_initialized() -> bool:
    return (data_root() / ".git").is_dir()


def ensure_repo() -> Path:
    """Create and initialize the audit-record repo if it isn't there yet.

    Idempotent: safe to run against an existing repo. Never touches history,
    never pushes.
    """
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "reviews").mkdir(exist_ok=True)
    (root / "ledger").mkdir(exist_ok=True)

    if not (root / ".git").is_dir():
        _git("init", "-q", "-b", "main")
        log(f"audit: initialized {root}")

    gitignore = root / ".gitignore"
    if not gitignore.is_file():
        gitignore.write_text(DATA_GITIGNORE, encoding="utf-8")
    readme = root / "README.md"
    if not readme.is_file():
        readme.write_text(README, encoding="utf-8")

    # Point origin at AUDIT_REPO, but never clobber an existing remote.
    remotes = _git("remote", check=False).stdout.split()
    if "origin" not in remotes:
        url = f"git@github.com:{settings.audit_repo}.git"
        _git("remote", "add", "origin", url, check=False)
        log(f"audit: origin -> {url}")
    return root


def sync(message: str | None = None) -> bool:
    """Commit and push reviews/ + ledger/. Returns True if a commit was made."""
    if not is_initialized():
        log(f"audit: {data_root()} is not a git repo — "
            f"run 'atlas-watcher audit-init'; skipping sync")
        return False

    _git("add", "reviews", "ledger")
    diff = _git("diff", "--cached", "--quiet", check=False)
    if diff.returncode == 0:
        log("audit: nothing to commit")
        return False

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _git("commit", "-q", "-m", message or f"audit: sync {ts}")

    push = _git("push", "-q", "origin", "main", check=False)
    if push.returncode != 0:
        # Committed locally; the record is safe. A missing/unreachable remote
        # must not take down a scan pass.
        log(f"audit: committed, push failed: {push.stderr.strip()[:200]}")
        return True
    log("audit: pushed")
    return True
