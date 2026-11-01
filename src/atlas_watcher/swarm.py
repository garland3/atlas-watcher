"""tmux-based parallel swarm launcher with per-role failover.

Each agent gets its own per-run shell script under
``<out_dir>/perspectives/.runners/<name>.sh`` which does:

    cd <checkout>
    timeout N <agent_cmd> < prompt > stdout.log 2> stderr.log
    echo $? > sentinel

Crucially, the agent itself writes its findings file (under perspectives/)
using its own file-write tool — we no longer redirect stdout into the
findings file. Stdout/stderr are kept only as a debug transcript.

Each perspective's role is backed by an ordered candidate list (see
``config.py``). The swarm runs in ROUNDS: every round launches the
perspectives that still need a run, each on the best available candidate.
When a round ends, agents that failed with an *infrastructure* failure — an
agent rate/session limit or an authentication error — are re-launched next
round on the next candidate in their role's list (failover). Ordinary
failures are NOT failed over: a PR that breaks one model must not burn every
other vendor's quota in turn; those agents are stubbed for the manager to
flag, as before.

The watcher blocks on per-agent sentinel files. If the tmux session
disappears before all sentinels exist, missing agents are marked failed
and we move on instead of hanging forever.

After the rounds, the swarm reports which agent/vendor produced each
perspective (``agents.json``, merged across resumed runs) and enforces
``MIN_SWARM_VENDORS``: fewer distinct vendors with real findings than the
minimum defers the item rather than posting a single-vendor review. A
completed swarm that still falls short gets a bounded round of re-runs on
unseen vendors; a re-run that fails has the slot's original findings
restored, so an opportunistic re-run never destroys a finished
perspective.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import agents, cooldown
from .config import settings
from .log import log
from .paths import PROMPTS


class AgentLimitError(RuntimeError):
    """Raised when agents report an account-wide rate/session limit and no
    fallback candidate is available.

    Callers treat this as infrastructure, not a defect in the item: it is
    recorded as ``deferred`` so it never consumes the per-item retry budget,
    and the per-vendor cooldown gates the next attempt.
    """

    def __init__(self, message: str, reset_ts: float | None = None) -> None:
        super().__init__(message)
        self.reset_ts = reset_ts


class VendorDiversityError(RuntimeError):
    """Raised when a finished swarm has findings from fewer distinct vendors
    than ``MIN_SWARM_VENDORS``.

    Like a limit, this is treated as infrastructure (``deferred``), never as
    a defect in the item being reviewed.
    """


# Statuses recorded as "deferred" (infrastructure) rather than "failed".
DEFERRED_ERRORS = (AgentLimitError, VendorDiversityError)

# Failed-agent log signatures that mean "this CLI cannot run here" and are
# worth failing over from, beyond a rate limit itself. Checked only on the
# tail of a failed attempt's logs and only for a failed run, so review
# content that *discusses* authentication (or a diff that contains the word)
# is not misread as the agent's own auth error.
_AUTH_PATTERNS = (
    r"\bunauthorized\b",
    r"\bauthentication (error|failed|failure|required)\b",
    r"\binvalid (api |auth )?key\b",
    r"\b(api |auth )?key (is )?invalid\b",
    r"\bnot (logged in|authenticated)\b",
    r"\bplease (run )?/?log ?in\b",
    r"\blogin required\b",
)
_AUTH_RE = re.compile("|".join(_AUTH_PATTERNS), re.IGNORECASE)


def _read_log(path: Path) -> str:
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        pass
    return ""


def _limit_from_logs(*paths: Path, anchored: bool = False,
                     exclude_text: str | None = None,
                     ) -> cooldown.LimitInfo | None:
    """Classify each log's tail SEPARATELY — a match spanning a stdout/stderr
    concatenation boundary is meaningless, and per-file scanning keeps the
    tail window honest for each stream. classify_limit does the echo
    filtering itself so the reset-line neighbor pairing stays correct."""
    for path in paths:
        info = cooldown.classify_limit(
            _read_log(path), anchored=anchored, exclude_text=exclude_text)
        if info:
            return info
    return None


def _auth_from_logs(*paths: Path, exclude_text: str | None = None) -> bool:
    """True if a *failed* attempt's log tail shows an authentication error.

    Scans the same shared window as the limit classifier
    (cooldown.filtered_tail): the tail, cut at a line boundary, minus echoed
    prompt lines — so a cut-off echoed diff line containing "Unauthorized",
    or a PR that quotes auth errors in its text, cannot read as the CLI's
    own auth error.
    """
    for path in paths:
        if _AUTH_RE.search(cooldown.filtered_tail(_read_log(path),
                                                  exclude_text)):
            return True
    return False


@dataclass(frozen=True)
class Perspective:
    name: str           # short label used as window name and findings filename stem
    role: str           # which role's candidate list fills this slot
    prompt_file: str    # filename under prompts/


PERSPECTIVES: tuple[Perspective, ...] = (
    Perspective("security",     "perspective", "perspective-1-security.md"),
    Perspective("correctness",  "perspective", "perspective-2-correctness.md"),
    Perspective("architecture", "perspective", "perspective-3-architecture.md"),
    Perspective("testing",      "perspective", "perspective-4-testing.md"),
    Perspective("performance",  "perspective", "perspective-5-performance.md"),
    Perspective("ux",           "perspective", "perspective-6-ux.md"),
    Perspective("evidence",     "perspective", "perspective-7-evidence.md"),
    Perspective("generic-a",    "generic-a",   "generic-review.md"),
    Perspective("generic-b",    "generic-b",   "generic-review.md"),
)


@dataclass(frozen=True)
class _Plan:
    perspective: Perspective
    prompt_path: Path
    runner_path: Path
    done_path: Path
    findings_path: Path
    logs_dir: Path

    def attempt_logs(self, agent: str) -> tuple[Path, Path]:
        """Per-attempt transcript paths — one pair per candidate that runs
        this perspective, so failed-over attempts stay inspectable."""
        stem = f"{self.perspective.name}.{agent}"
        return (self.logs_dir / f"{stem}.stdout.log",
                self.logs_dir / f"{stem}.stderr.log")


def _output_instructions(findings_path: Path, perspective_name: str) -> str:
    """Tell the agent exactly where to write its findings markdown."""
    return f"""## Output — required action

