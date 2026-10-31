"""Per-vendor cooldown state and limit detection."""
from __future__ import annotations

import json
import time

from atlas_watcher import cooldown
from atlas_watcher.cooldown import LimitInfo


def test_record_pauses_only_the_hit_vendor(cooldown_file):
    cooldown.record("anthropic", LimitInfo("usage limit reached", None))
    cooldown.record("openai", LimitInfo("quota exceeded", None))
    assert cooldown.active_for("anthropic")
    assert cooldown.active_for("openai")
    assert cooldown.active_for("xai") is None
    assert cooldown.active_for("cline") is None
    assert sorted(cooldown.active_all()) == ["anthropic", "openai"]


def test_consecutive_hits_back_off_per_vendor(cooldown_file):
    cooldown.record("anthropic", LimitInfo("usage limit reached", None))
    first = cooldown.active_for("anthropic")
    cooldown.record("openai", LimitInfo("quota exceeded", None))
    second = cooldown.record("anthropic", LimitInfo("usage limit reached", None))
    assert second["consecutive"] == 2
    assert second["until_ts"] > first["until_ts"]
    # openai's counter is independent
    assert cooldown.active_for("openai")["consecutive"] == 1


def test_stated_reset_time_is_honored_per_vendor(cooldown_file):
    when = time.time() + 5 * 3600
    state = cooldown.record("anthropic", LimitInfo("usage limit reached", when))
    assert abs(state["until_ts"] - when) < 1
    # A different vendor's shorter fallback pause is unaffected
    other = cooldown.record("xai", LimitInfo("quota exceeded", None))
    assert other["until_ts"] < when


def test_expired_state_self_clears(cooldown_file):
    cooldown_file.write_text(json.dumps({"agents": {
        "anthropic": {
            "until_ts": time.time() - 10,
            "set_at": time.time() - 4000,
            "consecutive": 2,
            "reason": "usage limit reached",
        },
    }}), encoding="utf-8")
    assert cooldown.active_for("anthropic") is None
    assert cooldown.active_all() == {}


def test_legacy_global_state_cools_every_vendor(cooldown_file):
    cooldown_file.write_text(json.dumps({
        "until_ts": time.time() + 3600,
        "set_at": time.time(),
        "consecutive": 1,
        "reason": "pre-per-agent cooldown",
    }), encoding="utf-8")
    assert cooldown.active_for("anthropic")
    assert cooldown.active_for("openai")
    assert cooldown.active_for("xai")
    assert sorted(cooldown.active_all()) == ["*"]


def test_earliest_reset_ignores_uncooled_vendors(cooldown_file):
    cooldown.record("anthropic", LimitInfo("usage limit reached",
                                           time.time() + 7200))
    cooldown.record("openai", LimitInfo("quota exceeded",
                                        time.time() + 600))
    soonest = cooldown.earliest_reset(["anthropic", "openai", "xai"])
    assert abs(soonest - (time.time() + 600)) < 5
    assert cooldown.earliest_reset(["xai"]) is None


def test_limit_found_in_the_tail_of_a_huge_failed_log():
    # codex echoes the whole prompt into its log; the limit message is at
    # the very end, far past the size guard.
    echoed_prompt = "REVIEW THIS DIFF: +def spam(): pass\n" * 2000
    log = echoed_prompt + "ERROR: usage limit reached — resets 3pm\n"
    info = cooldown.classify_limit(log, anchored=False)
    assert info is not None
    assert "usage limit reached" in info.reason
    assert info.reset_ts is not None


def test_long_successful_output_is_not_scanned():
    content = ("## Findings\nThe service handles rate limit reached "
               "gracefully.\n" * 200)
    assert cooldown.classify_limit(content, anchored=True) is None


def test_short_successful_output_still_matches_anchored():
    assert cooldown.classify_limit("usage limit reached", anchored=True)
    assert cooldown.classify_limit("Error: usage limit reached", anchored=True)
    # A quoted mention buried after a long prefix does not match.
    assert cooldown.classify_limit(
        "the upstream comment reads: usage limit reached yesterday",
        anchored=True) is None


def test_unknown_wording_fallback_on_failed_output():
    text = "you've burned your credits — try again after 5pm UTC"
    info = cooldown.classify_limit(text, anchored=False)
    assert info is not None
    assert cooldown.classify_limit(text, anchored=True) is None
