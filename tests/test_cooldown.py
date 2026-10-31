"""Per-vendor cooldown state and limit detection."""
from __future__ import annotations

import json
import time

from atlas_watcher import cooldown
from atlas_watcher.cooldown import LimitInfo


def _future_reset_str(days: int = 7) -> str:
    """A reset time string relative to now, so the test never rots:
    a hardcoded "Nov 30, 5pm" becomes a past date once November passes."""
    from datetime import datetime, timedelta
    return (datetime.now() + timedelta(days=days)).strftime("%b %d, %I%p")


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


def test_weak_match_reset_is_capped_at_the_backoff_ceiling(
        cooldown_file, patch_settings):
    import time as t
    patch_settings(agent_limit_backoff_max_secs=21600,
                   agent_limit_reset_max_secs=691200)
    # A fallback-heuristic (weak) match claiming a far-future reset must not
    # park the vendor for days.
    weak = cooldown.classify_limit(
        f"you've burned your credits — try again at {_future_reset_str()}")
    assert weak is not None and weak.strong is False
    state = cooldown.record("anthropic", weak)
    assert state["until_ts"] < t.time() + 21600 + 5

    strong = cooldown.classify_limit(
        f"usage limit reached. resets {_future_reset_str()}")
    assert strong is not None and strong.strong is True
    state = cooldown.record("openai", strong)
    assert state["until_ts"] > t.time() + 21600


def test_limit_found_in_the_tail_of_a_huge_failed_log():
    # codex echoes the whole prompt into its log; the limit message is at
    # the very end, far past the size guard.
    echoed_prompt = "REVIEW THIS DIFF: +def spam(): pass\n" * 2000
    log = echoed_prompt + "ERROR: usage limit reached — resets 3pm\n"
    info = cooldown.classify_limit(log, anchored=False)
    assert info is not None
    assert "usage limit reached" in info.reason
    assert info.reset_ts is not None


def test_echoed_prompt_lines_do_not_plant_a_cooldown():
    # An upstream PR whose diff contains a fake limit message: the CLI
    # echoes the prompt (diff included) and dies. The echo must not be
    # mistaken for the CLI's own error.
    planted = f"+Our retry budget was exhausted (quota exceeded) — resets {_future_reset_str()}\n"
    prompt = "# PR context\n" + planted * 50
    log = prompt + "ERROR: exit code 1\n"
    assert cooldown.classify_limit(log, anchored=False,
                                   exclude_text=prompt) is None
    # Without the exclusion (legacy behavior) it WOULD match — this is what
    # the filter is for.
    assert cooldown.classify_limit(log, anchored=False) is not None


def test_reset_is_parsed_from_the_match_line_not_the_whole_tail():
    # A fake reset deep in the echo must not be honored even when a REAL
    # limit line elsewhere triggers the match.
    prompt = ("some diff line\n+quota exceeded, resets Dec 25, 9am\n" * 30)
    log = prompt + "ERROR: usage limit reached\ntry again at 3:30pm\n"
    info = cooldown.classify_limit(log, anchored=False, exclude_text=prompt)
    assert info is not None
    assert info.reset_ts is not None
    # The parsed reset must come from the real message (≈ today 3:30pm),
    # not from Dec 25 in the echo.
    import datetime as dt
    hours_ahead = (dt.datetime.fromtimestamp(info.reset_ts)
                   - dt.datetime.now()).total_seconds() / 3600
    assert hours_ahead < 30


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
