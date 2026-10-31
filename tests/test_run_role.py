"""run_role failover against real (fake) agent binaries.

The fakes are real executables on PATH-less absolute paths, launched through
the same ``bash -lc`` path production uses — no launch-path mocking.
"""
from __future__ import annotations

import stat
from pathlib import Path

import pytest

from atlas_watcher import cooldown, swarm


def _write_agent(path: Path, body: str) -> str:
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.fixture()
def agents_env(tmp_path, patch_settings):
    """Fake agents bound to every role, so no test can launch a real CLI.

    limited: hits a rate limit (anthropic). working: succeeds (openai).
    crashy: ordinary failure (openai).
    """
    limited = _write_agent(tmp_path / "limited.sh",
                           "cat >/dev/null\n"
                           "echo 'usage limit reached' >&2\nexit 1\n")
    working = _write_agent(tmp_path / "working.sh",
                           "cat >/dev/null\necho 'VERDICT OK'\nexit 0\n")
    crashy = _write_agent(tmp_path / "crashy.sh",
                          "cat >/dev/null\necho 'boom' >&2\nexit 1\n")
    patch_settings(
        agent_commands={
            "limited": {"command": limited, "vendor": "anthropic"},
            "working": {"command": working, "vendor": "openai"},
            "crashy": {"command": crashy, "vendor": "openai"},
        },
        perspective_agents=["limited", "working"],
        generic_a_agents=["limited", "working"],
        generic_b_agents=["limited", "working"],
        manager_agents=["limited", "working"],
        security_triage_agents=["limited", "working"],
        issue_triage_agents=["limited", "working"],
    )
    return tmp_path


def test_fails_over_a_limit_to_the_next_candidate(agents_env, cooldown_file):
    result = swarm.run_role("security-triage", "prompt text",
                            out_file=agents_env / "out.txt",
                            log_file=agents_env / "triage.log")
    assert result.agent == "working"
    assert result.vendor == "openai"
    assert (agents_env / "out.txt").read_text().strip() == "VERDICT OK"
    # the limit was recorded for the limited agent's vendor only
    assert cooldown.active_for("anthropic") is not None
    assert cooldown.active_for("openai") is None
    # both attempts kept their own transcripts
    assert (agents_env / "triage.limited.log").is_file()
    assert (agents_env / "triage.working.log").is_file()


def test_skips_a_missing_binary_before_launch(agents_env, patch_settings,
                                              cooldown_file):
    patch_settings(security_triage_agents=["absent", "working"])
    result = swarm.run_role("security-triage", "prompt",
                            out_file=agents_env / "out.txt",
                            log_file=agents_env / "triage.log")
    assert result.agent == "working"
    # The missing binary produced no runner attempt at all.
    assert not (agents_env / "triage.absent.log").exists()


def test_ordinary_failure_does_not_fail_over(agents_env, patch_settings):
    patch_settings(security_triage_agents=["crashy", "working"])
    with pytest.raises(RuntimeError, match="rc=1"):
        swarm.run_role("security-triage", "prompt",
                       out_file=agents_env / "out.txt",
                       log_file=agents_env / "triage.log")
    assert cooldown.active_for("openai") is None


def test_all_candidates_cooling_raises_limit_error(agents_env, patch_settings,
                                                   cooldown_file):
    patch_settings(security_triage_agents=["limited", "limited2"])
    limited2 = _write_agent(agents_env / "limited2.sh",
                            "cat >/dev/null\necho 'quota exceeded' >&2\nexit 1\n")
    patch_settings(agent_commands={
        "limited": {"command": str(agents_env / "limited.sh"),
                    "vendor": "anthropic"},
        "limited2": {"command": limited2, "vendor": "openai"},
    })
    with pytest.raises(swarm.AgentLimitError):
        swarm.run_role("security-triage", "prompt",
                       out_file=agents_env / "out.txt",
                       log_file=agents_env / "triage.log")
    assert cooldown.active_for("anthropic") is not None
    assert cooldown.active_for("openai") is not None


def test_unsetting_the_harness_api_key(agents_env, patch_settings,
                                       monkeypatch):
    # Rule 13: the launch path must not let a stale ANTHROPIC_API_KEY reach
    # the agent. The fake agent reports what it saw.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-stale")
    probe = _write_agent(agents_env / "probe.sh",
                         "cat >/dev/null\n"
                         'if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then '
                         "echo key-leaked; else echo key-clean; fi\nexit 0\n")
    patch_settings(agent_commands={"probe": {"command": probe,
                                             "vendor": "xai"}},
                   security_triage_agents=["probe"])
    result = swarm.run_role("security-triage", "prompt",
                            out_file=agents_env / "out.txt",
                            log_file=agents_env / "triage.log")
    assert result.agent == "probe"
    assert "key-clean" in (agents_env / "out.txt").read_text()
