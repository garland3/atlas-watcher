"""Single funnel for posting comments — applies signature."""
from __future__ import annotations

from pathlib import Path

from . import gh
from .scrub import sign_comment


def _post(repo: str, kind: str, number: int, body_path: Path) -> str:
    body = body_path.read_text(encoding="utf-8")
    signed_path = body_path.with_suffix(body_path.suffix + ".signed")
    signed_path.write_text(sign_comment(body), encoding="utf-8")
    return gh.post_comment(repo, kind, number, signed_path)  # type: ignore[arg-type]


def post_pr_comment(repo: str, number: int, body_path: Path) -> str:
    return _post(repo, "pr", number, body_path)


def post_issue_comment(repo: str, number: int, body_path: Path) -> str:
    return _post(repo, "issue", number, body_path)