Use your file-write tool to create the following file with your findings:

    {findings_path}

The file must be valid Markdown and follow the format specified in the
perspective rules above. Create parent directories if they do not exist.
Use absolute paths if your tool requires them.

Do **not** print the same content to stdout — stdout is only kept for
debugging and will be ignored. After writing the file, exit cleanly.

Naming convention (do not improvise): the file is named
``{perspective_name}.md`` and lives in the ``perspectives/`` directory of
this PR's review folder. The harness expects this exact path."""


def _build_prompt(perspective: Perspective, context_file: Path,
                  findings_path: Path) -> str:
    identity = (PROMPTS / "identity.md").read_text(encoding="utf-8")
    persp = (PROMPTS / perspective.prompt_file).read_text(encoding="utf-8")
    ctx = context_file.read_text(encoding="utf-8")
    out_instr = _output_instructions(findings_path, perspective.name)
    return (
        f"{identity}\n\n---\n\n"
        f"{persp}\n\n---\n\n"
        f"{out_instr}\n\n---\n\n"
        f"# PR context\n\n{ctx}"
    )


def _write_runner(
    runner_path: Path, *, checkout: Path, agent_cmd: str, prompt: Path,
    stdout_log: Path, stderr_log: Path, done: Path,
) -> None:
    """Write the per-agent shell script and chmod +x."""
    # tmux sessions inherit the *tmux server's* environment, not the
    # environment of whoever launched the swarm (cron, a shell, ...). A stale
    # ANTHROPIC_API_KEY baked into a long-lived tmux server silently shadows
    # the CLI's own login and 401s every agent. Drop it unless the operator
    # explicitly opted in via AGENT_INHERIT_API_KEY.
    unset_line = ("" if settings.agent_inherit_api_key
                  else "unset ANTHROPIC_API_KEY anthropic_key\n")
    script = (
        "#!/usr/bin/env bash\n"
        "# Auto-generated by atlas_watcher.swarm. Do not hand-edit.\n"
        "set -u\n"
        f"{unset_line}"
        f"cd {shlex.quote(str(checkout))}\n"
        f"timeout {settings.agent_timeout_secs} {agent_cmd} "
        f"< {shlex.quote(str(prompt))} "
        f"> {shlex.quote(str(stdout_log))} "
        f"2> {shlex.quote(str(stderr_log))}\n"
        f"rc=$?\n"
        f"printf '%s\\n' \"$rc\" > {shlex.quote(str(done))}\n"
        'if [[ "$rc" -ne 0 ]]; then echo "exit rc=$rc — closing in 30s" >&2; sleep 30; fi\n'
    )
    runner_path.write_text(script, encoding="utf-8")
    runner_path.chmod(0o755)


