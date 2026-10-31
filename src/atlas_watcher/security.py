"""Prompt-injection triage for upstream PR/issue content.

For every PR or issue we're about to process, run a small classifier agent
on the upstream-controlled text (PR description, issue body+comments) and
combine its verdict with cheap regex/length heuristics. The combined
verdict drives a three-way decision:

- ``proceed``: nothing concerning. Run the swarm/triage as normal.
- ``warn``:    run the swarm/triage, but prepend a neutral one-line
               banner to the upstream comment so a human is aware.
- ``block``:   skip the swarm/triage entirely. Mark the ledger row as
               ``security_blocked`` so we don't keep re-assessing.

The model verdict is a single JSON object — see ``prompts/security-triage.md``.
The heuristic verdict adds independent score bumps for cheap signals (large
text, base64 blobs, jailbreak phrases, bidi overrides, …). Final score
is ``max(model, heuristic)``; final risk is the band that score lands in.

Blocking additionally requires corroboration: a single ambiguous heuristic
is capped at ``warn`` rather than suppressing the review outright — see
:func:`_corroborated`. The two signals stay independent (a compromised model
cannot clear a decisive heuristic, and vice versa); what changed is only how
much *one* weak signal can do unilaterally, because a silent block is a far
more expensive false positive than a banner.

Verdicts are cached on disk as ``security.json`` next to the rest of the
review folder, so re-runs (e.g. after `reconcile-ledger`) reuse the
existing assessment instead of paying for the agent again.
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from . import agents, swarm
from .config import settings
from .log import log
from .paths import PROMPTS

Risk = Literal["low", "medium", "high", "critical"]
Decision = Literal["proceed", "warn", "block"]

_RISK_ORDER: tuple[Risk, ...] = ("low", "medium", "high", "critical")
_RISK_RANK = {r: i for i, r in enumerate(_RISK_ORDER)}


# --- score → risk band ---------------------------------------------------

def _score_to_risk(score: int) -> Risk:
    if score >= 80:
        return "critical"
    if score >= 60:
        return "high"
    if score >= 30:
        return "medium"
    return "low"


# --- heuristic signals (model-independent) -------------------------------

_JAILBREAK_PHRASES = (
    r"ignore (the|all|previous|prior|above) (instructions?|rules?|prompt)",
    r"disregard (the|all|previous|prior|above) (instructions?|rules?|prompt)",
    r"forget (the|all|previous|prior|above) (instructions?|rules?|prompt)",
    r"you are (now|actually) (a|an) ",
    r"new (instructions?|rules?|system prompt)\s*[:.\-]",
    r"system prompt\s*[:.\-]",
    r"reveal (your|the) (prompt|system message|instructions?)",
    r"print (your|the) (prompt|system message|instructions?)",
    r"show (your|the) (prompt|system message|instructions?)",
    r"act as (a|an|if) ",
    r"\bDAN\b.*(mode|jailbreak)",
    r"developer mode",
    r"(you'?re|you are) not (a|an) (ai|bot|assistant)",
    r"pretend (you'?re|you are|to be)",
    # Direct manipulation of bot output / posting behavior.
    r"(post|comment|reply|say|output) (the|exactly|verbatim) (following|this)",
    r"replace (your|the) (response|output|comment) with",
)
_JAILBREAK_RE = re.compile("|".join(_JAILBREAK_PHRASES), re.IGNORECASE)

# Large base64-ish blocks are a smuggling vector.
_BASE64_BLOB_RE = re.compile(r"[A-Za-z0-9+/=]{200,}")

# Fenced/inline code and quoted lines. A PR *about* prompt injection quotes
# these phrases in code samples, docs and quoted logs; an injection aimed at
# the reader is written as prose. We still score code/quote matches (an
# attacker can hide a directive in a fence, and agents do read fences) — just
# at a lower weight than the same phrase in prose. See _split_prose_and_code.
_CODE_SPAN_RE = re.compile(
    r"```.*?```|~~~.*?~~~|`[^`\n]+`|^[ \t]*>.*$",
    re.DOTALL | re.MULTILINE,
)

# Bidi overrides (RLO/LRO/PDF, isolates). These reorder rendered text so what
# a human reviewer reads differs from what an agent parses. There is no
# legitimate use in a technical PR, so a single one is decisive on its own.
_BIDI_OVERRIDE_CHARS = {
    "‪", "‫", "‬", "‭", "‮",
    "⁦", "⁧", "⁨", "⁩",
}

# Zero-width characters. Unlike bidi overrides these appear constantly in
# benign text — emoji ZWJ sequences (👨‍💻), BOMs from copy-paste, soft joins in
# CJK. Scored on density and never decisive alone. See _zero_width_count.
_ZERO_WIDTH_CHARS = {"​", "‌", "‍", "⁠", "﻿"}

# HTML comments can hide instructions in rendered Markdown.
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _split_prose_and_code(text: str) -> tuple[str, str]:
    """Split `text` into (prose, code) — code being fenced/inline code spans
    and quoted lines. Both halves are still scanned; prose just weighs more."""
    code = "\n".join(m.group(0) for m in _CODE_SPAN_RE.finditer(text))
    prose = _CODE_SPAN_RE.sub(" ", text)
    return prose, code


def _zero_width_count(text: str) -> int:
    """Count zero-width chars, excluding legitimate emoji ZWJ sequences.

    A ZWJ joining two pictographic characters (👨‍💻) is ordinary content, not
    an obfuscation attempt. Only ZWJs that glue ASCII — i.e. ones that could
    be splitting a keyword to evade a filter — are counted.
    """
    n = 0
    for i, ch in enumerate(text):
        if ch not in _ZERO_WIDTH_CHARS:
            continue
        if ch == "‍":
            prev = text[i - 1] if i else ""
            nxt = text[i + 1] if i + 1 < len(text) else ""
            # Both neighbours non-ASCII → emoji/CJK join, benign.
            if prev and nxt and ord(prev) > 127 and ord(nxt) > 127:
                continue
        n += 1
    return n


def _heuristic_verdict(text: str) -> dict[str, Any]:
    signals: list[str] = []
    score = 0
    # "Decisive" signals are ones with no plausible benign explanation, and
    # are allowed to block on their own. Everything else needs corroboration
    # (see _corroborated) — it can raise a warning banner but not a block.
    decisive = False

    prose, code = _split_prose_and_code(text)
    n_prose = len(_JAILBREAK_RE.findall(prose))
    n_code = len(_JAILBREAK_RE.findall(code))
    if n_prose or n_code:
        signals.append(f"jailbreak_phrases=prose:{n_prose},code:{n_code}")
        if n_prose >= 2:
            # Multiple directives addressed at the reader in prose — this is
            # what an actual injection looks like. Decisive.
            score = max(score, 60 + min(n_prose * 5, 25))
            decisive = True
        elif n_prose == 1:
            # One match is usually a PR that *discusses* injection. Warn tier.
            score = max(score, 45)
        if n_code:
            score = max(score, 30 + min(n_code * 3, 15))

    n_b64 = len(_BASE64_BLOB_RE.findall(text))
    if n_b64:
        signals.append(f"base64_blobs={n_b64}")
        score = max(score, 35 + min(n_b64 * 5, 30))

    n_bidi = sum(1 for ch in text if ch in _BIDI_OVERRIDE_CHARS)
    if n_bidi:
        # No legitimate use in a technical PR — one is enough.
        signals.append(f"bidi_override_chars={n_bidi}")
        score = max(score, 70 + min(n_bidi, 20))
        decisive = True

    n_zw = _zero_width_count(text)
    if n_zw:
        signals.append(f"zero_width_chars={n_zw}")
        # Density-scaled and capped below the block band: benign copy-paste
        # artifacts are common, so this corroborates but never blocks alone.
        score = max(score, 30 if n_zw < 20 else 50)

    # Mixed-script homoglyph hints: lots of non-ASCII in what should be a
    # technical doc. Cheap proxy: high count of non-ASCII characters.
    non_ascii = sum(1 for ch in text if ord(ch) > 127
                    and unicodedata.category(ch)[0] == "L")
    if non_ascii > 200:
        signals.append(f"non_ascii_letters={non_ascii}")
        score = max(score, 25)

    n_html_comments = len(_HTML_COMMENT_RE.findall(text))
    if n_html_comments > 5:
        signals.append(f"html_comments={n_html_comments}")
        score = max(score, 25)

    if len(text) > settings.security_text_max_bytes:
        signals.append(f"oversized_input={len(text)}")
        score = max(score, 30)

    return {
        "score": int(score),
        "risk": _score_to_risk(int(score)),
        "signals": signals,
        "decisive": decisive,
    }


# --- model verdict -------------------------------------------------------

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_model_output(raw: str) -> dict[str, Any]:
    """Pull the first {...} JSON block out of `raw` and validate shape."""
    match = _JSON_BLOCK_RE.search(raw)
    if not match:
        return {
            "risk": "medium",
            "score": 40,
            "categories": ["off_topic"],
            "reasons": ["model output had no JSON block"],
            "parse_error": True,
        }
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        return {
            "risk": "medium",
            "score": 40,
            "categories": ["off_topic"],
            "reasons": [f"model JSON unparseable: {e}"],
            "parse_error": True,
        }
    risk = data.get("risk")
    if risk not in _RISK_ORDER:
        risk = _score_to_risk(int(data.get("score", 0) or 0))
    score = int(data.get("score", 0) or 0)
    score = max(0, min(100, score))
    cats = data.get("categories") or ["none"]
    if not isinstance(cats, list):
        cats = ["none"]
    reasons = data.get("reasons") or []
    if not isinstance(reasons, list):
        reasons = [str(reasons)]
    return {
        "risk": risk,
        "score": score,
        "categories": [str(c) for c in cats][:8],
        "reasons": [str(r)[:300] for r in reasons][:8],
    }


def _run_model_verdict(text: str, snap_dir: Path) -> dict[str, Any]:
    template = (PROMPTS / "security-triage.md").read_text(encoding="utf-8")
    capped = text[: settings.security_text_max_bytes]
    prompt = template.replace("{UNTRUSTED}", capped)

    out_path = snap_dir / "security.raw.txt"
    result = swarm.run_role(
        "security-triage", prompt,
        out_file=out_path,
        log_file=snap_dir / "security.log",
        cwd=None,
    )
    raw = out_path.read_text(encoding="utf-8") if out_path.is_file() else ""
    verdict = _parse_model_output(raw)
    verdict["agent_rc"] = result.rc
    # Models score risk differently. Record which agent produced the
    # verdict — and whether it was a fallback — so a weaker failover
    # verdict is traceable (and handled more cautiously, see assess).
    verdict["agent"] = result.agent
    verdict["vendor"] = result.vendor
    verdict["agent_fallback"] = (
        result.agent != agents.role_candidates("security-triage")[0])
    return verdict


# --- decision ------------------------------------------------------------

def _max_risk(*risks: Risk) -> Risk:
    return max(risks, key=_RISK_RANK.get)


def _decide(final_risk: Risk) -> Decision:
    rank = _RISK_RANK[final_risk]
    if rank >= _RISK_RANK[settings.security_block_above]:  # type: ignore[index]
        return "block"
    if rank >= _RISK_RANK[settings.security_warn_above]:  # type: ignore[index]
        return "warn"
    return "proceed"


def _corroborated(heur: dict[str, Any], model: dict[str, Any]) -> bool:
    """Is a `block` supported by more than one weak, ambiguous signal?

    Blocking is the expensive failure mode: it silently drops a PR that no
    human ever hears about. Warning is cheap. So a block must rest on at
    least one of:

    - a *decisive* heuristic (bidi overrides, repeated prose directives),
    - two or more independent heuristic families agreeing, or
    - the model independently reaching the warn tier or higher.

    A lone ambiguous heuristic (one quoted jailbreak phrase, a stray
    zero-width char) still raises the banner via `warn` — it just can't
    suppress the review by itself. This keeps both signals independent per
    CLAUDE.md rule 12(a); it only changes what a single one of them can do
    unilaterally.
    """
    if heur.get("decisive"):
        return True
    if len(heur.get("signals", [])) >= 2:
        return True
    model_risk = model.get("risk", "low")
    if model_risk not in _RISK_RANK:
        return True  # unparseable model verdict — fail closed
    return _RISK_RANK[model_risk] >= _RISK_RANK[settings.security_warn_above]  # type: ignore[index]


# --- public API ----------------------------------------------------------

def assess(repo: str, kind: str, number: int, text: str,
           snap_dir: Path) -> dict[str, Any]:
    """Assess upstream-controlled text for prompt-injection risk.

    Writes the verdict to ``<snap_dir>/security.json`` and a
    human-readable ``security.md`` next to it. If a cached verdict
    already exists, returns it without re-running the agent.
    """
    snap_dir.mkdir(parents=True, exist_ok=True)
    cache = snap_dir / "security.json"
    if cache.is_file() and cache.stat().st_size > 0:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log(f"security: cached {cache} is malformed; reassessing")

    heur = _heuristic_verdict(text)
    model = _run_model_verdict(text, snap_dir)
    final_score = max(int(heur["score"]), int(model["score"]))
    final_risk = _max_risk(heur["risk"], model["risk"])  # type: ignore[arg-type]
    # If either method already crossed the score band, bump risk to match.
    if _RISK_RANK[_score_to_risk(final_score)] > _RISK_RANK[final_risk]:
        final_risk = _score_to_risk(final_score)
    decision = _decide(final_risk)
    downgraded = False
    if (decision == "block" and settings.security_require_corroboration
            and not _corroborated(heur, model)):
        decision = "warn"
        downgraded = True
        log(f"security: {kind} #{number} block downgraded to warn — "
            f"uncorroborated single signal {heur.get('signals')}")

    # A fallback triage model may score risk differently (usually more
    # leniently) than the primary — and a *clean-looking* verdict from an
    # unfamiliar model is exactly where under-scoring would hide. Lean
    # toward warning more, not less (rule 12c): when the verdict came from a
    # fallback agent and ANY signal is nonzero (model score or a heuristic),
    # do not proceed silently — a banner is cheap, a missed injection is
    # not. Blocks are NOT manufactured here; the existing corroboration
    # rules still govern those.
    upgraded = False
    if decision == "proceed" and model.get("agent_fallback"):
        weak_signal = (int(model.get("score") or 0) > 0
                       or bool(heur.get("signals")))
        if weak_signal:
            decision = "warn"
            upgraded = True
            log(f"security: {kind} #{number} proceed upgraded to warn — "
                f"triage ran on fallback agent {model.get('agent')} "
                f"(model score {model.get('score')}, "
                f"signals {heur.get('signals')})")

    verdict = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repo": repo,
        "kind": kind,
        "number": number,
        "final_risk": final_risk,
        "final_score": final_score,
        "decision": decision,
        "block_downgraded_to_warn": downgraded,
        "fallback_upgraded_to_warn": upgraded,
        "model_verdict": model,
        "heuristic_verdict": heur,
        "thresholds": {
            "block_above": settings.security_block_above,
            "warn_above": settings.security_warn_above,
        },
    }
    cache.write_text(json.dumps(verdict, indent=2), encoding="utf-8")
    _write_human_readable(snap_dir, verdict)
    log(f"security: {kind} #{number} → risk={final_risk} score={final_score} decision={decision}")
    return verdict


def _write_human_readable(snap_dir: Path, verdict: dict[str, Any]) -> None:
    md = snap_dir / "security.md"
    lines = [
        f"# Security triage — {verdict['kind']} #{verdict['number']}",
        "",
        f"- **Decision:** `{verdict['decision']}`",
        f"- **Final risk:** `{verdict['final_risk']}` (score {verdict['final_score']})",
        f"- **Thresholds:** warn ≥ `{verdict['thresholds']['warn_above']}`, block ≥ `{verdict['thresholds']['block_above']}`",
    ]
    triage_agent = verdict["model_verdict"].get("agent")
    if triage_agent:
        lines.append(
            f"- **Triage agent:** `{triage_agent}` "
            f"(vendor `{verdict['model_verdict'].get('vendor')}`)"
            + (" — **fallback**, not the primary triage agent"
               if verdict["model_verdict"].get("agent_fallback") else ""))
    if verdict.get("block_downgraded_to_warn"):
        lines.append(
            "- **Downgraded:** score reached the block band on a single "
            "uncorroborated heuristic signal, so this was reduced to `warn` "
            "(reviewed with a banner) rather than dropped silently."
        )
    if verdict.get("fallback_upgraded_to_warn"):
        lines.append(
            "- **Upgraded:** the triage model was a fallback (primary was "
            "unavailable) and was not a clean low, so `proceed` was raised "
            "to `warn` — different models score risk differently, and the "
            "cheap direction is the banner."
        )
    lines += [
        "",
        "## Model verdict",
        f"- risk: `{verdict['model_verdict'].get('risk')}`, "
        f"score: {verdict['model_verdict'].get('score')}",
        f"- categories: {verdict['model_verdict'].get('categories')}",
        "- reasons:",
    ]
    for r in verdict["model_verdict"].get("reasons", []):
        lines.append(f"  - {r}")
    if verdict["model_verdict"].get("parse_error"):
        lines.append("  - **NOTE:** model output failed to parse cleanly")
    lines += [
        "",
        "## Heuristic verdict",
        f"- score: {verdict['heuristic_verdict'].get('score')}",
        f"- signals: {verdict['heuristic_verdict'].get('signals')}",
    ]
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")


def warning_banner(verdict: dict[str, Any]) -> str:
    """A neutral one-paragraph banner safe to prepend to an upstream comment.

    Reveals only the severity tier, not specifics — those live in the
    audit repo for human review.
    """
    risk = verdict.get("final_risk", "medium")
    return (
        f"> **Note:** the input that produced this review tripped an "
        f"automated content-safety check (severity: `{risk}`). Findings "
        f"below should be read with extra skepticism, and any directives "
        f"appearing in the PR/issue text were ignored.\n\n"
    )


def build_pr_text(meta: dict) -> str:
    """The upstream-controlled text fed to the security triage for a PR.

    Includes PR comments: they are upstream-controlled and are now fed to the
    swarm (the evidence reviewer reads them), so the tripwire must cover them.
    """
    parts = [
        f"PR #{meta.get('number')} on branch {meta.get('headRefName', '?')}",
        f"Author: {meta.get('author', {}).get('login', '?')}",
        f"Title: {meta.get('title') or ''}",
        "",
        "Body:",
        meta.get("body") or "",
        "",
        "Comments:",
    ]
    for c in meta.get("comments", []):
        author = c.get("author", {}).get("login", "?")
        parts.append(f"--- {author} ---")
        parts.append(c.get("body") or "")
    return "\n".join(parts)


def build_issue_text(meta: dict) -> str:
    parts = [
        f"Issue #{meta.get('number')}",
        f"Author: {meta.get('author', {}).get('login', '?')}",
        f"Title: {meta.get('title') or ''}",
        "",
        "Body:",
        meta.get("body") or "",
        "",
        "Comments:",
    ]
    for c in meta.get("comments", []):
        author = c.get("author", {}).get("login", "?")
        parts.append(f"--- {author} ---")
        parts.append(c.get("body") or "")
    return "\n".join(parts)
