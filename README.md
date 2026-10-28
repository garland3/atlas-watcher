# atlas-watcher

Automated multi-repo review bot, posting back as **AGENT-REVIEW-BOT-3**.

- **PRs:** checks out the head commit, runs a 9-agent review swarm in tmux
  (seven specific perspectives — security, correctness, architecture, testing,
  performance, UX, and a sufficiency-of-evidence score — plus two cross-tool
  generic reviewers). Each agent writes
  its own findings file. A manager agent synthesizes one combined comment,
  and the harness posts the comment upstream and pushes the audit trail.
- **Issues:** triages each new issue, archives a long internal analysis here,
  and posts back a short clarifying question only when the issue is too vague
  to act on.
- **Audit trail:** every prompt, perspective report, summary, and posted
  comment is committed to this repo, organized as
  `reviews/<owner>/<name>/{prs,issues}/...` in the audit-record repo.

## Quick start

This is a Python project managed with [`uv`](https://docs.astral.sh/uv/).

```bash
gh auth status                                    # must be logged in
uv sync                                           # install deps into .venv

uv run atlas-watcher --help
uv run atlas-watcher scan                         # one pass over every repo in REPOS
uv run atlas-watcher loop                         # poll forever
uv run atlas-watcher pr 42 --dry-run              # try one PR on the default repo
uv run atlas-watcher pr 42 --repo other/repo      # specify a non-default repo
uv run atlas-watcher issue 99                     # triage one issue
```

## Configuring multiple repos

`config.env` defines `REPOS` as a comma-separated list of `owner/name`
entries. The first entry is the **default repo** used by single-target
commands when `--repo` is omitted.

```env
REPOS=sandialabs/atlas-ui-3,myorg/another-repo,foo/bar
```

`uv run atlas-watcher scan` walks every repo in order and processes PRs
then issues for each. The audit trail is namespaced per repo, and ledger
rows include the repo so PR numbers can never collide across them.

Per-repo clones live under `workdir/<owner>/<name>/` and are kept across
runs — the orchestrator clones once and only fetches on subsequent scans.

## Running on a 15-minute cron

The `scan` subcommand runs one pass and exits, which is what cron wants.
Wrap it in `flock` so a slow run doesn't stack up if the next tick fires
before the previous finished:

```cron
# crontab -e — run scan every 15 minutes; one at a time only
*/15 * * * * /usr/bin/flock -n /tmp/atlas-watcher.lock /home/garlan/.local/bin/uv --directory /home/garlan/git/atlas-watcher run atlas-watcher scan >> /home/garlan/git/atlas-watcher/logs/cron.log 2>&1
```

Notes:

- Replace `/home/garlan/...` paths with whatever your install actually uses.
  Find your `uv` with `which uv`.
- `uv --directory <path> run` lets cron invoke `atlas-watcher` without
  `cd`-ing first; uv will pick up `.venv` from that directory.
- `flock -n` makes the new tick exit immediately if the previous run is
  still going. Use `-w 60` instead if you want the new tick to wait up to
  a minute before giving up.
- The watcher writes structured logs to stderr; cron captures stderr into
  `logs/cron.log` (gitignored). Rotate it with `logrotate` if it grows.
- Cron jobs run with a minimal environment. If your `uv`, `gh`, `tmux`,
  `claude`, `codex`, `cline`, or `opencode` binaries live in user-specific
  paths, set `PATH` at the top of the crontab:
  ```
  PATH=/home/garlan/.local/bin:/home/garlan/.nvm/versions/node/v22.22.1/bin:/usr/bin:/bin
  ```
- The same authenticated GitHub user (`gh auth status`) needs comment
  access on **every** repo in `REPOS`.

To verify cron picked up the schedule:

```bash
crontab -l
tail -f logs/cron.log
```

For a one-shot dry run from cron's environment:

```bash
env -i HOME=$HOME PATH=/home/garlan/.local/bin:/usr/bin:/bin \
  /home/garlan/.local/bin/uv --directory /home/garlan/git/atlas-watcher \
  run atlas-watcher scan --dry-run
```

If that succeeds, the real cron line will too.

## Configuration

Edit `config.env` (or override in `config.local.env`, gitignored). See the
header of that file for every knob. Notable ones:

- `REPOS` — comma-separated list of `owner/name` repos to watch.
- `AUDIT_REPO` — GitHub remote of the separate, private audit-record repo
  (`garland3/ATLAS-WATCHER-LOGS`).
- `AUDIT_DIR` — local path of that repo (default `../ATLAS-WATCHER-LOGS`).
  Bootstrap it once with `uv run atlas-watcher audit-init`.
- `BOT_NAME` — comment-body signature.
- `DCLAUDE_CMD`, `CODEX_CMD`, `CLINE_CMD`, `OPENCODE_CMD` — agent CLI
  commands.
- `MANAGER_AGENT` — CLI that runs the manager/orchestrator (`dclaude`,
  `codex`, `cline`, `opencode`, or any name registered in `AGENT_COMMANDS`).
  The tracked default is `dclaude` (Claude CLI).
- `AGENT_COMMANDS` — optional JSON map of extra agent names to commands, so
  a new CLI can become the orchestrator without a code change.
- `AGENT_TIMEOUT_SECS` — per-agent wall-clock cap (default 1800s).
- `POLL_INTERVAL_SECS` — sleep between passes in `loop` mode (default 900s
  / 15 min, matching the cron cadence).

## How it works (and why)

See [CLAUDE.md](./CLAUDE.md) — the full operations manual, including
identity rules, repo layout, idempotency model, multi-repo clone strategy,
and editing guidelines for anyone (human or agent) modifying this repo.
