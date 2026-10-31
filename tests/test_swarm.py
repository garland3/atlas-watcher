"""Swarm orchestration: failover rounds, vendor records, diversity gate.

tmux is never launched: ``_launch_round`` and ``_wait_round`` are replaced by
fakes that simulate each round's outcome by writing sentinel files and logs.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from atlas_watcher import cooldown, swarm
from atlas_watcher.paths import pr_dir

REPO = "sandialabs/atlas-ui-3"


@pytest.fixture()
def sha_dir(tmp_path, monkeypatch) -> Path:
    # Patch data_root BEFORE resolving pr_dir, so no test ever touches the
    # real audit repo.
    monkeypatch.setattr("atlas_watcher.paths.data_root",
                        lambda: tmp_path / "audit")
    d = pr_dir(REPO, 1, "deadbeef")
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture()
def fake_binaries(monkeypatch):
    """Declare which agent names have a launchable binary."""
    present: set[str] = set()
    monkeypatch.setattr(swarm.agents, "binary_available",
                        lambda agent: agent in present)
    return present


@pytest.fixture()
def no_tmux(monkeypatch):
    monkeypatch.setattr(swarm, "_launch_round", lambda *a, **k: None)
    monkeypatch.setattr(swarm, "_wait_round", lambda *a, **k: None)


def _plans(sha_dir: Path) -> dict[str, swarm._Plan]:
    return {p.perspective.name: p for p in swarm._plans(sha_dir)}


def _context(sha_dir: Path) -> Path:
    ctx = sha_dir / "context.md"
    if not ctx.is_file():
        ctx.write_text("# PR context\ndummy\n", encoding="utf-8")
    return ctx


def _complete(pl, agent: str) -> None:
    pl.done_path.write_text("0\n", encoding="utf-8")
    pl.findings_path.write_text("## findings\nok\n", encoding="utf-8")


def _limit(pl, agent: str) -> None:
    pl.done_path.write_text("1\n", encoding="utf-8")
    pl.findings_path.unlink(missing_ok=True)
    _, stderr_log = pl.attempt_logs(agent)
    stderr_log.parent.mkdir(parents=True, exist_ok=True)
    stderr_log.write_text("ERROR: usage limit reached\n", encoding="utf-8")


def test_record_vendors_ignores_stubs():
    record = {
        "security": {"agent": "dclaude", "vendor": "anthropic"},
        "generic-a": {"agent": "codex", "vendor": "openai"},
        "generic-b": {"agent": "codex", "vendor": "openai", "stub": True},
    }
    assert swarm.record_vendors(record) == {"anthropic", "openai"}


def test_attempt_log_names_the_agent(tmp_path):
    out = swarm._attempt_log(tmp_path / "manager.log", "dclaude")
    assert out.name == "manager.dclaude.log"
    out = swarm._attempt_log(tmp_path / "manager.log", "opencode-grok")
    assert out.name == "manager.opencode-grok.log"


def test_auth_detection_on_failed_log_tails(tmp_path):
    log = tmp_path / "agent.log"
    log.write_text("ERROR: 401 Unauthorized\n", encoding="utf-8")
    assert swarm._auth_from_logs(log) is True
    log.write_text("Please run /login to continue\n", encoding="utf-8")
    assert swarm._auth_from_logs(log) is True
    log.write_text("some other crash\n", encoding="utf-8")
    assert swarm._auth_from_logs(log) is False
    # Quoted content far from the tail (an echoed prompt citing HTTP 401
    # docs) must not read as an auth error: only the tail is examined.
    log.write_text("+docs say 401 Unauthorized here\n" * 500
                   + "plain crash output\n" * 300, encoding="utf-8")
    assert swarm._auth_from_logs(log) is False


def test_classify_round_stub_ordinary_failures(sha_dir):
    pl = _plans(sha_dir)["security"]
    pl.done_path.write_text("1\n", encoding="utf-8")  # rc=1, no findings
    record, seen, excluded, resolved = {}, set(), set(), set()
    failures = swarm._classify_round([(pl, "dclaude")], record, seen,
                                     excluded, resolved)
    assert failures == 0
    assert pl.findings_path.is_file() and swarm._is_stub(pl.findings_path)
    assert record["security"] == {"agent": "dclaude", "vendor": "anthropic",
                                  "stub": True}
    assert "security" in resolved and "dclaude" not in excluded


def test_classify_round_fails_over_limits(sha_dir, cooldown_file):
    pl = _plans(sha_dir)["security"]
    _limit(pl, "dclaude")
    record, seen, excluded, resolved = {}, set(), set(), set()
    failures = swarm._classify_round([(pl, "dclaude")], record, seen,
                                     excluded, resolved)
    assert failures == 1
    assert "dclaude" in excluded and "security" not in resolved
    assert cooldown.active_for("anthropic") is not None
    assert record == {}  # nothing resolved — the slot waits for its next round


def test_classify_round_records_completions(sha_dir):
    pl = _plans(sha_dir)["generic-a"]
    _complete(pl, "codex")
    record, seen, excluded, resolved = {}, set(), set(), set()
    failures = swarm._classify_round([(pl, "codex")], record, seen, excluded,
                                     resolved)
    assert failures == 0
    assert record["generic-a"] == {"agent": "codex", "vendor": "openai"}
    assert seen == {"openai"}


def test_swarm_completes_and_writes_the_record(
        sha_dir, patch_settings, fake_binaries, no_tmux, monkeypatch):
    patch_settings(perspective_agents=["dclaude"], generic_a_agents=["codex"],
                   generic_b_agents=["cline"], min_swarm_vendors=2)
    fake_binaries.update({"dclaude", "codex", "cline"})
    by_agent = {"security": "dclaude", "correctness": "dclaude",
                "architecture": "dclaude", "testing": "dclaude",
                "performance": "dclaude", "ux": "dclaude",
                "evidence": "dclaude", "generic-a": "codex",
                "generic-b": "cline"}

    def launch(session, launches, checkout, context_file):
        for pl, agent in launches:
            assert agent == by_agent[pl.perspective.name]
            _complete(pl, agent)

    monkeypatch.setattr(swarm, "_launch_round", launch)
    record = swarm.run_swarm(REPO, 1, sha_dir, sha_dir, _context(sha_dir))
    assert record["security"] == {"agent": "dclaude", "vendor": "anthropic"}
    assert record["generic-b"] == {"agent": "cline", "vendor": "cline"}
    saved = json.loads((sha_dir / "agents.json").read_text(encoding="utf-8"))
    assert saved == record


def test_swarm_fails_over_a_limited_agent(
        sha_dir, patch_settings, fake_binaries, no_tmux, monkeypatch,
        cooldown_file):
    # generic-b's primary (cline) hits a limit in round 1; the fallback
    # (dclaude) runs it in round 2. Everything else runs once.
    patch_settings(perspective_agents=["dclaude"], generic_a_agents=["codex"],
                   generic_b_agents=["cline", "dclaude"],
                   min_swarm_vendors=2)
    fake_binaries.update({"dclaude", "codex", "cline"})
    plans = _plans(sha_dir)
    rounds: list[list[str]] = []

    def launch(session, launches, checkout, context_file):
        rounds.append([pl.perspective.name for pl, _ in launches])
        for pl, agent in launches:
            _complete(pl, agent)
        if len(rounds) == 1:
            _limit(plans["generic-b"], "cline")

    monkeypatch.setattr(swarm, "_launch_round", launch)
    record = swarm.run_swarm(REPO, 1, sha_dir, sha_dir, _context(sha_dir))
    assert len(rounds) == 2
    assert "generic-b" in rounds[0] and rounds[1] == ["generic-b"]
    assert record["generic-b"] == {"agent": "dclaude", "vendor": "anthropic"}
    # the limit was recorded against cline's vendor — and only that vendor
    assert cooldown.active_for("cline") is not None
    assert cooldown.active_for("anthropic") is None
    assert cooldown.active_for("openai") is None


def test_swarm_defers_when_every_candidate_is_cooling(
        sha_dir, patch_settings, fake_binaries, no_tmux, monkeypatch,
        cooldown_file):
    patch_settings(perspective_agents=["dclaude"], generic_a_agents=["codex"],
                   generic_b_agents=["cline"], min_swarm_vendors=2)
    fake_binaries.update({"dclaude", "codex", "cline"})
    cooldown.record("cline", cooldown.LimitInfo("usage limit reached",
                                                time.time() + 600))
    with pytest.raises(swarm.AgentLimitError) as excinfo:
        swarm.run_swarm(REPO, 1, sha_dir, sha_dir, _context(sha_dir))
    assert excinfo.value.reset_ts is not None
    # nothing ran: the record (if written) is empty
    saved = (sha_dir / "agents.json")
    assert not saved.exists() or json.loads(saved.read_text()) == {}


def test_swarm_defers_when_diversity_cannot_be_met(
        sha_dir, patch_settings, fake_binaries, no_tmux, monkeypatch,
        cooldown_file):
    # Every role runs on xai (opencode-grok) — a single-vendor review, which
    # must defer rather than post.
    patch_settings(perspective_agents=["opencode-grok"],
                   generic_a_agents=["opencode-grok"],
                   generic_b_agents=["opencode-grok"],
                   min_swarm_vendors=2)
    fake_binaries.add("opencode-grok")

    def launch(session, launches, checkout, context_file):
        for pl, _ in launches:
            _complete(pl, "opencode-grok")

    monkeypatch.setattr(swarm, "_launch_round", launch)
    with pytest.raises(swarm.VendorDiversityError):
        swarm.run_swarm(REPO, 1, sha_dir, sha_dir, _context(sha_dir))


def test_swarm_all_agents_failed_ordinarily_is_a_failure(
        sha_dir, patch_settings, fake_binaries, no_tmux, monkeypatch,
        cooldown_file):
    patch_settings(perspective_agents=["dclaude"], generic_a_agents=["codex"],
                   generic_b_agents=["cline"], min_swarm_vendors=2)
    fake_binaries.update({"dclaude", "codex", "cline"})

    def launch(session, launches, checkout, context_file):
        for pl, _ in launches:
            pl.done_path.write_text("1\n", encoding="utf-8")

    monkeypatch.setattr(swarm, "_launch_round", launch)
    with pytest.raises(RuntimeError, match="failed without findings"):
        swarm.run_swarm(REPO, 1, sha_dir, sha_dir, _context(sha_dir))


def test_swarm_resumes_completed_agents(
        sha_dir, patch_settings, fake_binaries, no_tmux, monkeypatch,
        cooldown_file):
    patch_settings(perspective_agents=["dclaude"], generic_a_agents=["codex"],
                   generic_b_agents=["cline"], min_swarm_vendors=2)
    fake_binaries.update({"dclaude", "codex", "cline"})
    plans = _plans(sha_dir)
    # security perspective already complete from an earlier run
    _complete(plans["security"], "dclaude")
    (sha_dir / "agents.json").write_text(json.dumps(
        {"security": {"agent": "dclaude", "vendor": "anthropic"}}),
        encoding="utf-8")
    launched: list[str] = []

    def launch(session, launches, checkout, context_file):
        launched.extend(pl.perspective.name for pl, _ in launches)
        for pl, _ in launches:
            _complete(pl, "dclaude")

    monkeypatch.setattr(swarm, "_launch_round", launch)
    record = swarm.run_swarm(REPO, 1, sha_dir, sha_dir, _context(sha_dir))
    assert "security" not in launched
    assert len(launched) == 8
    # the resumed entry survived the merge
    assert record["security"] == {"agent": "dclaude", "vendor": "anthropic"}
