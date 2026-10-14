"""End-to-end PR processing."""
from __future__ import annotations

import json
from pathlib import Path

from . import audit, gh, git, ledger, security, swarm
from .config import settings
from .log import log
from .paths import PROMPTS, pr_dir, repo_workdir
from .post import post_pr_comment
from .scrub import scrub_identity


def _format_comments(comments: list | None) -> str:
    """Render PR discussion as plain text for the context fed to agents.

    The evidence reviewer relies on this to spot screenshots / verification
    notes posted as follow-up comments rather than in the description.
    """
    if not comments:
        return "_No comments._"
    blocks = []
    for c in comments:
        author = (c.get("author") or {}).get("login", "?")
        when = c.get("createdAt", "")
        body = c.get("body") or ""
        blocks.append(f"**{author}** ({when}):\n\n{body}")
    return "\n\n---\n\n".join(blocks)


def _build_context(repo: str, meta: dict, workdir: Path,
                   base_ref: str, sha_dir: Path) -> Path:
    diff = git.diff_against_base(workdir, base_ref)
    files = git.name_status_against_base(workdir, base_ref)

    diff_path = sha_dir / "diff.patch"
    diff_path.write_text(diff, encoding="utf-8")

    capped = diff.encode("utf-8")[: settings.diff_context_max_bytes].decode("utf-8", errors="ignore")

    parts = [
        f"## {repo} PR #{meta['number']} — {meta['title']}\n",
        f"**URL:** {meta['url']}",
        f"**Author:** {meta['author']['login']}",
        f"**Base:** {meta['baseRefName']}  **Head:** {meta['headRefName']} @ {meta['headRefOid']}",
        f"**Changed files:** {meta['changedFiles']}  (+{meta['additions']} / -{meta['deletions']})\n",
        f"### Description\n\n{meta.get('body') or ''}\n",
        f"### Discussion / comments\n\n{_format_comments(meta.get('comments'))}\n",
        f"### Files changed\n\n```\n{files}\n```\n",
        "### Unified diff\n\n```diff\n" + capped + "\n```\n",
    ]
    ctx_path = sha_dir / "context.md"
    ctx_path.write_text("\n".join(parts), encoding="utf-8")
    return ctx_path


