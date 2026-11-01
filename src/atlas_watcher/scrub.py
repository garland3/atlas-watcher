"""Last-line defense: strip identity leaks from agent output before posting."""
from __future__ import annotations

import re

from .config import settings

_FORBIDDEN_WORDS = (
    r"claude",
    r"anthropic",
    r"codex",
    r"openai",
    r"gpt",
    r"chatgpt",
    r"cline",
    r"opencode",
    r"openrouter",
)

# The xAI/Grok tokens are narrower than the list above, on purpose: "grok"
# is an ordinary English verb and "XAI" is a standard abbreviation for
# explainable AI, so a bare word match would silently corrupt reviews that
# legitimately use those words. Only identity-shaped forms are scrubbed —
# model ids ("grok-4.7"), the domain ("x.ai") and the org id ("x-ai").
# prompts/identity.md still forbids the bare words at the source; this
# scrubber is the last-line net, and a missed bare mention costs less than
# a mangled review.
_XAI_WORDS = (
    r"grok[-_.\s]?\d",   # grok-4, grok 4.7, grok4
    r"x\.ai",
    r"\bx-ai\b",
)
# Trailing "s?" catches plurals/org forms ("anthropics", "codexes" → "codex"
# already matches); \b handles possessives ("claude's") on its own.
_WORD_RE = re.compile(rf"\b({'|'.join(_FORBIDDEN_WORDS)})s?\b", re.IGNORECASE)
_PHRASE_RES = [
    (re.compile(r"\b(large language model|language model)\b", re.IGNORECASE),
     "automated reviewer"),
    (re.compile(r"\bas an ai\b", re.IGNORECASE),
     "as an automated reviewer"),
    (re.compile(r"\bi am an ai\b", re.IGNORECASE),
     "this is an automated review"),
    (re.compile(r"\bmy training data\b", re.IGNORECASE),
     "the available context"),
    (re.compile(r"\bmy knowledge cutoff\b", re.IGNORECASE),
     "the available context"),
]


# Dot-directory names in the *reviewed* codebase (`~/.claude/skills`,
# `.codex/config`, `foo/.cline/`) are not identity leaks — they are quoted
# upstream paths. Scrubbing them silently corrupts the review into something
# factually wrong ("Default roots include `~/.the reviewer/skills`"), which is
# worse than useless on a PR that is *about* those directories.
#
# The exemption is deliberately narrow: the token must be a dot-directory —
# preceded by "." which itself starts the path segment ("/", "~", or the start
# of the token). That covers real paths while still scrubbing vendor URLs
# ("https://claude.ai", "github.com/anthropics/claude-code") and model names
# ("claude-sonnet-4", "gpt-4"), where the term IS the identity leak.
_TOKEN_BREAK = set(" \t\n\r\"'`()[]{}<>,;:|")


def _is_dot_directory(text: str, start: int, end: int) -> bool:
    """True if the match at [start, end) is a dot-directory path segment."""
    if start == 0 or text[start - 1] != ".":
        return False
    # The "." must begin a path segment, not be a mid-token separator
    # (which would make this "claude.ai" or "foo.claude-3").
    before_dot = text[start - 2] if start >= 2 else ""
    if before_dot not in ("", "/", "~") and before_dot not in _TOKEN_BREAK:
        return False
    # Must be followed by a path separator or the end of the token, so
    # ".claude-sonnet" is still scrubbed.
    nxt = text[end] if end < len(text) else ""
    return nxt in ("", "/") or nxt in _TOKEN_BREAK


def scrub_identity(text: str) -> str:
    """Return text with forbidden identity tokens neutralized.

    Dot-directory paths quoted from the reviewed codebase are left intact —
    see :func:`_is_dot_directory`.
    """
    def sub(source: str, pattern: re.Pattern) -> str:
        def _replace(m: re.Match[str]) -> str:
            if _is_dot_directory(source, m.start(), m.end()):
                return m.group(0)
            return "the reviewer"
        return pattern.sub(_replace, source)

    out = sub(text, _WORD_RE)
    out = sub(out, re.compile("|".join(_XAI_WORDS), re.IGNORECASE))
    for pattern, replacement in _PHRASE_RES:
        out = pattern.sub(replacement, out)
    return out


def sign_comment(body: str) -> str:
    """Append the bot signature."""
    return f"{body.rstrip()}\n\n---\n_Posted automatically by **{settings.bot_name}**_\n"
