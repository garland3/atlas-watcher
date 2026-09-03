"""Settings loaded from config.env (and optional config.local.env override)."""
from __future__ import annotations

from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from .paths import ROOT


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(ROOT / "config.env", ROOT / "config.local.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Comma-separated list of "owner/name" repos to watch. The first entry is
    # the "default repo" used by single-repo CLI commands when --repo is omitted.
    repos: Annotated[list[str], NoDecode] = ["sandialabs/atlas-ui-3"]

    audit_repo: str = "garland3/ATLAS-WATCHER-LOGS"

    # Filesystem root of the durable audit record (reviews/ + ledger/). This is
    # a SEPARATE git repo from the codebase, so the code can be open-sourced
    # without publishing third-party diffs or raw agent transcripts. Relative
    # paths resolve against the code repo root; empty means "<code root>/
    # audit-data". Bootstrap it with `atlas-watcher audit-init`.
    audit_dir: str = ""
    bot_name: str = "AGENT-REVIEW-BOT-3"

    dclaude_cmd: str = "claude --dangerously-skip-permissions --print"
    codex_cmd: str = "codex exec"
    # cline reads the prompt from stdin; it has no --print flag (that was a
    # copy of the dclaude invocation and made every generic-b agent exit 1).
    cline_cmd: str = "cline --auto-approve true"

    # Agents authenticate through their own CLI login. An ANTHROPIC_API_KEY in
    # the inherited environment silently takes precedence over that login, so
    # a stale key 401s every agent. We unset it in each runner by default; set
    # this true only if you deliberately drive the agents by API key.
    agent_inherit_api_key: bool = False

    agent_timeout_secs: int = 1800
    poll_interval_secs: int = 900  # 15 min by default — matches the cron cadence

    # Max attempts per (repo, kind, number, key) before the watcher gives up.
    # Each failure is appended to the ledger as a "failed" row; once the count
    # hits this cap the item is skipped until the key changes (new PR commit /
    # new issue comment) or the "failed" rows are removed from the ledger.
    max_retries: int = 3

    # Cap the diff size we feed agents so prompts stay tractable.
    diff_context_max_bytes: int = 200_000

    # Security triage thresholds. Risk levels are ordered low < medium < high < critical.
    # If the verdict is at or above security_block_above the swarm/triage is skipped
    # and no upstream comment is posted. If at or above security_warn_above, the
    # comment is posted with a neutral "this content tripped a content-safety check"
    # banner prepended. Set to "critical" to disable the warn/block behavior
    # respectively; set both to "low" for maximum sensitivity.
    security_block_above: str = "high"
    security_warn_above: str = "medium"
    security_timeout_secs: int = 600
    security_text_max_bytes: int = 100_000

    # Require corroboration before a *block*. With this on, a lone ambiguous
    # heuristic (one quoted jailbreak phrase, a stray zero-width char) is
    # capped at "warn" — the review still runs, with the banner — unless the
    # heuristic is decisive (bidi overrides, repeated prose directives), a
    # second heuristic family agrees, or the model independently reaches the
    # warn tier. Set false to restore the old "any single signal blocks"
    # behaviour.
    security_require_corroboration: bool = True

    @field_validator("repos", mode="before")
    @classmethod
    def _parse_repos(cls, v):
        if isinstance(v, str):
            return [r.strip() for r in v.split(",") if r.strip()]
        return v

    @property
    def default_repo(self) -> str:
        if not self.repos:
            raise RuntimeError("no repos configured; set REPOS in config.env")
        return self.repos[0]


settings = Settings()
