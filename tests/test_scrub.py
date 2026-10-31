"""Identity scrubbing: vendor terms added for the fallback feature."""
from atlas_watcher.scrub import scrub_identity


def test_new_vendor_terms_are_scrubbed():
    assert "grok" not in scrub_identity("ran on grok for this review").lower()
    assert "xai" not in scrub_identity("the xai endpoint said").lower()
    assert "x.ai" not in scrub_identity("see https://x.ai/docs").lower()
    assert "grok" not in scrub_identity("model grok-4.7 reviewed it").lower()


def test_existing_terms_still_scrubbed():
    out = scrub_identity("claude and codex and cline and opencode")
    for term in ("claude", "codex", "cline", "opencode"):
        assert term not in out.lower()


def test_dot_directory_exemption_survives():
    text = "Default roots include `~/.claude/skills` and `.codex/config`."
    assert scrub_identity(text) == text
