"""Scan-pass gating: the pass stops only when no role can run at all."""
from __future__ import annotations

import time

from atlas_watcher import cli, cooldown


def _configure(patch_settings, fake_binaries, *, missing: set[str] = frozenset()):
    patch_settings(perspective_agents=["dclaude"], generic_a_agents=["codex"],
                   generic_b_agents=["cline"], manager_agents=["dclaude"],
                   security_triage_agents=["dclaude"],
                   issue_triage_agents=["dclaude"])
    fake_binaries.update({"dclaude", "codex", "cline"} - set(missing))


def test_pass_continues_when_only_some_roles_are_cooling(
        patch_settings, fake_binaries, cooldown_file, caplog):
    # anthropic (dclaude) capped, but codex/cline roles are fine: the pass
    # must NOT stop — issues (security-triage + issue-triage) can still run
    # on a healthy vendor, and per-item skipping handles the PRs.
    _configure(patch_settings, fake_binaries)
    cooldown.record("anthropic", cooldown.LimitInfo("usage limit reached",
                                                    time.time() + 3600))
    assert cli._blocked_pass() is False


def test_pass_stops_only_when_every_role_is_cooling(
        patch_settings, fake_binaries, cooldown_file):
    _configure(patch_settings, fake_binaries)
    for vendor in ("anthropic", "openai", "cline"):
        cooldown.record(vendor, cooldown.LimitInfo("usage limit reached",
                                                   time.time() + 3600))
    assert cli._blocked_pass() is True


def test_pass_does_not_stop_on_config_errors(
        patch_settings, fake_binaries, cooldown_file):
    # Every role blocked by missing binaries: not a cooldown — the items
    # must fail loudly through the normal path, not stall silently.
    _configure(patch_settings, fake_binaries,
               missing={"dclaude", "codex", "cline"})
    assert cli._blocked_pass() is False


def test_pass_does_not_stop_when_nothing_is_cooling(
        patch_settings, fake_binaries, cooldown_file):
    _configure(patch_settings, fake_binaries)
    assert cli._blocked_pass() is False


def test_item_skipped_when_one_of_its_roles_is_cooling(
        patch_settings, fake_binaries, cooldown_file):
    _configure(patch_settings, fake_binaries)
    cooldown.record("openai", cooldown.LimitInfo("quota exceeded",
                                                 time.time() + 1800))
    # generic-a runs on codex (openai) → PR roles are blocked → skip
    assert cli._skip_item("o/r", "PR #1", cli.agents.PR_ROLES) is True
    # issue roles don't include the cooling role → not skipped
    assert cli._skip_item("o/r", "issue #2", cli.agents.ISSUE_ROLES) is False


def test_item_not_skipped_for_a_missing_binary(
        patch_settings, fake_binaries, cooldown_file):
    # A missing binary is a config error, not self-healing: the item must
    # run and fail loudly, so the misconfiguration surfaces in the ledger.
    _configure(patch_settings, fake_binaries, missing={"cline"})
    assert cli._skip_item("o/r", "PR #1", cli.agents.PR_ROLES) is False


def test_exempt_repo_item_not_held_back_by_cooling_triage(
        patch_settings, fake_binaries, cooldown_file):
    # Only security triage's vendor is cooling: an exempt repo never runs
    # triage, so its items must not be skipped; other repos' items are.
    _configure(patch_settings, fake_binaries)
    patch_settings(generic_b_agents=["codex"],
                   security_triage_agents=["cline"],
                   security_exempt_repos=["myorg/*"])
    cooldown.record("cline", cooldown.LimitInfo("usage limit reached",
                                                 time.time() + 3600))
    from atlas_watcher import agents
    assert cli._skip_item("myorg/r", "PR #1",
                          cli._item_roles("myorg/r", agents.PR_ROLES)) is False
    assert cli._skip_item("other/r", "PR #1",
                          cli._item_roles("other/r", agents.PR_ROLES)) is True
