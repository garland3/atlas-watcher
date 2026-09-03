"""Git checkout helpers. One persistent clone per upstream repo."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def _git(workdir: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(workdir), *args],
        check=True, capture_output=True, text=True,
    ).stdout


def ensure_clone(repo: str, workdir: Path) -> None:
    """Clone the repo into `workdir` if it isn't already there.

    The clone is kept across runs — we never rm it. If `workdir` exists
    but is not a git repo, we treat it as junk and replace it.
    """
    if (workdir / ".git").is_dir():
        return
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["gh", "repo", "clone", repo, str(workdir), "--", "--quiet"],
        check=True,
    )


def reset_worktree(workdir: Path) -> None:
    """Discard any local modifications left behind in the shared clone.

    The clone is reused across every review, and agents routinely run tools
    (`uv sync`, formatters, build steps) that rewrite tracked files such as
    `uv.lock`. A single leftover modification makes every later
    `git checkout pr-N` abort with "local changes would be overwritten",
    which silently wedges the watcher for all PRs. Nothing in this workdir is
    ever authored by us, so discarding is always safe.
    """
    _git(workdir, "reset", "--hard", "--quiet")
    subprocess.run(
        ["git", "-C", str(workdir), "clean", "-qfd"],
        check=False, capture_output=True,
    )


def checkout_pr(repo: str, workdir: Path, pr_number: int, base_ref: str) -> None:
    """Fetch and check out the PR head into `workdir` (clone in place)."""
    ensure_clone(repo, workdir)
    reset_worktree(workdir)
    _git(workdir, "fetch", "--quiet", "origin")
    _git(workdir, "checkout", "--force", "--quiet", base_ref)
    subprocess.run(
        ["git", "-C", str(workdir), "pull", "--quiet", "--ff-only", "origin", base_ref],
        check=False, capture_output=True,
    )
    _git(workdir, "fetch", "--quiet", "--force", "origin",
         f"pull/{pr_number}/head:pr-{pr_number}")
    _git(workdir, "checkout", "--force", "--quiet", f"pr-{pr_number}")


def diff_against_base(workdir: Path, base_ref: str) -> str:
    return _git(workdir, "diff", f"origin/{base_ref}...HEAD")


def name_status_against_base(workdir: Path, base_ref: str) -> str:
    return _git(workdir, "diff", "--name-status", f"origin/{base_ref}...HEAD")
