"""Agent registry: vendors, commands, preflight, and candidate picking."""
from __future__ import annotations

import time

import pytest

from atlas_watcher import agents, cooldown
from atlas_watcher.config import AgentCommand
from conftest import GROK


@pytest.fixture()
def fake_binaries(monkeypatch):
    """Declare which fake agent names have a binary present. Replaces the
    real (cached) probe entirely, so the test never shells out."""
    present: set[str] = set()
    monkeypatch.setattr(agents, "binary_available",
                        lambda agent: agent in present)
    return present


def test_vendor_of_builtins_and_overrides(patch_settings):
    patch_settings(agent_commands={
        "opencode-grok": AgentCommand(
            command="opencode run --model openrouter/x-ai/grok-4.7 --auto",
            vendor="xai"),
        "proxy-claude": AgentCommand(command="proxy --model claude"),
    })
    assert agents.vendor_of("dclaude") == "anthropic"
    assert agents.vendor_of("codex") == "openai"
    assert agents.vendor_of("opencode") == "openrouter"
    assert agents.vendor_of("opencode-grok") == "xai"
    # No vendor statement -> the agent name is its own account
    assert agents.vendor_of("proxy-claude") == "proxy-claude"


def test_command_of_prefers_agent_commands(patch_settings):
    patch_settings(agent_commands={"dclaude": AgentCommand(command="fake claude")})
    assert agents.command_of("dclaude") == "fake claude"
    assert agents.command_of("codex") == "codex exec"


def test_command_of_unknown_agent_raises(patch_settings):
    patch_settings()
    with pytest.raises(ValueError, match="unknown agent"):
        agents.command_of("nope")


def test_role_candidates_come_from_settings(patch_settings):
    patch_settings(perspective_agents=["dclaude", "opencode-grok"])
    assert agents.role_candidates("perspective") == ["dclaude", "opencode-grok"]


def test_pick_takes_the_first_available_in_order(
        patch_settings, fake_binaries, cooldown_file):
    fake_binaries.update({"dclaude", "opencode-grok"})
    agent, reason = agents.pick(["dclaude", "opencode-grok"])
    assert (agent, reason) == ("dclaude", None)


def test_pick_skips_a_missing_binary(patch_settings, fake_binaries):
    fake_binaries.add("opencode-grok")  # dclaude "not installed"
    agent, reason = agents.pick(["dclaude", "opencode-grok"])
    assert (agent, reason) == ("opencode-grok", None)


def test_pick_skips_a_cooling_vendor(patch_settings, fake_binaries,
                                     cooldown_file):
    fake_binaries.update({"dclaude", "opencode-grok"})
    cooldown.record("anthropic", cooldown.LimitInfo("usage limit reached", None))
    agent, reason = agents.pick(["dclaude", "opencode-grok"])
    assert (agent, reason) == ("opencode-grok", None)


def test_pick_all_cooling_reports_cooldown(patch_settings, fake_binaries,
                                           cooldown_file):
    patch_settings(agent_commands={"opencode-grok": GROK})
    fake_binaries.update({"dclaude", "opencode-grok"})
    cooldown.record("anthropic", cooldown.LimitInfo("usage limit reached", None))
    cooldown.record("xai", cooldown.LimitInfo("quota exceeded", None))
    agent, reason = agents.pick(["dclaude", "opencode-grok"])
    assert agent is None and reason == "cooldown"


def test_pick_all_missing_reports_missing(patch_settings, fake_binaries):
    agent, reason = agents.pick(["dclaude", "opencode-grok"])
    assert agent is None and reason == "missing"


def test_pick_mixed_cooling_and_missing_prefers_cooldown(
        patch_settings, fake_binaries):
    # dclaude cooling (self-healing), grok's binary gone (config error):
    # the role CAN come back when the cooldown lifts.
    fake_binaries.add("dclaude")
    cooldown.record("anthropic", cooldown.LimitInfo("usage limit reached", None))
    agent, reason = agents.pick(["dclaude", "opencode-grok"])
    assert agent is None and reason == "cooldown"


def test_pick_excludes_already_failed_agents(patch_settings, fake_binaries):
    fake_binaries.update({"dclaude", "opencode-grok"})
    agent, _ = agents.pick(["dclaude", "opencode-grok"],
                           exclude_agents={"dclaude"})
    assert agent == "opencode-grok"


def test_pick_failover_prefers_a_new_vendor(patch_settings, fake_binaries):
    # proxy-claude drives the same anthropic account as dclaude -> same
    # vendor, so failing over to it does not grow diversity; grok does.
    patch_settings(agent_commands={
        "proxy-claude": AgentCommand(command="proxy --model x",
                                     vendor="anthropic"),
    })
    fake_binaries.update({"dclaude", "proxy-claude", "opencode-grok"})
    agent, _ = agents.pick(
        ["dclaude", "proxy-claude", "opencode-grok"],
        exclude_agents={"dclaude"}, seen_vendors={"anthropic"})
    assert agent == "opencode-grok"


def test_pick_first_choice_never_reorders_for_diversity(
        patch_settings, fake_binaries):
    fake_binaries.update({"dclaude", "opencode-grok"})
    # Even with both vendors already represented, the operator's first
    # choice wins on the initial pick; only failover reorders.
    agent, _ = agents.pick(["dclaude", "opencode-grok"],
                           seen_vendors={"anthropic", "xai"})
    assert agent == "dclaude"


def test_block_reason_classifies_the_block(patch_settings, fake_binaries,
                                           cooldown_file):
    patch_settings(perspective_agents=["dclaude"],
                   issue_triage_agents=["dclaude"],
                   security_triage_agents=["dclaude"],
                   generic_a_agents=["codex"], generic_b_agents=["cline"],
                   manager_agents=["dclaude"])
    fake_binaries.update({"dclaude", "codex", "cline"})
    assert agents.block_reason(agents.ALL_ROLES) is None
    cooldown.record("anthropic", cooldown.LimitInfo("usage limit reached", None))
    assert agents.block_reason(agents.ALL_ROLES) == "cooldown"
    # A role blocked purely by a missing binary is a config error, and wins
    # only when nothing is self-healing.
    fake_binaries.discard("cline")
    cooldown.clear()
    assert agents.block_reason(agents.ALL_ROLES) == "missing"


def test_earliest_reset_spans_the_blocked_roles(patch_settings, cooldown_file):
    patch_settings(perspective_agents=["dclaude"], generic_a_agents=["codex"],
                   generic_b_agents=["cline"], manager_agents=["dclaude"],
                   security_triage_agents=["dclaude"],
                   issue_triage_agents=["dclaude"])
    cooldown.record("openai", cooldown.LimitInfo("quota exceeded",
                                                 time.time() + 300))
    soonest = agents.earliest_reset(agents.ALL_ROLES)
    assert abs(soonest - (time.time() + 300)) < 5