def _run_manager(repo: str, sha_dir: Path, review_round: int) -> Path:
    perspectives_dir = sha_dir / "perspectives"
    summary_path = sha_dir / "summary.md"
    summary_path.unlink(missing_ok=True)

    base_prompt = (PROMPTS / "manager.md").read_text(encoding="utf-8")
    runtime = (
        "## Runtime inputs\n\n"
        f"- `REPO` (upstream being reviewed): `{repo}`\n"
        f"- `PERSPECTIVES_DIR`: `{perspectives_dir}`\n"
        f"- `SUMMARY_PATH`: `{summary_path}`\n"
        f"- `REVIEW_ROUND`: `{review_round}`\n"
    )
    full_prompt = f"{base_prompt}\n\n---\n\n{runtime}\n"

    log_path = sha_dir / "manager.log"
    rc = swarm.run_single(
        "dclaude", full_prompt,
        out_file=sha_dir / "manager.stdout.log",
        log_file=log_path,
        # The manager only reads PERSPECTIVES_DIR and writes SUMMARY_PATH, both
        # absolute. It is deliberately NOT given the audit repo as its cwd —
        # publishing is the harness's job (audit.sync), so an agent that has
        # just ingested untrusted upstream text never has a git remote in reach.
        cwd=sha_dir,
    )
    log(f"manager rc={rc}")

    if not summary_path.is_file() or summary_path.stat().st_size == 0:
        raise RuntimeError(
            f"manager did not produce {summary_path}; see {log_path}"
        )

    summary_path.write_text(
        scrub_identity(summary_path.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    return summary_path


def _generate_review(repo: str, meta: dict, sha_dir: Path) -> Path:
    sha_dir.mkdir(parents=True, exist_ok=True)
    signature = f"_Posted automatically by **{settings.bot_name}**_"
    review_round = 1 + sum(
        signature in (comment.get("body") or "")
        for comment in meta.get("comments", [])
    )
    (sha_dir / "metadata.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8",
    )
    workdir = repo_workdir(repo)
    git.checkout_pr(repo, workdir, meta["number"], meta["baseRefName"])
    ctx_path = _build_context(repo, meta, workdir, meta["baseRefName"], sha_dir)
    swarm.run_swarm(repo, meta["number"], workdir, sha_dir, ctx_path)
    return _run_manager(repo, sha_dir, review_round)


def _record_block(sha_dir: Path, verdict: dict) -> None:
    """Write a blocked.json sentinel that the ledger reconciler treats
    as terminal evidence."""
    (sha_dir / "blocked.json").write_text(
        json.dumps({
            "blocked": True,
            "reason": "security_triage",
            "final_risk": verdict.get("final_risk"),
            "final_score": verdict.get("final_score"),
        }, indent=2),
        encoding="utf-8",
    )


def process(repo: str, pr_number: int, dry_run: bool = False) -> str:
    meta = gh.view_pr(repo, pr_number)
    if meta["state"] != "OPEN":
        log(f"{repo} PR #{pr_number} is {meta['state']} — skipping")
        return "skipped:state"
    if meta["isDraft"]:
        log(f"{repo} PR #{pr_number} is draft — skipping")
        return "skipped:draft"

    sha = meta["headRefOid"]
    if ledger.has_done(repo, "pr", pr_number, sha):
        log(f"{repo} PR #{pr_number} @ {sha} already reviewed — skipping")
        return "skipped:ledger"
    if ledger.retries_exhausted(repo, "pr", pr_number, sha):
        log(f"{repo} PR #{pr_number} @ {sha[:12]} hit the retry limit "
            f"({settings.max_retries} failed attempts) — skipping; remove "
            f"'failed' rows from the ledger or push a new commit to retry")
        return "skipped:retries"
    if not ledger.retry_ready(repo, "pr", pr_number, sha):
        log(f"{repo} PR #{pr_number} @ {sha[:12]} in retry backoff until "
            f"{ledger.next_retry_at(repo, 'pr', pr_number, sha)} — skipping")
        return "skipped:backoff"

    try:
        return _process(repo, meta, dry_run)
    except Exception as e:
        if not dry_run:
            # A rate/session limit is account-wide: record it as "deferred"
            # so it neither counts against the retry budget nor is mistaken
            # for a defect in this PR. The cooldown gates the next attempt.
            status = "deferred" if isinstance(e, swarm.AgentLimitError) else "failed"
            try:
                ledger.append(repo, "pr", pr_number, sha, status=status,
                              error=repr(e)[:500])
            except Exception as le:
                log(f"could not record failure in ledger: {le!r}")
        raise


def _process(repo: str, meta: dict, dry_run: bool) -> str:
    pr_number = meta["number"]
    sha = meta["headRefOid"]
    sha_dir = pr_dir(repo, pr_number, sha)
    sha_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: security triage on upstream-controlled text only.
    verdict = security.assess(
        repo, "pr", pr_number,
        security.build_pr_text(meta),
        sha_dir,
    )

    if verdict["decision"] == "block":
        log(f"{repo} PR #{pr_number} BLOCKED by security triage "
            f"(risk={verdict['final_risk']}, score={verdict['final_score']})")
        _record_block(sha_dir, verdict)
        if not dry_run:
            ledger.append(repo, "pr", pr_number, sha, status="security_blocked")
            audit.sync(message=f"block {repo} PR #{pr_number} @ {sha[:12]}")
        return "blocked"

    log(f"Processing {repo} PR #{pr_number} @ {sha} — {meta['title']!r}")

    summary_path = sha_dir / "summary.md"
    if summary_path.is_file() and summary_path.stat().st_size > 0:
        log(f"reusing existing summary at {summary_path}")
    else:
        summary_path = _generate_review(repo, meta, sha_dir)

    # Step 2: if the verdict was 'warn', prepend a neutral banner before
    # posting (kept out of summary.md so re-posting doesn't double-stack).
    post_path = summary_path
    if verdict["decision"] == "warn":
        banner_path = sha_dir / "summary.warned.md"
        banner_path.write_text(
            security.warning_banner(verdict)
            + summary_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        post_path = banner_path

    if dry_run:
        log(f"DRY_RUN — not posting; summary at {post_path}")
        return "dry-run"

    url = post_pr_comment(repo, pr_number, post_path)
    (sha_dir / "posted.json").write_text(
        json.dumps({"comment_url": url,
                    "security_decision": verdict["decision"]}, indent=2),
        encoding="utf-8",
    )
    log(f"posted: {url}")

    ledger.append(repo, "pr", pr_number, sha)
    audit.sync(message=f"finalize {repo} PR #{pr_number} @ {sha[:12]}")
    return "done"
