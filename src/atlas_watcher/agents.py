"""Agent registry and role routing.

Every review role (the 7 specialist perspectives, the two generic slots, the
manager, and the two triage roles) is backed by an ORDERED list of candidate
agents. This module resolves a role's list to the agent that should run right
now:

- a candidate whose CLI binary is missing is skipped (preflighted with
  ``command -v`` so a missing CLI fails over *before* launch), and
- a candidate whose vendor is cooling down (per-agent cooldown, see
  :mod:`atlas_watcher.cooldown`) is skipped.

Among the usable candidates the earliest one wins — the list order is the
operator's preference — except during failover, where a candidate on a vendor
not yet represented in the swarm is preferred, so failover grows vendor
diversity toward ``MIN_SWARM_VENDORS`` when it can.
"""
from __future__ import annotations

import functools
import shlex
import subprocess

from . import cooldown
from .config import AGENT_COMMAND_FIELDS, AGENT_VENDORS, settings

# Role name -> Settings field holding its ordered candidate list.
ROLE_FIELDS: dict[str, str] = {
    "perspective": "perspective_agents",
    "generic-a": "generic_a_agents",
    "generic-b": "generic_b_agents",
    "manager": "manager_agents",
    "security-triage": "security_triage_agents",
    "issue-triage": "issue_triage_agents",
}

ALL_ROLES: tuple[str, ...] = tuple(ROLE_FIELDS)
# Roles a PR review needs, in the order they run. Every one must have a
# usable candidate before an item is worth starting.
PR_ROLES: tuple[str, ...] = ("security-triage", "perspective",
                             "generic-a", "generic-b", "manager")
# Roles an issue triage needs.
ISSUE_ROLES: tuple[str, ...] = ("security-triage", "issue-triage")


def known_agents() -> set[str]:
    return set(AGENT_COMMAND_FIELDS) | set(settings.agent_commands)


def role_candidates(role: str) -> list[str]:
    """The ordered fallback list configured for ``role``."""
    return list(getattr(settings, ROLE_FIELDS[role]))


def command_of(agent: str) -> str:
    """Resolve an agent name to its CLI command.

    AGENT_COMMANDS wins over the built-ins, so an operator can repoint an
    existing name or register a new CLI entirely from config.
    """
    spec = settings.agent_commands.get(agent)
    if spec:
        return spec.command
    attr = AGENT_COMMAND_FIELDS.get(agent)
    if attr:
        return getattr(settings, attr)
    known = sorted(known_agents())
    raise ValueError(
        f"unknown agent: {agent!r}; built-ins/config: {', '.join(known)}"
    )


def vendor_of(agent: str) -> str:
    """The vendor whose account/limits this agent runs on.

    A vendor, not an agent name, is what cooldowns key on and what diversity
    counts: two agents on the same account share one limit, and two agents on
    different accounts are the diversity the swarm is after.
    """
    spec = settings.agent_commands.get(agent)
    if spec and spec.vendor:
        return spec.vendor
    if agent in AGENT_VENDORS:
        return AGENT_VENDORS[agent]
    # A CLI registered in AGENT_COMMANDS without a vendor statement is its
    # own account: key cooldowns and diversity on the agent name itself.
    return agent


@functools.lru_cache(maxsize=None)
def binary_available(agent: str) -> bool:
    """Preflight: can this agent's CLI be launched at all?

    Probed through ``bash -lc`` because that is how runners execute agents,
    so the probe sees the same PATH they will. Cached per agent: presence
    does not change within a run. Call ``binary_available.cache_clear()``
    in tests.
    """
    try:
        cmd = command_of(agent).strip()
    except ValueError:
        # Not a known agent at all — treat exactly like a missing binary so
        # a typo'd entry fails over instead of crashing the pass.
        return False
    if not cmd:
        return False
    try:
        bin0 = shlex.split(cmd)[0]
    except ValueError:
        return False
    probe = subprocess.run(
        ["bash", "-lc", f"command -v {shlex.quote(bin0)} >/dev/null 2>&1"],
        capture_output=True, check=False,
    )
    return probe.returncode == 0


def pick(
    candidates: list[str],
    *,
    exclude_agents: frozenset[str] | set[str] = frozenset(),
    seen_vendors: frozenset[str] | set[str] = frozenset(),
) -> tuple[str | None, str | None]:
    """Choose which candidate should run a role right now.

    Returns ``(agent, None)`` on success, or ``(None, reason)`` when every
    candidate is unusable, where reason is:

    - ``"cooldown"`` — at least one candidate has its binary present but its
      vendor is cooling down. The role can become runnable on its own when
      the cooldown expires (self-healing), so callers should defer/skip.
    - ``"missing"`` — no candidate is cooling; the binaries are simply absent
      (or the list names unknown agents). That is a configuration error that
      no amount of waiting fixes, so callers should fail loudly.

    ``exclude_agents`` drops candidates that already failed with an
    infrastructure error during this run (they must not be retried in-loop).
    Its presence marks this as a *failover* pick: then, and only then, a
    candidate on a vendor not yet in ``seen_vendors`` (vendors already
    represented in the swarm) is preferred over an earlier candidate on a
    vendor the swarm already has, so failover grows diversity toward
    MIN_SWARM_VENDORS. The initial pick never reorders: the operator's list
    order is the preference.
    """
    available: list[str] = []
    cooling = False
    for agent in candidates:
        if not binary_available(agent):
            continue
        is_cooling = cooldown.active_for(vendor_of(agent)) is not None
        if is_cooling:
            # Counts toward the block reason even if the agent already
            # failed this run: a limit-exhausted role is still cooling, and
            # cooling is the self-healing way to be blocked.
            cooling = True
        if agent in exclude_agents or is_cooling:
            continue
        available.append(agent)
    if not available:
        return None, ("cooldown" if cooling else "missing")
    if exclude_agents and seen_vendors:
        for agent in available:
            if vendor_of(agent) not in seen_vendors:
                return agent, None
    return available[0], None


def block_reason(roles: tuple[str, ...] | list[str]) -> str | None:
    """``None`` if every role has a usable candidate; else why roles are blocked.

    ``"cooldown"`` when at least one blocked role can heal itself as its
    cooldowns expire (cooldowns dominate a mixed block: the missing binaries
    will still be missing later, and the run will fail loudly then).
    ``"missing"`` only when no blocked role has a cooldown path back — a
    configuration error that no amount of waiting fixes.
    """
    reasons: list[str] = []
    for role in roles:
        _, reason = pick(role_candidates(role))
        if reason is not None:
            reasons.append(reason)
    if not reasons:
        return None
    return "cooldown" if "cooldown" in reasons else "missing"


def earliest_reset(roles: tuple[str, ...] | list[str]) -> float | None:
    """Earliest cooldown reset among the vendors backing ``roles``.

    The moment the first blocked role could run again — when a paused pass
    should be retried.
    """
    vendors: set[str] = set()
    for role in roles:
        vendors.update(vendor_of(a) for a in role_candidates(role))
    return cooldown.earliest_reset(vendors)
