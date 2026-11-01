"""Shared fixtures: settings overrides and a tmp cooldown file."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas_watcher.config import AgentCommand, Settings, settings

# The standard extra-agent registration used across tests: grok via the
# existing opencode CLI on OpenRouter, on xAI's account.
GROK = AgentCommand(
    command="opencode run --model openrouter/x-ai/grok-4.7 --auto",
    vendor="xai",
)


@pytest.fixture(autouse=True)
def cooldown_file(tmp_path, monkeypatch):
    """Redirect the cooldown state file into the test's tmp dir (autouse, so
    no test ever reads or writes the developer's real cooldown state)."""
    from atlas_watcher import cooldown

    path = tmp_path / "logs" / "cooldown.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cooldown, "cooldown_file", lambda: path)
    return path


@pytest.fixture()
def fake_binaries(monkeypatch):
    """Declare which agent names have a launchable binary. Replaces the
    real (cached) ``agents.binary_available`` probe entirely, so a test
    never shells out and never sees the developer's installed CLIs."""
    from atlas_watcher import agents

    present: set[str] = set()
    monkeypatch.setattr(agents, "binary_available",
                        lambda agent: agent in present)
    return present


@pytest.fixture()
def patch_settings(monkeypatch):
    """Patch attributes onto the settings singleton for one test.

    Values start from the class defaults (config.env is not loaded), so
    tests don't depend on the operator's config. Agent-command dicts given
    as plain str values are normalized to AgentCommand objects.
    """
    defaults = Settings(_env_file=None)

    def apply(**overrides) -> Settings:
        current = {
            name: getattr(settings, name) for name in type(settings).model_fields
        }
        current.update(overrides)

        def as_command(val):
            if isinstance(val, AgentCommand):
                return val
            if isinstance(val, dict):
                return AgentCommand(**val)
            return AgentCommand(command=val)

        current["agent_commands"] = {
            name: as_command(val)
            for name, val in current.get("agent_commands", {}).items()
        }
        for name, value in current.items():
            monkeypatch.setattr(settings, name, value, raising=True)
        return settings

    # Clear any leftover patched state between tests by restoring class
    # defaults first, so a test that forgets to override a field still sees
    # defaults rather than the operator's config.env values.
    for name in type(settings).model_fields:
        monkeypatch.setattr(settings, name, getattr(defaults, name),
                            raising=True)
    return apply


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