def _session_alive(session: str) -> bool:
    return subprocess.run(
        ["tmux", "has-session", "-t", session],
        capture_output=True, check=False,
    ).returncode == 0


# Marker in the placeholder the harness writes for an agent that produced
# nothing. Keyed on this, not on NO_FINDINGS: a bare NO_FINDINGS is a real
# result ("nothing to flag") that a resumed swarm must not re-run.
_STUB_MARKER = "agent did not write findings file"


def _is_stub(path: Path) -> bool:
    """True if a findings file is the placeholder the harness writes when an
    agent produced nothing (as opposed to real findings)."""
    try:
        return _STUB_MARKER in path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return True


def _agent_completed(done_path: Path, findings_path: Path) -> bool:
    """True if this perspective finished cleanly and its findings are real.

    A resumable swarm skips these agents. The sentinel carries the runner's
    exit code, so a crash (rc != 0) or a stub findings file means "re-run".
    """
    if not done_path.is_file() or not findings_path.is_file():
        return False
    try:
        rc = int(done_path.read_text(encoding="utf-8").strip() or "1")
    except ValueError:
        return False
    if rc != 0:
        return False
    return findings_path.stat().st_size > 0 and not _is_stub(findings_path)


def _plans(out_dir: Path) -> list[_Plan]:
    perspectives_dir = out_dir / "perspectives"
    sentinels_dir = perspectives_dir / ".sentinels"
    logs_dir = perspectives_dir / ".logs"
    runners_dir = perspectives_dir / ".runners"
    for d in (perspectives_dir, sentinels_dir, logs_dir, runners_dir):
        d.mkdir(parents=True, exist_ok=True)
    return [
        _Plan(
            perspective=p,
            prompt_path=perspectives_dir / f".{p.name}.prompt.md",
            runner_path=runners_dir / f"{p.name}.sh",
            done_path=sentinels_dir / f"{p.name}.done",
            findings_path=perspectives_dir / f"{p.name}.md",
            logs_dir=logs_dir,
        )
        for p in PERSPECTIVES
    ]


# --- what actually ran ----------------------------------------------------

def _record_file(out_dir: Path) -> Path:
    return out_dir / "agents.json"


