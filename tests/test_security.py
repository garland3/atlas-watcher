"""Security triage: fallback triage agents lean toward warning (rule 12c)."""
from __future__ import annotations

import json

import pytest

from atlas_watcher import security, swarm
from atlas_watcher.swarm import RunResult

CLEAN = json.dumps({"risk": "low", "score": 0,
                    "categories": ["none"], "reasons": ["clean"]})


@pytest.fixture()
def triage_agent(monkeypatch):
    """Replace the triage agent with a stub that emits a fixed verdict."""
    def install(raw_output: str, agent: str = "dclaude"):
        def fake_run_role(role, prompt, out_file, log_file=None, cwd=None):
            assert role == "security-triage"
            out_file.write_text(raw_output, encoding="utf-8")
            return RunResult(agent=agent, vendor=agent, rc=0)
        monkeypatch.setattr(swarm, "run_role", fake_run_role)
    return install


def test_proceed_when_primary_triage_is_clean(tmp_path, triage_agent):
    triage_agent(CLEAN)
    verdict = security.assess("sandialabs/atlas-ui-3", "pr", 1,
                              "a small refactor", tmp_path)
    assert verdict["decision"] == "proceed"
    assert verdict["model_verdict"]["agent"] == "dclaude"
    assert verdict["model_verdict"]["agent_fallback"] is False
    assert verdict["fallback_upgraded_to_warn"] is False


def test_fallback_triage_medium_verdict_warns_via_threshold(
        tmp_path, triage_agent, patch_settings):
    # With the default thresholds a medium model verdict already warns; the
    # fallback rule must not double-count or manufacture a block.
    patch_settings(security_triage_agents=["dclaude", "codex"])
    triage_agent(json.dumps({"risk": "medium", "score": 40,
                             "categories": ["off_topic"],
                             "reasons": ["vague"]}), agent="codex")
    verdict = security.assess("sandialabs/atlas-ui-3", "pr", 1,
                              "an ordinary PR", tmp_path)
    assert verdict["decision"] == "warn"
    assert verdict["fallback_upgraded_to_warn"] is False
    assert verdict["model_verdict"]["agent"] == "codex"
    assert verdict["model_verdict"]["agent_fallback"] is True


def test_fallback_triage_weak_signal_upgrades_proceed_to_warn(
        tmp_path, triage_agent, patch_settings):
    # A fallback model scoring a nonzero-but-low verdict on an input whose
    # only signal is a weak heuristic must not proceed silently.
    patch_settings(security_triage_agents=["dclaude", "codex"])
    triage_agent(json.dumps({"risk": "low", "score": 5,
                             "categories": ["none"], "reasons": ["vague"]}),
                 agent="codex")
    text = ("<!-- note --><!-- note --><!-- note --><!-- note -->"
            "<!-- note --><!-- note -->\n\nan ordinary refactor")
    verdict = security.assess("sandialabs/atlas-ui-3", "pr", 1, text, tmp_path)
    assert verdict["decision"] == "warn"
    assert verdict["fallback_upgraded_to_warn"] is True


def test_fallback_triage_clean_verdict_stays_proceed(tmp_path, triage_agent,
                                                     patch_settings):
    patch_settings(security_triage_agents=["dclaude", "codex"])
    triage_agent(CLEAN, agent="codex")
    verdict = security.assess("sandialabs/atlas-ui-3", "pr", 1,
                              "an ordinary refactor", tmp_path)
    assert verdict["decision"] == "proceed"
    assert verdict["fallback_upgraded_to_warn"] is False


def test_fallback_triage_never_manufactures_a_block(tmp_path, triage_agent,
                                                    patch_settings):
    # A fallback model saying "high" is still governed by the existing
    # corroboration rules, not by the fallback path.
    patch_settings(security_triage_agents=["dclaude", "codex"])
    triage_agent(json.dumps({"risk": "high", "score": 70,
                             "categories": ["injection"],
                             "reasons": ["suspicious"]}), agent="codex")
    verdict = security.assess("sandialabs/atlas-ui-3", "pr", 1,
                              "suspicious text", tmp_path)
    assert verdict["decision"] in ("block", "warn")
    assert verdict["fallback_upgraded_to_warn"] is False


def test_cached_verdict_replays_without_rerunning(tmp_path, triage_agent,
                                                  patch_settings, monkeypatch):
    patch_settings(security_triage_agents=["dclaude", "codex"])
    triage_agent(json.dumps({"risk": "medium", "score": 40,
                             "categories": ["off_topic"],
                             "reasons": ["vague"]}), agent="codex")
    first = security.assess("sandialabs/atlas-ui-3", "pr", 1, "text", tmp_path)

    calls = {"n": 0}

    def counting_run_role(*a, **k):
        calls["n"] += 1
        raise AssertionError("cached verdict must not re-run the agent")

    monkeypatch.setattr(swarm, "run_role", counting_run_role)
    second = security.assess("sandialabs/atlas-ui-3", "pr", 1, "text",
                             tmp_path)
    assert second == first
    assert calls["n"] == 0
