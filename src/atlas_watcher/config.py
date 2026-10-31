"""Settings loaded from config.env (and optional config.local.env override)."""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, field_validator, model_validator
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

# Built-in agent name -> the vendor whose account/limits it runs on. Used to
# key per-agent cooldowns (a limit on one vendor pauses only that vendor) and
# to count MIN_SWARM_VENDORS diversity. An agent registered in AGENT_COMMANDS
# defaults to its own name as vendor unless the entry declares one.
AGENT_VENDORS: dict[str, str] = {
    "dclaude": "anthropic",
    "codex": "openai",
    "cline": "cline",
    "opencode": "openrouter",
}


class AgentCommand(BaseModel):
    """One AGENT_COMMANDS entry: the CLI command plus optional vendor.

    A bare string in AGENT_COMMANDS is normalized to ``{"command": ...}``.
    ``vendor`` overrides the default vendor for cooldown keying and diversity
    counting — set it when a custom command drives a known vendor's account.
    """
    command: str
    vendor: str | None = None


def _parse_agent_commands(v) -> dict:
    """Accept ``{"name": "cmd"}`` and ``{"name": {"command": ..., "vendor": ...}}``."""
    if isinstance(v, dict):
        return {
            name: ({"command": val} if isinstance(val, str) else val)
            for name, val in v.items()
        }
    return v


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
    # Superseded by manager_agents below; kept so old configs keep working.
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
    # different binary/flags without touching the code. A value may also be an
    # object declaring the vendor whose account the command drives:
    #   AGENT_COMMANDS={"opencode-grok": {"command":
    #       "opencode run --model openrouter/x-ai/grok-4.7 --auto",
    #       "vendor": "xai"}}
    # The vendor keys per-agent cooldowns and MIN_SWARM_VENDORS counting.
    agent_commands: dict[str, AgentCommand] = {}

    # --- role fallback lists -------------------------------------------------
    # Each role takes an ORDERED list of agents (built-ins or AGENT_COMMANDS
    # keys). The first candidate that is usable (binary present, vendor not
    # cooling down) runs the role; on an infrastructure failure — an agent
    # rate/session limit, an auth error, or a missing binary — the role fails
    # over to the next candidate. Ordinary failures do NOT fail over: a PR
    # that breaks one model must not burn every other vendor's quota in turn.
    perspective_agents: Annotated[list[str], NoDecode] = ["dclaude"]
    generic_a_agents: Annotated[list[str], NoDecode] = ["codex"]
    generic_b_agents: Annotated[list[str], NoDecode] = ["cline"]
    manager_agents: Annotated[list[str], NoDecode] = []  # empty -> [manager_agent]
    security_triage_agents: Annotated[list[str], NoDecode] = ["dclaude"]
    issue_triage_agents: Annotated[list[str], NoDecode] = ["dclaude"]

    # A review must draw on findings from at least this many distinct vendors
    # before the manager synthesizes it. A swarm that cannot reach the
    # minimum (every non-primary candidate is capped/absent) is deferred
    # instead of posting a single-vendor review. Agents that produced only a
    # stub don't count; the manager never counts (it summarizes, not reviews).
    min_swarm_vendors: int = 2

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

    @field_validator("agent_commands", mode="before")
    @classmethod
    def _parse_agent_commands_values(cls, v):
        return _parse_agent_commands(v)

    @field_validator(
        "perspective_agents", "generic_a_agents", "generic_b_agents",
        "manager_agents", "security_triage_agents", "issue_triage_agents",
        mode="before",
    )
    @classmethod
    def _parse_agent_lists(cls, v):
        if isinstance(v, str):
            return [a.strip() for a in v.split(",") if a.strip()]
        return v

    @model_validator(mode="after")
    def _check_agents(self):
        known = set(AGENT_COMMAND_FIELDS) | set(self.agent_commands)
        if self.manager_agent not in known:
            raise ValueError(
                f"MANAGER_AGENT={self.manager_agent!r} is not a known agent; "
                f"use one of {', '.join(sorted(known))} or add it to "
                f"AGENT_COMMANDS"
            )
        # MANAGER_AGENTS supersedes MANAGER_AGENT; an empty list means "use
        # the old single setting" so existing configs keep working.
        if not self.manager_agents:
            self.manager_agents = [self.manager_agent]
        lists = {
            "PERSPECTIVE_AGENTS": self.perspective_agents,
            "GENERIC_A_AGENTS": self.generic_a_agents,
            "GENERIC_B_AGENTS": self.generic_b_agents,
            "MANAGER_AGENTS": self.manager_agents,
            "SECURITY_TRIAGE_AGENTS": self.security_triage_agents,
            "ISSUE_TRIAGE_AGENTS": self.issue_triage_agents,
        }
        for name, agents in lists.items():
            unknown = [a for a in agents if a not in known]
            if unknown:
                raise ValueError(
                    f"{name} names unknown agent(s) {unknown}; use one of "
                    f"{', '.join(sorted(known))} or add them to AGENT_COMMANDS"
                )
        if self.min_swarm_vendors < 1:
            raise ValueError("MIN_SWARM_VENDORS must be >= 1")
        # The swarm can only ever cover the vendors named by its role lists:
        # no run can reach MIN_SWARM_VENDORS distinct vendors if the union of
        # those lists' vendors is smaller. Reject that at load time rather
        # than deferring every item with a misleading "wait for capped
        # vendors" message — the lists are static, so waiting cannot fix
        # them. (Binary availability is checked later, per pass: a missing
        # CLI is a failover/live problem, not a config problem.)
        swarm_vendors: set[str] = set()
        for agent in (*self.perspective_agents, *self.generic_a_agents,
                      *self.generic_b_agents):
            spec = self.agent_commands.get(agent)
            if spec and spec.vendor:
                swarm_vendors.add(spec.vendor)
            elif agent in AGENT_VENDORS:
                swarm_vendors.add(AGENT_VENDORS[agent])
            else:
                # An agent registered in AGENT_COMMANDS without a vendor
                # statement is its own account (see agents.vendor_of).
                swarm_vendors.add(agent)
        if len(swarm_vendors) < self.min_swarm_vendors:
            raise ValueError(
                f"swarm role lists cover {len(swarm_vendors)} vendor(s) "
                f"{sorted(swarm_vendors)} but MIN_SWARM_VENDORS="
                f"{self.min_swarm_vendors}; add candidates on more vendors "
                f"to PERSPECTIVE_AGENTS/GENERIC_A_AGENTS/GENERIC_B_AGENTS or "
                f"lower MIN_SWARM_VENDORS"
            )
        return self

    @property
    def default_repo(self) -> str:
        if not self.repos:
            raise RuntimeError("no repos configured; set REPOS in config.env")
        return self.repos[0]


settings = Settings()
