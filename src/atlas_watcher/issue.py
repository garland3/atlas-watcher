"""End-to-end issue triage."""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import audit, gh, ledger, security, swarm
from .config import settings
from .log import log
from .paths import PROMPTS, issue_dir
from .post import post_issue_comment
from .scrub import scrub_identity


_ANALYSIS_RE = re.compile(
    r"=====\s*ANALYSIS\s*=====\s*(.*?)\s*=====\s*END ANALYSIS\s*=====",
    re.DOTALL | re.IGNORECASE,
)
_COMMENT_RE = re.compile(
    r"=====\s*COMMENT\s*=====\s*(.*?)\s*=====\s*END COMMENT\s*=====",
    re.DOTALL | re.IGNORECASE,
)


def _build_context(repo: str, meta: dict) -> str:
    labels = ", ".join(label["name"] for label in meta.get("labels", []))
    parts = [
        f"## {repo} Issue #{meta['number']} — {meta['title']}\n",
        f"**URL:** {meta['url']}",
        f"**Author:** {meta['author']['login']}",
        f"**Labels:** {labels}\n",
        f"### Body\n\n{meta.get('body') or ''}\n",
        "### Comments\n",
    ]
    for c in meta.get("comments", []):
        parts.append(f"**{c['author']['login']}:** {c.get('body') or ''}\n")
    return "\n".join(parts)


def _generate_triage(repo: str, meta: dict, snap_dir: Path) -> None:
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "metadata.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8",
    )
    ctx = _build_context(repo, meta)
    (snap_dir / "context.md").write_text(ctx, encoding="utf-8")

    identity = (PROMPTS / "identity.md").read_text(encoding="utf-8")
    triage = (PROMPTS / "issue-triage.md").read_text(encoding="utf-8")
    full = (
        f"{identity}\n\n---\n\n{triage}\n\n---\n\n"
        f"===== ISSUE =====\n\n{ctx}\n===== END ISSUE =====\n"
    )

    raw_path = snap_dir / "triage.raw.md"
    result = swarm.run_role("issue-triage", full, raw_path,
                            log_file=snap_dir / "triage.log",
                            raise_on_failure=False)
    audit.merge_ran(snap_dir, "issue_triage",
                    {"agent": result.agent, "vendor": result.vendor})
    raw = raw_path.read_text(encoding="utf-8")

    a_match = _ANALYSIS_RE.search(raw)
    c_match = _COMMENT_RE.search(raw)
    analysis_text = a_match.group(1).strip() if a_match else raw
    comment_text = (c_match.group(1).strip() if c_match else "") or "NO_COMMENT"

    (snap_dir / "analysis.md").write_text(scrub_identity(analysis_text),
                                          encoding="utf-8")
    (snap_dir / "comment.md").write_text(scrub_identity(comment_text),
                                         encoding="utf-8")


def _record_block(snap_dir: Path, verdict: dict) -> None:
    (snap_dir / "blocked.json").write_text(
        json.dumps({
            "blocked": True,
            "reason": "security_triage",
            "final_risk": verdict.get("final_risk"),
            "final_score": verdict.get("final_score"),
        }, indent=2),
        encoding="utf-8",
    )


def process(repo: str, issue_number: int, dry_run: bool = False) -> str:
    meta = gh.view_issue(repo, issue_number)
    if meta["state"] != "OPEN":
        log(f"{repo} issue #{issue_number} is {meta['state']} — skipping")
        return "skipped:state"

    n_comments = len(meta.get("comments", []))
    key = f"comments:{n_comments}"
    if ledger.has_done(repo, "issue", issue_number, key):
        log(f"{repo} issue #{issue_number} already triaged at {key} — skipping")
        return "skipped:ledger"
    if ledger.retries_exhausted(repo, "issue", issue_number, key):
        log(f"{repo} issue #{issue_number} at {key} hit the retry limit "
            f"({settings.max_retries} failed attempts) — skipping; remove "
            f"'failed' rows from the ledger or wait for a new comment to retry")
        return "skipped:retries"
    if not ledger.retry_ready(repo, "issue", issue_number, key):
        log(f"{repo} issue #{issue_number} at {key} in retry backoff until "
            f"{ledger.next_retry_at(repo, 'issue', issue_number, key)} — skipping")
        return "skipped:backoff"

    try:
        return _process(repo, meta, dry_run)
    except Exception as e:
        if not dry_run:
            # A rate/session limit (or a vendor-diversity shortfall) is
            # infrastructure: defer, don't burn the retry budget. The
            # per-vendor cooldown gates the next attempt.
            status = ("deferred" if isinstance(e, swarm.DEFERRED_ERRORS)
                      else "failed")
            try:
                ledger.append(repo, "issue", issue_number, key,
                              status=status, error=repr(e)[:500])
            except Exception as le:
                log(f"could not record failure in ledger: {le!r}")
        raise


