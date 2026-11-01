"""Settings: role fallback lists, AGENT_COMMANDS forms, validation."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from atlas_watcher.config import Settings


def _settings(**env) -> Settings:
    return Settings(_env_file=None, _env_file_encoding="utf-8", **env)


def test_role_lists_default_to_the_legacy_single_agents():
    s = _settings()
    assert s.perspective_agents == ["dclaude"]
    assert s.generic_a_agents == ["codex"]
    assert s.generic_b_agents == ["cline"]
    assert s.security_triage_agents == ["dclaude"]
    assert s.issue_triage_agents == ["dclaude"]
    # manager_agents empty -> falls back to the legacy MANAGER_AGENT
    assert s.manager_agents == ["dclaude"]


def test_manager_agents_supersedes_manager_agent():
    s = _settings(manager_agent="codex", manager_agents="dclaude,opencode")
    assert s.manager_agents == ["dclaude", "opencode"]


def test_comma_separated_lists_are_parsed():
    s = _settings(
        agent_commands={
            "opencode-grok": {
                "command": "opencode run --model openrouter/x-ai/grok-4.7 --auto",
                "vendor": "xai",
            },
        },
        perspective_agents="dclaude, opencode-grok",
        generic_a_agents="codex,opencode-grok",
    )
    assert s.perspective_agents == ["dclaude", "opencode-grok"]
    assert s.generic_a_agents == ["codex", "opencode-grok"]


def test_agent_commands_string_and_object_forms():
    s = _settings(agent_commands={
        "gemini": "gemini --approval-mode yolo",
        "opencode-grok": {
            "command": "opencode run --model openrouter/x-ai/grok-4.7 --auto",
            "vendor": "xai",
        },
    })
    assert s.agent_commands["gemini"].command == "gemini --approval-mode yolo"
    assert s.agent_commands["gemini"].vendor is None
    grok = s.agent_commands["opencode-grok"]
    assert "openrouter/x-ai/grok-4.7" in grok.command
    assert grok.vendor == "xai"


def test_unknown_agent_in_a_role_list_is_rejected():
    with pytest.raises(ValidationError, match="PERSPECTIVE_AGENTS"):
        _settings(perspective_agents="dclaude,not-a-cli")


def test_min_swarm_vendors_must_be_positive():
    with pytest.raises(ValidationError, match="MIN_SWARM_VENDORS"):
        _settings(min_swarm_vendors=0)
