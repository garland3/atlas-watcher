"""Settings loaded from config.env (and optional config.local.env override)."""
from __future__ import annotations

from typing import Annotated

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from .paths import ROOT


# Built-in agent name -> Settings field holding its CLI command. AGENT_COMMANDS
# can override any of these or add new names, all from config.
AGENT_COMMAND_FIELDS: dict[str, str] = {
    "dclaude": "dclaude_cmd",
    "codex": "codex_cmd",
    "cline": "cline_cmd",
    "opencode": "opencode_cmd",
}


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

    # Which CLI runs the manager (the orchestrator that reads the perspective
    # reports and writes the final summary). Use any built-in below, or a name
    # registered in AGENT_COMMANDS. Changing this is all it takes to switch
    # the orchestrator between claude/codex/cline/opencode/anything else.
    manager_agent: str = "dclaude"

    dclaude_cmd: str = "claude --dangerously-skip-permissions --print"
    codex_cmd: str = "codex exec"
    # cline reads the prompt from stdin; it has no --print flag (that was a
    # copy of the dclaude invocation and made every generic-b agent exit 1).
    cline_cmd: str = "cline --auto-approve true"
    # OpenCode reads the prompt from stdin. The model id is provider/model,
    # so OpenRouter's Auto Router is "openrouter/openrouter/auto".
    opencode_cmd: str = "opencode run --model openrouter/openrouter/auto --auto"

    # Extra agent commands as a JSON object, e.g.
    #   AGENT_COMMANDS={"gemini": "gemini --approval-mode yolo"}
    # Keys may also shadow a built-in, so the same CLI can be repointed at a
    # different binary/flags without touching the code.
    agent_commands: dict[str, str] = {}

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

    # Agent rate/session limits are account-wide, not per item. When an agent
    # reports one the watcher sets a global cooldown and skips whole passes
    # until it expires, instead of launching swarms that are certain to fail.
    # The reset time named by the agent is honored when it parses; otherwise
    # the pause backs off exponentially (base * 2^(consecutive-1)), capped.
    # Because cron may fire every 15 min, these are the only backstop against a
    # tight retry loop while the account is capped.
    agent_limit_backoff_secs: int = 3600
    agent_limit_backoff_max_secs: int = 21_600
    # Ceiling on a reset time the agent *stated* (and we parsed). Longer than
    # the backoff cap so a weekly limit is waited out in one pause instead of
    # relaunching a doomed swarm every few hours.
    agent_limit_reset_max_secs: int = 691_200

    # Per-item retry backoff. An item that failed is not retried until
    # base * 2^(failures-1) seconds have elapsed, capped. Failure kinds that
    # are not the item's fault (a rate limit) are recorded as "deferred" and
    # do not consume this budget at all.
    retry_backoff_base_secs: int = 900
    retry_backoff_max_secs: int = 86_400

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

    @model_validator(mode="after")
    def _check_manager_agent(self):
        known = set(AGENT_COMMAND_FIELDS) | set(self.agent_commands)
        if self.manager_agent not in known:
            raise ValueError(
                f"MANAGER_AGENT={self.manager_agent!r} is not a known agent; "
                f"use one of {', '.join(sorted(known))} or add it to "
                f"AGENT_COMMANDS"
            )
        return self

    @property
    def default_repo(self) -> str:
        if not self.repos:
            raise RuntimeError("no repos configured; set REPOS in config.env")
        return self.repos[0]


settings = Settings()