def _load_record(out_dir: Path) -> dict[str, dict]:
    """Which agent/vendor produced each perspective, merged across runs.

    Resumed swarms must account for vendors that produced findings in an
    earlier process (diversity is a property of the whole swarm, not of one
    pass). Entries for perspectives that no longer exist are dropped.
    """
    try:
        raw = json.loads(_record_file(out_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return {p.name: raw[p.name] for p in PERSPECTIVES
            if isinstance(raw.get(p.name), dict)}


def _save_record(out_dir: Path, record: dict[str, dict]) -> None:
    _record_file(out_dir).write_text(
        json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")


def record_vendors(record: dict[str, dict]) -> set[str]:
    """Distinct vendors that produced real findings (stubs never count)."""
    return {
        rec["vendor"] for rec in record.values()
        if isinstance(rec, dict) and rec.get("vendor") and not rec.get("stub")
    }


def read_agent_record(out_dir: Path) -> dict[str, dict]:
    """Public read of the merged per-perspective agent record."""
    return _load_record(out_dir)


# The pre-fallback hardcoded slot→agent mapping. Used only to backfill the
# record for a review folder written before agents.json existed (upgrade
# day): those perspectives are complete on disk but their producer would
# otherwise be invisible to diversity accounting.
_LEGACY_SLOT_AGENTS: dict[str, str] = {
    "security": "dclaude", "correctness": "dclaude",
    "architecture": "dclaude", "testing": "dclaude",
    "performance": "dclaude", "ux": "dclaude", "evidence": "dclaude",
    "generic-a": "codex", "generic-b": "cline",
}


def _backfill_legacy_record(plans: list[_Plan], record: dict[str, dict]) -> None:
    """Seed record entries for slots completed by the pre-fallback code."""
    for pl in plans:
        name = pl.perspective.name
        if name in record:
            continue
        if not _agent_completed(pl.done_path, pl.findings_path):
            continue
        legacy = _LEGACY_SLOT_AGENTS.get(name)
        if legacy:
            record[name] = {"agent": legacy,
                            "vendor": agents.vendor_of(legacy)}


# --- the swarm ------------------------------------------------------------

def _fmt_reset(reset: float | None) -> str:
    if not reset:
        return "unknown"
    return datetime.fromtimestamp(reset, tz=UTC).astimezone().isoformat(
        timespec="seconds")


def _raise_exhausted(
    exhausted: list[tuple[_Plan, str]], exhausted_roles: set[str],
) -> None:
    """No candidate is available for some pending perspectives.

    A cooldown exhaustion is infrastructure (self-healing: the item is
    deferred and the next pass resumes when the earliest cooldown lifts). A
    missing-binary exhaustion is configuration (nothing will heal it), so it
    fails the run normally.
    """
    names = sorted({pl.perspective.name for pl, _ in exhausted})
    if any(reason == "cooldown" for _, reason in exhausted):
        reset = agents.roles_ready_at(sorted(exhausted_roles))
        raise AgentLimitError(
            f"every candidate for {names} is cooling down or missing; "
            f"resumes {_fmt_reset(reset)}", reset)
    raise RuntimeError(
        f"no usable agent for {names}: every candidate's CLI binary is "
        f"missing, the name is unknown, or every runner failed this pass "
        f"(see perspectives/.logs) — check the *_CMD settings, "
        f"AGENT_COMMANDS, and the role fallback lists")


def _launch_round(session: str, launches: list[tuple[_Plan, str]],
                  checkout: Path, context_file: Path) -> None:
    """Write prompts/runners for this round's (plan, agent) pairs and start
    one tmux window per agent."""
    for pl, agent in launches:
        # Clear stale artifacts so a re-run starts clean; a partial findings
        # file from a failed attempt must not be mistaken for real output.
        for stale in (pl.findings_path, pl.done_path):
            stale.unlink(missing_ok=True)
        pl.prompt_path.write_text(
            _build_prompt(pl.perspective, context_file, pl.findings_path),
            encoding="utf-8",
        )
        stdout_log, stderr_log = pl.attempt_logs(agent)
        _write_runner(
            pl.runner_path,
            checkout=checkout,
            agent_cmd=agents.command_of(agent),
            prompt=pl.prompt_path,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            done=pl.done_path,
        )

    subprocess.run(["tmux", "kill-session", "-t", session],
                   check=False, capture_output=True)
    # The session is anchored by a plain shell window that stays alive for
    # the whole round: agents that fail INSTANTLY (a limit printed before
    # doing any work) would otherwise tear down the session between window
    # creations, and the remaining new-window calls would fail. The anchor
    # window idles; it never reads the prompt (runners redirect stdin).
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "-x", "220", "-y", "50"],
        check=True,
    )
    subprocess.run(
        ["tmux", "set-option", "-w", "-t", session, "remain-on-exit", "on"],
        check=False, capture_output=True,
    )
    for pl, _ in launches:
        subprocess.run(
            ["tmux", "new-window", "-t", session, "-n", pl.perspective.name,
             "bash", str(pl.runner_path)],
            check=True,
        )

    list_out = subprocess.run(
        ["tmux", "list-windows", "-t", session, "-F", "#{window_name}"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    expected = {pl.perspective.name for pl, _ in launches}
    missing = expected - set(list_out)
    if missing:
        log(f"WARN: tmux is missing windows {sorted(missing)} — got {list_out}")


# Added to AGENT_TIMEOUT_SECS when waiting for a round: the runner's own
# `timeout` caps each agent, then it writes its sentinel — the grace covers
# the runner's failure sleep (30s) and startup skew. Without a deadline, a
# runner killed before writing its sentinel (OOM, a killed pane) would hang
# the wait loop forever, because the anchor window keeps the session alive.
_WAIT_GRACE_SECS = 180
_POLL_SECS = 10


def _wait_round(session: str, launches: list[tuple[_Plan, str]]) -> None:
    """Block on this round's sentinel files, with a wall-clock deadline."""
    total = len(launches)
    last_done = -1
    deadline = time.monotonic() + settings.agent_timeout_secs + _WAIT_GRACE_SECS
    while True:
        done = sum(1 for pl, _ in launches if pl.done_path.is_file())
        if done != last_done:
            log(f"swarm progress: {done}/{total}")
            last_done = done
        if done >= total:
            break
        if not _session_alive(session):
            for pl, _ in launches:
                if not pl.done_path.is_file():
                    pl.done_path.write_text("session_died\n", encoding="utf-8")
                    log(f"WARN: {pl.perspective.name} sentinel synthesized "
                        "— tmux session died")
            break
        if time.monotonic() > deadline:
            for pl, _ in launches:
                if not pl.done_path.is_file():
                    pl.done_path.write_text("timed_out\n", encoding="utf-8")
                    log(f"WARN: {pl.perspective.name} sentinel synthesized "
                        "— wait deadline exceeded")
            break
        time.sleep(_POLL_SECS)

    subprocess.run(["tmux", "kill-session", "-t", session],
                   check=False, capture_output=True)


def _classify_round(launches: list[tuple[_Plan, str]], record: dict[str, dict],
                    seen_vendors: set[str], excluded: set[str],
                    resolved: set[str]) -> int:
    """Sort this round's outcomes into complete / failover / stub.

    Returns the number of agents that failed with an infrastructure error;
    they stay pending and are relaunched next round on their next candidate.
    Ordinary failures are resolved for this pass (stubbed when they produced
    nothing) and never failed over — a PR that breaks one model must not
    burn every other vendor's quota in turn.
    """
    infra_failures = 0
    # One round = one limit hit per vendor for backoff purposes: seven
    # perspectives failing on the same capped account are ONE event, not
    # seven consecutive hits.
    limits: dict[str, cooldown.LimitInfo] = {}
    for pl, agent in launches:
        name = pl.perspective.name
        vendor = agents.vendor_of(agent)
        if _agent_completed(pl.done_path, pl.findings_path):
            record[name] = {"agent": agent, "vendor": vendor}
            seen_vendors.add(vendor)
            continue
        stdout_log, stderr_log = pl.attempt_logs(agent)
        # Each slot has its own prompt (a distinct perspective rules file plus
        # the shared context), so the echo filter must be built from THAT
        # slot's prompt: excluding with another slot's prompt could suppress
        # a real CLI error (a phrase present in one prompt but not the
        # other), or fail to suppress a line that only this slot echoes.
        slot_prompt = _read_log(pl.prompt_path)
        limit = _limit_from_logs(stdout_log, stderr_log,
                                 exclude_text=slot_prompt)
        if limit:
            limits.setdefault(vendor, limit)
            excluded.add(agent)
            infra_failures += 1
            continue
        if _auth_from_logs(stdout_log, stderr_log, exclude_text=slot_prompt):
            log(f"swarm: {name} agent {agent} failed authentication "
                f"— failing over")
            excluded.add(agent)
            infra_failures += 1
            continue
        # Ordinary failure: resolved for this pass. Stub it when it produced
        # nothing so the manager can flag the gap; keep any findings it did
        # manage to write (the manager reads what is there).
        if not pl.findings_path.is_file() or pl.findings_path.stat().st_size == 0:
            pl.findings_path.write_text(
                f"<!-- {name}: {_STUB_MARKER}. "
                f"See .logs/{name}.{agent}.stdout.log and "
                f".logs/{name}.{agent}.stderr.log -->\n"
                "NO_FINDINGS\n",
                encoding="utf-8",
            )
            log(f"WARN: {name} ({agent}) produced no findings file; stub written")
            record[name] = {"agent": agent, "vendor": vendor, "stub": True}
        else:
            record[name] = {"agent": agent, "vendor": vendor}
        resolved.add(name)
    for vendor, limit in limits.items():
        cooldown.record(vendor, limit)
    return infra_failures


def _diversity_rerun_targets(plans, record, seen_vendors, excluded,
                             rerun_tried) -> list[tuple[_Plan, str]]:
    """Slots worth re-running on an unseen vendor to grow diversity.

    A completed swarm that falls short of MIN_SWARM_VENDORS can often still
    improve: a slot that completed on vendor X (the only candidate its
    operator-list order ever reached) may have a candidate on vendor Y that
    is available right now. Re-running a bounded number of such slots on an
    unseen vendor replaces their findings with a run on the new vendor, so
    the gate can actually be met instead of deferring forever.

    Each slot is re-run at most once per pass for this purpose
    (``rerun_tried``); a failed attempt restores the slot's original
    findings instead of stubbing it (see :func:`_restore_rerun_backups`),
    so a re-run can never turn a finished perspective into a stub or loop.
    """
    minimum = settings.min_swarm_vendors
    need = minimum - len(record_vendors(record))
    if need <= 0:
        return []
    targets: list[tuple[_Plan, str]] = []
    gained: set[str] = set()
    for pl in plans:
        name = pl.perspective.name
        if name in rerun_tried:
            continue  # one re-run attempt per slot per pass
        rec = record.get(name)
        if not rec or rec.get("stub"):
            continue  # stubs are handled by the ordinary-failure branch
        agent, _reason = agents.pick(
            agents.role_candidates(pl.perspective.role),
            exclude_agents=excluded,
            seen_vendors=seen_vendors,
            require_unseen_vendor=True,
        )
        if agent is None:
            continue
        vendor = agents.vendor_of(agent)
        if vendor in gained:
            continue
        gained.add(vendor)
        targets.append((pl, agent))
        if len(targets) >= need:
            break
    return targets


def _restore_rerun_backups(launches: list[tuple[_Plan, str]],
                           record: dict[str, dict],
                           rerun_backups: dict[str, tuple[Path, str, dict]],
                           unseen_only: set[str]) -> None:
    """Undo diversity re-runs that did not finish cleanly.

    A re-run is opportunistic — it exists to grow vendor diversity, and the
    slot it touches had already produced real findings. When the attempt
    fails (an ordinary failure classified as a stub, or an infra failure
    that leaves the slot pending), the backed-up findings file, completion
    sentinel, and record entry are put back: the slot ends up exactly as it
    was before the attempt, so a failed re-run can never replace a finished
    perspective with a stub. An attempt that did finish cleanly keeps its
    new findings, and its backup is discarded.
    """
    for pl, _agent in launches:
        name = pl.perspective.name
        backup = rerun_backups.pop(name, None)
        if backup is None:
            continue
        findings_bak, done_text, original = backup
        if (_agent_completed(pl.done_path, pl.findings_path)
                and not (record.get(name) or {}).get("stub")):
            # the re-run succeeded: keep the new findings, drop the backup
            findings_bak.unlink(missing_ok=True)
            continue
        findings_bak.replace(pl.findings_path)
        pl.done_path.write_text(done_text, encoding="utf-8")
        record[name] = dict(original)
        unseen_only.discard(name)
        log(f"swarm: {name}'s diversity re-run failed — restored its "
            f"original findings from {original.get('agent', 'unknown agent')}")


def run_swarm(repo: str, pr_number: int, checkout: Path,
              out_dir: Path, context_file: Path) -> dict[str, dict]:
    """Launch all perspectives in parallel, failing over as needed.

    Resumable: an agent whose sentinel exists with rc=0 and whose findings
    file is real is left alone, so a re-run after a manager failure (or a
    rate-limit pause) re-executes only the agents that actually need it.
    Vendors that produced findings in an earlier run are read from
    ``agents.json`` so diversity accounting spans runs.

    Returns the merged record mapping perspective name to
    ``{"agent": ..., "vendor": ..., "stub"?: True}``.
    """
    plans = _plans(out_dir)
    record = _load_record(out_dir)
    _backfill_legacy_record(plans, record)
    seen_vendors = record_vendors(record)
    excluded: set[str] = set()
    resolved: set[str] = set()   # resolved THIS pass (stubs, kept partials)
    # Slots being re-run on an unseen vendor to grow diversity: they pick
    # with require_unseen_vendor so the re-run cannot just reproduce the
    # vendor the record already has.
    unseen_only: set[str] = set()
    # Diversity re-run bookkeeping: each slot is re-run at most once per
    # pass (rerun_tried), and each re-run's original completed state
    # (findings file, sentinel text, record entry) is kept in rerun_backups
    # for restore if the attempt does not finish cleanly.
    rerun_tried: set[str] = set()
    rerun_backups: dict[str, tuple[Path, str, dict]] = {}
    round_no = 0

    while True:
        pending = [pl for pl in plans
                   if pl.perspective.name not in resolved
                   and not _agent_completed(pl.done_path, pl.findings_path)]
        if not pending:
            # Every slot is resolved for this pass. A shortfall on vendor
            # diversity can still improve: re-run a bounded number of
            # completed slots on unseen vendors before giving up. A re-run
            # that fails has the slot's original findings restored
            # (_restore_rerun_backups), so the shortfall stays a defer
            # rather than a stub, and the slot is marked tried so the next
            # shortfall pass gives up instead of livelocking.
            targets = _diversity_rerun_targets(plans, record, seen_vendors,
                                               excluded, rerun_tried)
            if targets:
                for pl, agent in targets:
                    name = pl.perspective.name
                    log(f"swarm: re-running {name} on {agent} "
                        f"({agents.vendor_of(agent)}) to reach vendor "
                        f"diversity")
                    # Keep the slot's completed state aside: a re-run that
                    # fails must not turn a real perspective into a stub (or
                    # erase the vendor it came from).
                    findings_bak = pl.findings_path.with_suffix(
                        pl.findings_path.suffix + ".rerun-bak")
                    pl.findings_path.replace(findings_bak)
                    rerun_backups[name] = (
                        findings_bak,
                        pl.done_path.read_text(encoding="utf-8")
                        if pl.done_path.is_file() else "0\n",
                        dict(record.get(name) or {}),
                    )
                    pl.done_path.unlink(missing_ok=True)
                    record.pop(name, None)
                    unseen_only.add(name)
                    rerun_tried.add(name)
                seen_vendors = record_vendors(record)
                continue
            break
        round_no += 1
        launches: list[tuple[_Plan, str]] = []
        exhausted: list[tuple[_Plan, str]] = []
        exhausted_roles: set[str] = set()
        for pl in pending:
            agent, reason = agents.pick(
                agents.role_candidates(pl.perspective.role),
                exclude_agents=excluded,
                seen_vendors=seen_vendors,
                require_unseen_vendor=pl.perspective.name in unseen_only,
            )
            if agent is None:
                exhausted.append((pl, reason))
                exhausted_roles.add(pl.perspective.role)
            else:
                launches.append((pl, agent))
        if exhausted:
            _save_record(out_dir, record)
            _raise_exhausted(exhausted, exhausted_roles)
        if round_no > 1:
            log(f"swarm: failover round {round_no} — "
                f"{', '.join(pl.perspective.name for pl, _ in launches)}")
        repo_slug_safe = repo.replace("/", "-")
        session = f"swarm-{repo_slug_safe}-pr-{pr_number}-{os.getpid()}"
        _launch_round(session, launches, checkout, context_file)
        log(f"swarm round {round_no} started: tmux session={session}, "
            f"{len(launches)}/{len(plans)} agents "
            f"(resuming {len(plans) - len(pending)})")
        log(f"attach with: tmux attach -t {session}")
        _wait_round(session, launches)
        failures = _classify_round(launches, record, seen_vendors, excluded,
                                   resolved)
        _restore_rerun_backups(launches, record, rerun_backups, unseen_only)
        _save_record(out_dir, record)
        unseen_only -= resolved
        if failures == 0:
            # Every launched slot resolved cleanly this pass. A vendor-
            # diversity shortfall can still improve from here: the loop head
            # checks for re-run targets when pending is empty, so fall
            # through to it instead of breaking on the first shortfall pass
            # (the old break deferred every under-diversified swarm at
            # least once).
            continue

    if not any(not rec.get("stub") for rec in record.values()):
        raise RuntimeError(
            f"swarm: all {len(plans)} agents failed without findings; "
            f"see {out_dir / 'perspectives' / '.logs'}")

    vendors = record_vendors(record)
    minimum = settings.min_swarm_vendors
    if len(vendors) < minimum:
        stubbed = sorted(name for name, rec in record.items()
                         if isinstance(rec, dict) and rec.get("stub"))
        if stubbed:
            # The shortfall traces to ordinary failures: a retry would re-run
            # the same agents on the same PR and stub out again, so deferring
            # would loop forever with no ceiling. Fail normally instead —
            # MAX_RETRIES and the per-item backoff apply.
            raise RuntimeError(
                f"swarm findings cover {len(vendors)} vendor(s) "
                f"{sorted(vendors)} but MIN_SWARM_VENDORS={minimum}; slots "
                f"{stubbed} failed ordinarily (stubs) — failing so the retry "
                f"budget applies instead of deferring forever")
        # No stubs: every slot either produced findings or its role is
        # cooldown-blocked (which raises earlier). A shortfall here means
        # the reachable vendors are temporarily capped out of reach of the
        # configured lists — self-healing, so defer.
        raise VendorDiversityError(
            f"swarm findings cover {len(vendors)} vendor(s) "
            f"{sorted(vendors)} but MIN_SWARM_VENDORS={minimum}; deferring "
            f"rather than posting a single-vendor review — widen the role "
            f"fallback lists or wait for the capped vendors to recover")
    log(f"swarm complete: {len(record)}/{len(plans)} perspectives, "
        f"vendors={sorted(vendors)}")
    return record


# --- single-agent roles ---------------------------------------------------

@dataclass(frozen=True)
class RunResult:
    agent: str
    vendor: str
    rc: int


def _attempt_log(base: Path, agent: str) -> Path:
    """Per-attempt log path: ``manager.log`` → ``manager.dclaude.log`` —
    failed-over attempts each keep their own transcript."""
    return base.with_name(f"{base.stem}.{agent}{base.suffix}")


def _launch_single(agent: str, prompt: str, out_file: Path,
                   log_path: Path, cwd: Path | None) -> int:
    # Same stale-credential guard as the tmux runners (see _write_runner):
    # an invalid ANTHROPIC_API_KEY in the inherited environment shadows the
    # CLI's own login and 401s the manager / security-triage agent.
    unset_prefix = ("" if settings.agent_inherit_api_key
                    else "unset ANTHROPIC_API_KEY anthropic_key; ")
    cmd = ["bash", "-lc",
           (f"{unset_prefix}timeout {settings.agent_timeout_secs} "
            f"{agents.command_of(agent)}")]
    with out_file.open("wb") as out, log_path.open("wb") as err:
        proc = subprocess.run(cmd, input=prompt.encode("utf-8"),
                              stdout=out, stderr=err, check=False,
                              cwd=str(cwd) if cwd else None)
    return proc.returncode


def run_role(role: str, prompt: str, out_file: Path,
             log_file: Path | None = None,
             cwd: Path | None = None,
             raise_on_failure: bool = True) -> RunResult:
    """Run one role synchronously, failing over across its candidate list.

    Used by the manager and the triage flows. The agent reads the prompt
    from stdin; stdout is captured to ``out_file`` (overwritten by the
    attempt that decides the outcome). Fails over on infrastructure
    failures only — an agent rate/session limit, an authentication error,
    or a missing binary (preflighted before launch). An ordinary failure
    is the item's problem, not the vendor's:

    - ``raise_on_failure=True`` (default) raises on it, so the caller
      decides whether the item counts as failed.
    - ``raise_on_failure=False`` returns the failed attempt's
      :class:`RunResult` with its rc instead — used by triage and manager
      roles, where an empty/failed run must fall back to a cautious
      ``warn`` verdict (triage) or an explicit "no summary" check
      (manager) rather than marking the whole item failed. Limit and
      auth failures still fail over in this mode.
    """
    candidates = agents.role_candidates(role)
    excluded: set[str] = set()
    recorded_vendors: set[str] = set()
    last_failure = "no candidate was ever usable (missing binaries?)"
    while True:
        agent, reason = agents.pick(candidates, exclude_agents=excluded)
        if agent is None:
            if reason == "cooldown":
                # roles_ready_at checks liveness on every cooldown entry
                # (per-vendor and the legacy global), so an expired
                # per-vendor entry can't hide a still-live global pause.
                reset = agents.roles_ready_at((role,))
                raise AgentLimitError(
                    f"{role}: every candidate is cooling down; "
                    f"earliest reset {_fmt_reset(reset)}", reset)
            raise RuntimeError(
                f"{role}: no usable agent (candidates: "
                f"{', '.join(candidates)}); {last_failure}")
        log_path = _attempt_log(log_file or
                                out_file.with_suffix(out_file.suffix + ".log"),
                                agent)
        vendor = agents.vendor_of(agent)
        rc = _launch_single(agent, prompt, out_file, log_path, cwd)
        if rc == 0:
            # A CLI can print a limit and still exit 0 (truncated output).
            # Only a line that *opens* with the phrase counts on a
            # successful run; a failed run is matched leniently below.
            limit = _limit_from_logs(out_file, log_path, anchored=True,
                                     exclude_text=prompt)
            if limit:
                if vendor not in recorded_vendors:
                    cooldown.record(vendor, limit)
                    recorded_vendors.add(vendor)
                excluded.add(agent)
                last_failure = f"{agent} hit an agent limit (rc=0)"
                continue
            return RunResult(agent, vendor, rc)
        limit = _limit_from_logs(out_file, log_path, exclude_text=prompt)
        if limit:
            if vendor not in recorded_vendors:
                cooldown.record(vendor, limit)
                recorded_vendors.add(vendor)
            excluded.add(agent)
            last_failure = f"{agent} hit an agent limit (rc={rc})"
            continue
        if _auth_from_logs(out_file, log_path, exclude_text=prompt):
            log(f"{role}: agent {agent} failed authentication — failing over")
            excluded.add(agent)
            last_failure = f"{agent} failed authentication (rc={rc})"
            continue
        if raise_on_failure:
            raise RuntimeError(
                f"{role} agent {agent} failed with rc={rc}; see {log_path}")
        # Non-raising mode: the caller (triage/manager) handles an ordinary
        # failure itself — a cautious warn or a "no summary" check — so the
        # attempt's rc is surfaced instead of marking the item failed.
        log(f"WARN: {role} agent {agent} failed with rc={rc} "
            f"(non-raising mode); see {log_path}")
        return RunResult(agent, vendor, rc)
