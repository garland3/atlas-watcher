"""Identity scrubbing: vendor terms added for the fallback feature."""
from atlas_watcher.scrub import scrub_identity


def test_identity_shaped_xai_terms_are_scrubbed():
    assert "grok-4.7" not in scrub_identity("model grok-4.7 reviewed it")
    assert "grok" not in scrub_identity("model grok4 reviewed it")
    assert "x.ai" not in scrub_identity("see https://x.ai/docs")
    assert "x-ai" not in scrub_identity("openrouter/x-ai/grok-4.7")


def test_ordinary_uses_of_grok_and_xai_survive():
    # "grok" is an ordinary verb and "XAI" a standard ML abbreviation;
    # scrubbing them would corrupt legitimate review prose.
    text = "It takes a while to grok the new XAI pipeline in this module."
    assert scrub_identity(text) == text


def test_deepseek_and_glm_identity_terms_are_scrubbed():
    for leak in ("deepseek/deepseek-v4.1-flash", "DeepSeek reviewed it",
                 "openrouter/z-ai/glm-5.3-flash", "model glm-5.3", "glm4",
                 "see https://z.ai/docs", "a Zhipu model"):
        out = scrub_identity(leak).lower()
        for term in ("deepseek", "glm-5", "glm4", "z-ai", "z.ai", "zhipu"):
            assert term not in out, (leak, out)


def test_glm_as_a_statistics_term_survives():
    text = "Fit a GLM here; the glm() call in stats.py drops the offset."
    assert scrub_identity(text) == text


def test_existing_terms_still_scrubbed():
    out = scrub_identity("claude and codex and cline and opencode")
    for term in ("claude", "codex", "cline", "opencode"):
        assert term not in out.lower()


def test_dot_directory_exemption_survives():
    text = "Default roots include `~/.claude/skills` and `.codex/config`."
    assert scrub_identity(text) == text
