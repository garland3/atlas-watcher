"""Thin wrappers around the gh CLI. Every call takes an explicit repo."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Literal


def _run(args: list[str]) -> str:
    return subprocess.run(
        args, check=True, capture_output=True, text=True
    ).stdout


def list_open_prs(repo: str) -> list[int]:
    out = _run([
        "gh", "pr", "list",
        "--repo", repo,
        "--state", "open",
        "--limit", "200",
        "--json", "number",
        "--jq", ".[].number",
    ])
    return [int(line) for line in out.splitlines() if line.strip()]


def list_open_issues(repo: str) -> list[int]:
    out = _run([
        "gh", "issue", "list",
        "--repo", repo,
        "--state", "open",
        "--search", "is:issue",
        "--limit", "200",
        "--json", "number",
        "--jq", ".[].number",
    ])
    return [int(line) for line in out.splitlines() if line.strip()]


def view_pr(repo: str, number: int) -> dict[str, Any]:
    fields = (
        "number,title,body,author,headRefOid,headRefName,baseRefName,"
        "state,isDraft,url,additions,deletions,changedFiles,comments"
    )
    return json.loads(_run([
        "gh", "pr", "view", str(number),
        "--repo", repo,
        "--json", fields,
    ]))


def view_issue(repo: str, number: int) -> dict[str, Any]:
    fields = "number,title,body,author,labels,state,url,comments,createdAt,updatedAt"
    return json.loads(_run([
        "gh", "issue", "view", str(number),
        "--repo", repo,
        "--json", fields,
    ]))


def post_comment(repo: str, kind: Literal["pr", "issue"],
                 number: int, body_file: Path) -> str:
    cmd = "pr" if kind == "pr" else "issue"
    return _run([
        "gh", cmd, "comment", str(number),
        "--repo", repo,
        "--body-file", str(body_file),
    ]).strip()