def _process(repo: str, meta: dict, dry_run: bool) -> str:
    issue_number = meta["number"]
    n_comments = len(meta.get("comments", []))
    key = f"comments:{n_comments}"
    snap_dir = issue_dir(repo, issue_number, key)
    snap_dir.mkdir(parents=True, exist_ok=True)

    verdict = security.assess(
        repo, "issue", issue_number,
        security.build_issue_text(meta),
        snap_dir,
    )

    if verdict["decision"] == "block":
        log(f"{repo} issue #{issue_number} BLOCKED by security triage "
            f"(risk={verdict['final_risk']}, score={verdict['final_score']})")
        _record_block(snap_dir, verdict)
        if not dry_run:
            ledger.append(repo, "issue", issue_number, key,
                          status="security_blocked")
            audit.sync(message=f"block {repo} issue #{issue_number} ({key})")
        return "blocked"

    log(f"Triaging {repo} issue #{issue_number} (comments={n_comments})")
    analysis_path = snap_dir / "analysis.md"
    comment_path = snap_dir / "comment.md"

    if analysis_path.is_file() and comment_path.is_file():
        log(f"reusing existing triage at {snap_dir}")
    else:
        _generate_triage(repo, meta, snap_dir)

    # Which agent/vendor produced the security verdict (travels in the
    # cached security.json, so this also works on a reused verdict).
    audit.merge_ran(snap_dir, "security_triage", {
        "agent": verdict["model_verdict"].get("agent"),
        "vendor": verdict["model_verdict"].get("vendor"),
        "fallback": verdict["model_verdict"].get("agent_fallback", False),
    })

    comment_text = comment_path.read_text(encoding="utf-8").strip()
    should_post = bool(comment_text) and comment_text.upper() != "NO_COMMENT"

    if dry_run:
        action = "post clarifier" if should_post else "skip (issue clear)"
        log(f"DRY_RUN — would {action} on {repo} issue #{issue_number}")
        return "dry-run"

    if should_post:
        post_path = comment_path
        if verdict["decision"] == "warn":
            banner_path = snap_dir / "comment.warned.md"
            banner_path.write_text(
                security.warning_banner(verdict)
                + comment_path.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            post_path = banner_path
        url = post_issue_comment(repo, issue_number, post_path)
        (snap_dir / "posted.json").write_text(
            json.dumps({"posted": True, "comment_url": url,
                        "security_decision": verdict["decision"]}, indent=2),
            encoding="utf-8",
        )
        log(f"posted: {url}")
    else:
        log(f"{repo} issue #{issue_number} looks clear; not posting")
        (snap_dir / "posted.json").write_text(
            json.dumps({"posted": False,
                        "security_decision": verdict["decision"]}, indent=2),
            encoding="utf-8",
        )

    # Record the triage agent's vendor on the ledger row (no swarm here, so
    # the "vendor list" is the single triage vendor when one is recorded).
    try:
        ran = json.loads((snap_dir / "metadata.json").read_text(
            encoding="utf-8")).get("ran", {})
        triage = ran.get("issue_triage") or {}
    except (OSError, json.JSONDecodeError):
        triage = {}
    triage_vendor = [triage["vendor"]] if triage.get("vendor") else []
    ledger.append(repo, "issue", issue_number, key, vendors=triage_vendor)
    return "done"
