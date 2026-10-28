"""tmux-based parallel swarm launcher.

Each agent gets its own per-run shell script under
``<out_dir>/perspectives/.runners/<name>.sh`` which does:

    cd <checkout>
    timeout N <agent_cmd> < prompt > stdout.log 2> stderr.log
    echo $? > sentinel

Crucially, the agent itself writes its findings file (under perspectives/)
using its own file-write tool — we no longer redirect stdout into the
findings file. Stdout/stderr are kept only as a debug transcript.

The watcher blocks on per-agent sentinel files. If the tmux session
disappears before all sentinels exist, missing agents are marked failed
and we move on instead of hanging forever. After the wait loop, we also
check that each agent actually produced its findings file; missing files
are filled with a stub the manager can recognize.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import cooldown
from .config import AGENT_COMMAND_FIELDS, settings
from .log import log
from .paths import PROMPTS


class AgentLimitError(RuntimeError):
    """Raised when an agent reports an account-wide rate/session limit.

    Callers treat this as infrastructure, not a defect in the item: it sets a
    global cooldown and is recorded as ``deferred`` so it never consumes the
    per-item retry budget.
    """

    def __init__(self, message: str, reset_ts: float | None = None) -> None:
        super().__init__(message)
        self.reset_ts = reset_ts


def _limit_from_logs(*paths: Path,
                     anchored: bool = False) -> cooldown.LimitInfo | None:
    for p in paths:
        try:
            if p.is_file():
                info = cooldown.classify_limit(
                    p.read_text(encoding="utf-8", errors="ignore"),
                    anchored=anchored,
                )
                if info:
                    return info
        except OSError:
            continue
    return None



@dataclass(frozen=True)
class Perspective:
    name: str           # short label used as window name and findings filename stem
    agent: str          # which CLI: "dclaude", "codex", "cline"
    prompt_file: str    # filename under prompts/


PERSPECTIVES: tuple[Perspective, ...] = (
    Perspective("security",     "dclaude", "perspective-1-security.md"),
    Perspective("correctness",  "dclaude", "perspective-2-correctness.md"),
    Perspective("architecture", "dclaude", "perspective-3-architecture.md"),
    Perspective("testing",      "dclaude", "perspective-4-testing.md"),
    Perspective("performance",  "dclaude", "perspective-5-performance.md"),
    Perspective("ux",           "dclaude", "perspective-6-ux.md"),
    Perspective("evidence",     "dclaude", "perspective-7-evidence.md"),
    Perspective("generic-a",    "codex",   "generic-review.md"),
    Perspective("generic-b",    "cline",   "generic-review.md"),
)


def _agent_cmd(agent: str) -> str:
    """Resolve an agent name to its CLI command.

    AGENT_COMMANDS wins over the built-ins, so an operator can repoint an
    existing name or register a new CLI entirely from config.
    """
    override = settings.agent_commands.get(agent)
    if override:
        return override
    attr = AGENT_COMMAND_FIELDS.get(agent)
    if attr:
        return getattr(settings, attr)
    known = sorted(set(AGENT_COMMAND_FIELDS) | set(settings.agent_commands))
    raise ValueError(
        f"unknown agent: {agent!r}; built-ins/config: {', '.join(known)}"
    )


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
        capture_output=True,
    ).returncode == 0


def _is_stub(path: Path) -> bool:
    """True if a findings file is the placeholder the harness writes when an
    agent produced nothing (as opposed to real findings)."""
    try:
        return "NO_FINDINGS" in path.read_text(encoding="utf-8", errors="ignore")
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


def perspectives_complete(out_dir: Path) -> bool:
    perspectives_dir = out_dir / "perspectives"
    return all(
        _agent_completed(
            perspectives_dir / ".sentinels" / f"{p.name}.done",
            perspectives_dir / f"{p.name}.md",
        )
        for p in PERSPECTIVES
    )


def run_swarm(repo: str, pr_number: int, checkout: Path,
              out_dir: Path, context_file: Path) -> None:
    """Launch all perspectives in parallel and block until done.

    Resumable: an agent whose sentinel exists with rc=0 and whose findings
    file is real is left alone, so a re-run after a manager failure only
    re-executes the agents that actually need it (usually none).
    """
    perspectives_dir = out_dir / "perspectives"
    sentinels_dir = perspectives_dir / ".sentinels"
    logs_dir = perspectives_dir / ".logs"
    runners_dir = perspectives_dir / ".runners"
    for d in (perspectives_dir, sentinels_dir, logs_dir, runners_dir):
        d.mkdir(parents=True, exist_ok=True)

    plans = []
    for p in PERSPECTIVES:
        prompt_path = perspectives_dir / f".{p.name}.prompt.md"
        findings_path = perspectives_dir / f"{p.name}.md"
        stdout_log = logs_dir / f"{p.name}.stdout.log"
        stderr_log = logs_dir / f"{p.name}.stderr.log"
        done_path = sentinels_dir / f"{p.name}.done"
        runner_path = runners_dir / f"{p.name}.sh"
        plans.append((p, prompt_path, runner_path, done_path, findings_path,
                      stdout_log, stderr_log))

    pending = [pl for pl in plans if not _agent_completed(pl[3], pl[4])]
    if not pending:
        log("swarm: all perspectives already complete — resuming at manager")
        return

    for p, prompt_path, runner_path, done_path, findings_path, stdout_log, stderr_log in pending:
        prompt_path.write_text(
            _build_prompt(p, context_file, findings_path), encoding="utf-8",
        )
        for stale in (findings_path, done_path):
            stale.unlink(missing_ok=True)
        _write_runner(
            runner_path,
            checkout=checkout,
            agent_cmd=_agent_cmd(p.agent),
            prompt=prompt_path,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            done=done_path,
        )

    repo_slug_safe = repo.replace("/", "-")
    session = f"swarm-{repo_slug_safe}-pr-{pr_number}-{os.getpid()}"
    subprocess.run(["tmux", "kill-session", "-t", session],
                   check=False, capture_output=True)

    first_p, _, first_runner, *_ = pending[0]
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "-x", "220", "-y", "50",
         "-n", first_p.name, "bash", str(first_runner)],
        check=True,
    )
    for p, _, runner, *_ in pending[1:]:
        subprocess.run(
            ["tmux", "new-window", "-t", session, "-n", p.name,
             "bash", str(runner)],
            check=True,
        )

    list_out = subprocess.run(
        ["tmux", "list-windows", "-t", session, "-F", "#{window_name}"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    expected = {p.name for p, *_ in pending}
    missing = expected - set(list_out)
    if missing:
        log(f"WARN: tmux is missing windows {sorted(missing)} — got {list_out}")

    log(f"swarm started: tmux session={session}, {len(pending)}/{len(plans)} "
        f"agents (resuming {len(plans) - len(pending)})")
    log(f"attach with: tmux attach -t {session}")

    total = len(pending)
    last_done = -1
    while True:
        done = sum(1 for pl in pending if pl[3].is_file())
        if done != last_done:
            log(f"swarm progress: {done}/{total}")
            last_done = done
        if done >= total:
            break
        if not _session_alive(session):
            for p, _, _, done_path, *_ in pending:
                if not done_path.is_file():
                    done_path.write_text("session_died\n", encoding="utf-8")
                    log(f"WARN: {p.name} sentinel synthesized — tmux session died")
            break
        time.sleep(10)

    subprocess.run(["tmux", "kill-session", "-t", session],
                   check=False, capture_output=True)

    # Verify each agent actually wrote its findings file. If not, stub it so
    # the manager can flag the gap rather than silently dropping a perspective.
    for p, _, _, _, findings_path, *_ in pending:
        if not findings_path.is_file() or findings_path.stat().st_size == 0:
            findings_path.write_text(
                f"<!-- {p.name}: agent did not write findings file. "
                f"See .logs/{p.name}.stdout.log and .logs/{p.name}.stderr.log -->\n"
                "NO_FINDINGS\n",
                encoding="utf-8",
            )
            log(f"WARN: {p.name} produced no findings file; stub written")

    # A rate limit can strike any agent and leaves rc=0 with a stub, so the
    # completed-check alone would not notice. Inspect the logs we just wrote
    # and escalate: the whole pass must pause, not limp on to the manager.
    # Only failed agents are considered — a successful agent's logs may
    # mention rate limiting while discussing the code under review.
    for p, _, _, done_path, findings_path, stdout_log, stderr_log in pending:
        if _agent_completed(done_path, findings_path):
            continue
        limit = _limit_from_logs(stdout_log, stderr_log)
        if limit:
            cooldown.record(limit)
            raise AgentLimitError(
                f"{p.name} hit an agent limit: {limit.reason}", limit.reset_ts,
            )

    log("swarm complete")



def run_single(agent: str, prompt: str, out_file: Path,
               log_file: Path | None = None,
               cwd: Path | None = None) -> int:
    """Run one agent synchronously, capturing stdout to out_file.

    Used by the manager and the issue-triage flow. The agent reads the
    prompt from stdin. ``cwd`` controls the working directory; default is
    the caller's cwd.
    """
    # Same stale-credential guard as the tmux runners (see _write_runner):
    # an invalid ANTHROPIC_API_KEY in the inherited environment shadows the
    # CLI's own login and 401s the manager / security-triage agent.
    unset_prefix = ("" if settings.agent_inherit_api_key
                    else "unset ANTHROPIC_API_KEY anthropic_key; ")
    cmd = ["bash", "-lc",
           f"{unset_prefix}timeout {settings.agent_timeout_secs} "
           f"{_agent_cmd(agent)}"]
    log_path = log_file or out_file.with_suffix(out_file.suffix + ".log")
    with out_file.open("wb") as out, log_path.open("wb") as err:
        proc = subprocess.run(cmd, input=prompt.encode("utf-8"),
                              stdout=out, stderr=err, check=False,
                              cwd=str(cwd) if cwd else None)

    # A rate/session limit exits before producing usable output. Detect it
    # here so the caller aborts instead of parsing (and caching) a garbage
    # result, then raise: this is account-wide infrastructure, not a defect
    # in the item being reviewed. A successful run's output is real content
    # that may quote upstream text mentioning a limit, so only a line that
    # *opens* with the phrase counts then; a failed run is matched leniently.
    limit = _limit_from_logs(out_file, log_path,
                             anchored=proc.returncode == 0)
    if limit:
        cooldown.record(limit)
        raise AgentLimitError(limit.reason, limit.reset_ts)
    return proc.returncode
