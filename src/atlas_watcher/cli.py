"""Typer CLI entry point.

    atlas-watcher scan                     # one pass over every repo in REPOS
    atlas-watcher loop                     # poll forever
    atlas-watcher pr 42                    # one PR on the default repo
    atlas-watcher pr 42 --repo o/r         # one PR on a specific repo
    atlas-watcher issue 99                 # one issue on the default repo
    atlas-watcher audit-init               # bootstrap the audit-record repo
    atlas-watcher sync                     # commit & push the audit trail
    atlas-watcher config                   # show resolved settings
"""
from __future__ import annotations

import time
from datetime import UTC, datetime

import typer

from . import audit, cooldown, gh, issue as issue_mod, ledger as ledger_mod, pr as pr_mod
from .config import settings
from .log import log
from .paths import data_root

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="Multi-repo review bot. Posts as the configured BOT_NAME.")


def _resolve_repo(repo: str | None) -> str:
    return repo or settings.default_repo


@app.command()
def pr(
    number: int = typer.Argument(..., help="PR number"),
    repo: str = typer.Option(None, "--repo", "-r",
                             help="owner/name; defaults to first in REPOS"),
    dry_run: bool = typer.Option(False, "--dry-run",
                                 help="Run the swarm but don't post the comment."),
) -> None:
    """Review one PR."""
    pr_mod.process(_resolve_repo(repo), number, dry_run=dry_run)


@app.command()
def issue(
    number: int = typer.Argument(..., help="Issue number"),
    repo: str = typer.Option(None, "--repo", "-r",
                             help="owner/name; defaults to first in REPOS"),
    dry_run: bool = typer.Option(False, "--dry-run",
                                 help="Triage but don't post the clarifier."),
) -> None:
    """Triage one issue."""
    issue_mod.process(_resolve_repo(repo), number, dry_run=dry_run)


def _scan_repo(repo: str, dry_run: bool) -> None:
    log(f"scan: {repo}")
    try:
        prs = gh.list_open_prs(repo)
    except Exception as e:
        log(f"{repo}: list PRs failed: {e!r}")
        prs = []
    for n in prs:
        if cooldown.active():
            log(f"{repo}: agent limit active — stopping this pass")
            return
        try:
            pr_mod.process(repo, n, dry_run=dry_run)
        except Exception as e:
            log(f"{repo} PR #{n} failed: {e!r}")

    try:
        issues = gh.list_open_issues(repo)
    except Exception as e:
        log(f"{repo}: list issues failed: {e!r}")
        issues = []
    for n in issues:
        if cooldown.active():
            log(f"{repo}: agent limit active — stopping this pass")
            return
        try:
            issue_mod.process(repo, n, dry_run=dry_run)
        except Exception as e:
            log(f"{repo} issue #{n} failed: {e!r}")


def _one_pass(dry_run: bool) -> None:
    state = cooldown.active()
    if state:
        until = datetime.fromtimestamp(state["until_ts"], tz=UTC).astimezone().isoformat(timespec="seconds")
        log(f"agent cooldown active until {until} — skipping pass")
        return
    log(f"scan start: {len(settings.repos)} repo(s) — {settings.repos}")
    for repo in settings.repos:
        _scan_repo(repo, dry_run)
        if cooldown.active():
            log("agent limit hit mid-pass — stopping scan")
            break
    if not dry_run:
        try:
            audit.sync()
        except Exception as e:
            log(f"audit sync failed: {e!r}")
    log("scan done")


@app.command()
def scan(
    dry_run: bool = typer.Option(False, "--dry-run",
                                 help="Run end to end but don't post or push."),
) -> None:
    """One pass over every configured repo's open PRs and issues."""
    _one_pass(dry_run=dry_run)


@app.command()
def loop(
    dry_run: bool = typer.Option(False, "--dry-run",
                                 help="Loop forever but don't post or push."),
) -> None:
    """Poll forever, sleeping POLL_INTERVAL_SECS between passes."""
    while True:
        try:
            _one_pass(dry_run=dry_run)
        except Exception as e:
            log(f"pass failed: {e!r}")
        log(f"sleeping {settings.poll_interval_secs}s")
        time.sleep(settings.poll_interval_secs)


@app.command()
def sync() -> None:
    """Commit and push reviews/ and ledger/ to the audit repo."""
    audit.sync()


@app.command("cooldown")
def cooldown_cmd(
    clear: bool = typer.Option(False, "--clear",
                               help="Clear an active cooldown (debugging only)."),
) -> None:
    """Show the global agent rate-limit cooldown.

    Recovery is automatic: the cooldown expires and the next scan resumes on
    its own. --clear exists only for debugging a stuck timer.
    """
    if clear:
        cooldown.clear()
        typer.echo("cooldown cleared")
        return
    state = cooldown.active()
    if not state:
        typer.echo("no active cooldown")
        return
    until = datetime.fromtimestamp(state["until_ts"], tz=UTC).astimezone().isoformat(timespec="seconds")
    typer.echo(f"cooldown until {until} (consecutive={state.get('consecutive')})")
    typer.echo(f"reason: {state.get('reason')}")


@app.command("audit-init")
def audit_init() -> None:
    """Create and initialize the audit-record repo at AUDIT_DIR.

    Idempotent, local-only: it never pushes and never rewrites history. After
    running it, create the private GitHub repo AUDIT_REPO and push once by
    hand; from then on ``sync`` keeps it current.
    """
    root = audit.ensure_repo()
    typer.echo(f"audit record repo: {root}")
    typer.echo(f"remote origin    : git@github.com:{settings.audit_repo}.git")
    typer.echo("")
    typer.echo("Next: create that repo on GitHub as PRIVATE (it contains "
               "verbatim upstream diffs), then push once:")
    typer.echo(f"  git -C {root} add -A && git -C {root} commit -m 'initial audit record'")
    typer.echo(f"  git -C {root} push -u origin main")


@app.command()
def config() -> None:
    """Print resolved settings."""
    typer.echo(settings.model_dump_json(indent=2))
    typer.echo(f"# resolved audit record root: {data_root()}")
    typer.echo(f"# initialized: {audit.is_initialized()}")


@app.command("reconcile-ledger")
def reconcile_ledger() -> None:
    """Drop ``done`` rows that have no posted.json on disk.

    Useful after dry-runs or partial runs left the ledger thinking work
    was finished when it wasn't actually posted upstream.
    """
    kept, dropped = ledger_mod.reconcile()
    typer.echo(f"kept {kept} rows; dropped {dropped} rows lacking posted.json")
