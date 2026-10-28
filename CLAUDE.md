# CLAUDE.md — atlas-watcher

This repo is a **multi-repo review bot**. It watches PRs and issues on every
upstream repo listed in `REPOS` (default: `sandialabs/atlas-ui-3`) and posts
back review comments under the persona **`AGENT-REVIEW-BOT-3`**.

If you (Claude) are opening this repo, read this file end to end before
making any change. The rules below are load-bearing and easy to break by
accident.

This is a **Python project managed with `uv`** and exposing a single Typer
CLI: `atlas-watcher`. There are no shell scripts; everything is a CLI
subcommand.

---

## What this repo does

1. `atlas-watcher scan` (or `loop`) iterates over every repo in `$REPOS`
   and lists open PRs and issues from each via the `gh` CLI. PRs first,
   then issues, in declaration order. Single-target commands (`pr <n>`,
   `issue <n>`) default to the first repo in `$REPOS`; pass `--repo
   owner/name` to target a different one.
2. **Security triage runs first** for every PR and issue, before the swarm
   or triage agent starts. A single `dclaude` invocation classifies the
   upstream-controlled text (PR title + body + comments, or issue title +
   body + comments) into a JSON verdict — see `prompts/security-triage.md` and
   `src/atlas_watcher/security.py`. The model verdict is combined with
   model-independent regex/length heuristics (jailbreak phrases, base64
   blobs, RTL-override / zero-width chars, oversized inputs). Final risk
   is the higher of the two — except that a **block** additionally requires
   corroboration (`SECURITY_REQUIRE_CORROBORATION`): a lone ambiguous
   heuristic is capped at `warn` rather than silently suppressing the
   review. Output is cached at
   `<sha_dir>/security.json` (machine-readable) plus `security.md`
   (human-readable) so re-runs reuse the verdict.
   Three decisions:
   - **proceed** — risk below `SECURITY_WARN_ABOVE`. Continue normally.
   - **warn** — at or above `SECURITY_WARN_ABOVE`. The swarm/triage runs;
     before posting upstream, a neutral one-line banner is prepended to
     the comment ("input tripped an automated content-safety check —
     read with skepticism"). Banner template lives in
     `security.warning_banner`; it never reveals model/vendor identity.
   - **block** — at or above `SECURITY_BLOCK_ABOVE`. Swarm/triage is
     skipped entirely. A `blocked.json` sentinel is written next to
     `security.json`, and a ledger row is appended with
     `status:"security_blocked"`. **Nothing is posted upstream.** The
     audit repo gets the verdict committed for human review.
3. For each unreviewed **PR**, it checks out the head commit and launches an
   **9-agent swarm in tmux**, in parallel — one tmux window per agent in a
   single session:
   - 7 windows running `dclaude` on specific perspectives:
     security, correctness, architecture, testing, performance, ux, and
     evidence. The **evidence** perspective does not hunt for defects — it
     scores 1–10 how well the PR demonstrates that the change works (e2e
     screenshots, tests, verification steps), with the bar scaled to the
     size of the change, reading the PR description and comments as well as
     the diff.
   - 1 window running `codex` on the **generic** review prompt
     (`prompts/generic-review.md`) — flags anything it thinks matters.
   - 1 window running `cline` on the same **generic** review prompt.
   Each agent is told the exact path of the markdown findings file it
   must write (under `reviews/prs/PR-<n>/<sha>/perspectives/<name>.md`),
   and uses its own file-write tool to do so. Stdout/stderr are kept only
   as a debug transcript under `.logs/`.
4. Once all 9 finish (we block on per-agent sentinel files), a **manager
   agent** (the orchestrator; prompt: `prompts/manager.md`) reads every
   perspective file and writes the combined `summary.md`. Which CLI runs it
   is set by `MANAGER_AGENT` — any of `dclaude`, `codex`, `cline`,
   `opencode`, or a name registered in `AGENT_COMMANDS`; switching it is a
   config-only change. The tracked `config.env` currently uses `dclaude`
   (the Claude CLI). That is
   all the manager does — it has no shell/git step and is not given the
   audit repo as its cwd, so an agent that has just ingested untrusted
   upstream text never has a git remote in reach. The harness then scrubs
   `summary.md` (defense in depth), appends the bot signature, posts the
   comment upstream via `gh`, and calls `audit.sync()` to publish the whole
   review folder plus the new ledger row.
5. For each unreviewed **issue**, a single triage agent (`dclaude`) produces:
   - a long internal analysis (archived in the audit repo, never posted), and
   - a short, human-sounding clarifying question, posted **only if** the
     issue is too vague to act on. If the issue is already clear, no
     comment is posted (`NO_COMMENT` sentinel).
6. **The durable audit record lives in a separate git repo** —
   `ATLAS-WATCHER-LOGS`, private, owned by garland3. Its local path is
   `$AUDIT_DIR` (default `../ATLAS-WATCHER-LOGS`) and its remote is
   `$AUDIT_REPO`. It holds `reviews/` + `ledger/` at its own root;
   `audit.sync()` commits and pushes there, never to the code repo. This
   split exists so this codebase can be open-sourced without publishing
   third-party diffs or unscrubbed agent transcripts. Bootstrap it with
   `atlas-watcher audit-init`.
7. A ledger (`ledger/ledger.jsonl`, in the audit repo) records what's been done. The watcher
   never re-reviews work that is already in the ledger in a terminal
   state (`done` for posted reviews, `security_blocked` for items the
   security triage refused). Keys: PR head SHA, or issue comment-count
   snapshot. Runs that fail before posting are recorded as `failed` rows
   and retried on a per-item exponential backoff (`RETRY_BACKOFF_*`); after
   `MAX_RETRIES` failures at the same key the item is skipped until the key
   changes or the `failed` rows are removed. An account-wide agent
   rate/session limit is treated differently: it is recorded as `deferred`
   (never counted against the retry budget), sets a global cooldown, and
   the next scan resumes on its own once the cooldown expires — the watcher
   runs unattended, so no recovery step requires a human.

---

## Identity rules — DO NOT VIOLATE

The bot persona is `AGENT-REVIEW-BOT-3`. Posted comments must read like
output from a single neutral review bot. They must **never**:

- Mention or hint at the underlying model, vendor, or tool: `claude`,
  `anthropic`, `codex`, `openai`, `gpt`, `chatgpt`, `cline`, `opencode`,
  `openrouter`, `llm`, `language model`, `as an ai`, `i am an ai`, etc.
- Refer to "the perspectives", "the swarm", "the reviewers", or the
  pipeline. Each comment is one voice.
- Apologize for being automated, refuse on policy grounds, or include
  meta-commentary about being a bot beyond the auto-appended signature.
- Include chain-of-thought, "thinking", or self-reflection.

These rules live in three places. Keep them consistent across all three:

1. `prompts/identity.md` — included in every agent's prompt.
2. `prompts/issue-triage.md` and `prompts/perspective-*.md` — restate
   format constraints per role.
3. `src/atlas_watcher/scrub.py` — last-line regex defense applied to
   every output before posting. If you add a new forbidden term, add it
   to **both** the identity prompt and the scrubber.

One deliberate exemption: **dot-directory paths quoted from the reviewed
codebase** (`~/.claude/skills`, `.codex/config`) are left intact. They are
upstream content, not self-reference, and scrubbing them rewrote reviews
into something factually wrong (`~/.the reviewer/skills`). The exemption is
narrow by design — see `_is_dot_directory`. Vendor URLs (`https://claude.ai`),
org paths (`github.com/anthropics/...`) and model names (`claude-sonnet-4`)
are still scrubbed, because there the term *is* the identity leak. When
changing `scrub.py`, test both directions: paths that must survive and
leaks that must not.

---

## Repo layout

```
atlas-watcher/
├── CLAUDE.md                       # this file
├── README.md
├── pyproject.toml                  # uv-managed project; deps + entry point
├── uv.lock
├── config.env                      # default settings (loaded by pydantic-settings)
├── config.local.env                # optional, gitignored, local overrides
├── src/atlas_watcher/
│   ├── __init__.py
│   ├── cli.py                      # Typer app — every subcommand
│   ├── config.py                   # Settings model (config.env loader)
│   ├── cooldown.py                 # global pause after an agent rate limit
│   ├── paths.py                    # filesystem layout
│   ├── log.py                      # rich console logger
│   ├── ledger.py                   # append + has_done over ledger.jsonl
│   ├── scrub.py                    # scrub_identity, sign_comment
│   ├── gh.py                       # gh CLI wrappers (list/view/comment)
│   ├── git.py                      # PR checkout / diff helpers
│   ├── swarm.py                    # 9-agent tmux orchestrator + run_single
│   ├── post.py                     # only path to gh ... comment (signs body)
│   ├── pr.py                       # process(pr_number)
│   ├── issue.py                    # process(issue_number)
│   └── audit.py                    # init/commit/push the audit-record repo
├── prompts/
│   ├── identity.md                 # universal identity rules
│   ├── perspective-1-security.md
│   ├── perspective-2-correctness.md
│   ├── perspective-3-architecture.md
│   ├── perspective-4-testing.md
│   ├── perspective-5-performance.md
│   ├── perspective-6-ux.md
│   ├── perspective-7-evidence.md   # sufficiency-of-evidence score (1–10)
│   ├── generic-review.md           # used by codex and cline
│   ├── manager.md                  # manager agent: synthesize summary.md
│   └── issue-triage.md             # issue analysis + clarification prompt
├── workdir/<owner>/<name>/        # ONE persistent clone per upstream repo
│                                   # (kept across runs; only fetch + checkout
│                                   # on subsequent reviews); gitignored
└── logs/                           # gitignored; logs/cooldown.json is the
                                    # ephemeral agent rate-limit cooldown
```

The **audit record repo** (`$AUDIT_DIR`, default `../ATLAS-WATCHER-LOGS`) is a
separate git repo with its own remote and its own `.gitignore`:

```
ATLAS-WATCHER-LOGS/
├── reviews/<owner>/<name>/        # namespaced per upstream repo
│   ├── prs/PR-<n>/<sha>/           # one folder per PR per head SHA
│   │   ├── metadata.json
│   │   ├── context.md              # PR description + comments + diff fed to agents
│   │   ├── diff.patch
│   │   ├── perspectives/           # one markdown file per agent, written
│   │   │   ├── security.md         #   by the agent itself via its file-
│   │   │   ├── correctness.md      #   write tool (NOT stdout redirection)
│   │   │   ├── architecture.md
│   │   │   ├── testing.md
│   │   │   ├── performance.md
│   │   │   ├── ux.md
│   │   │   ├── evidence.md          # sufficiency-of-evidence score + gaps
│   │   │   ├── generic-a.md         # codex, generic prompt
│   │   │   ├── generic-b.md         # cline, generic prompt
│   │   │   ├── .runners/<name>.sh  # one launcher script per agent
│   │   │   ├── .sentinels/<name>.done
│   │   │   └── .logs/<name>.{stdout,stderr}.log
│   │   ├── manager.log             # manager agent stderr
│   │   ├── manager.stdout.log      # manager agent stdout (one-line summary)
│   │   ├── summary.md              # what gets posted (after scrub, before signature)
│   │   └── posted.json             # GitHub comment URL
│   └── issues/ISSUE-<n>/comments-<n>/
│       ├── metadata.json
│       ├── context.md
│       ├── triage.raw.md
│       ├── analysis.md             # long internal report
│       ├── comment.md              # short comment posted (or NO_COMMENT)
│       └── posted.json
└── ledger/
    └── ledger.jsonl                # append-only audit log; idempotency source
```

Everything under `perspectives/.runners/`, `.sentinels/`, `.logs/`, plus
`*.log` and `*.raw.txt`, is **gitignored in the audit repo**. Those are
runtime transcripts, not record: they never pass through `scrub.py`, so they
can carry model/vendor identity. They stay on disk for debugging and are
never committed. Dropping them cut the tracked file count from 28,641 to
8,655. The durable record is `metadata.json`, `context.md`, `diff.patch`,
`perspectives/*.md`, `security.{json,md}`, `summary.md`, `analysis.md`,
`comment.md`, `posted.json`, `blocked.json` and the ledger.

---

## Running it

One-time setup:

```bash
gh auth status                   # must be logged in with comment access on $UPSTREAM_REPO
uv sync                          # install deps into .venv
```

The CLI is `atlas-watcher`. Run any subcommand with `uv run`:

```bash
uv run atlas-watcher --help
uv run atlas-watcher config              # show resolved settings
uv run atlas-watcher scan                # one pass over open PRs + issues, then sync
uv run atlas-watcher loop                # poll forever (POLL_INTERVAL_SECS)
uv run atlas-watcher pr 42               # one PR end to end
uv run atlas-watcher issue 99            # one issue end to end
uv run atlas-watcher pr 42 --dry-run     # do everything except post
uv run atlas-watcher audit-init          # bootstrap the audit-record repo
uv run atlas-watcher sync                # commit & push reviews/ + ledger/
uv run atlas-watcher cooldown            # show the agent rate-limit cooldown
```

`cooldown --clear` exists only to debug a stuck timer; recovery is
automatic. A swarm is **resumable**: an agent whose sentinel exists with
`rc=0` and whose findings file is real is not re-run, so a retry after a
manager failure (or a rate-limit pause) re-executes only the agents that
actually need it, not the whole 9-agent swarm.

Watch a live swarm from another shell:

```bash
tmux ls                          # find swarm-pr-<n>-<pid>
tmux attach -t swarm-pr-42-12345
```

To override settings without editing `config.env`, drop a `config.local.env`
in the repo root (gitignored) — same `KEY=VALUE` format. Pydantic-settings
loads `config.env` first, then `config.local.env` overrides.

---

## Idempotency — read this before "fixing" something that looks stuck

The ledger (`ledger/ledger.jsonl`, in the **audit repo** — see
`paths.ledger_file()`) is what makes the watcher safe to re-run.
Before working on a PR or issue, every entry point consults it via
`ledger.has_done(repo, kind, number, key)`. The lookup is keyed by
**(repo, kind, number, key)** — PR numbers can collide across repos, so
the repo field is load-bearing.

- **PRs:** key = head commit SHA. New push → new SHA → new review.
- **Issues:** key = `comments:<n>` (count at triage time). New comment →
  new key → re-triage.
- **Failures:** any exception during `process()` (swarm failure, manager
  refusal, post failure, ...) appends a `failed` row with the error —
  except in `--dry-run`, which records nothing. At `MAX_RETRIES` `failed`
  rows for the same key the item is skipped (`skipped:retries`) so a
  persistently-broken run can't re-launch the swarm every poll forever.
  The counter resets naturally when the key changes; to force a retry at
  the same key, delete that item's `failed` rows from the ledger.
- **Agent rate/session limits are not failures.** `run_swarm` and
  `run_single` scan agent output for a limit, record a global cooldown
  (`cooldown.py`), and raise `AgentLimitError`; `process()` then writes a
  `deferred` row. Deferred rows are neither terminal nor counted by
  `failure_count`, so the item retries automatically after the cooldown
  expires. `scan` checks the cooldown before each item (and between repos),
  so a mid-pass limit stops the pass instead of launching doomed swarms.
  Nothing here needs a human: the cooldown self-expires and the next cron
  pass picks up where this one stopped.

Backward compat: ledger rows written before the multi-repo migration have
no `repo` field. They are treated as belonging to the **first** repo in
`$REPOS` (`settings.repos[0]`) — that's where the data originally came
from. New rows always carry `repo` explicitly.

**Dry-run safety:** `--dry-run` does **not** write to the ledger and does
**not** write `posted.json`. The PR/issue summary still gets generated and
archived under `reviews/`. On a subsequent non-dry-run, `process()` reuses
that existing summary (skipping the swarm + manager) and just posts —
flipping a dry run into a real post is one `atlas-watcher scan` away,
with no rerun cost.

If a previous version of the watcher (or some other failure mode) left
ledger rows for items that were never posted, run:

```bash
uv run atlas-watcher reconcile-ledger
```

It drops every `done` row whose corresponding `posted.json` is missing or
flagged as a dry-run, and keeps everything else. Safe to re-run.

If you want to force a re-review of an item you've already reviewed at the
same key:

1. **Preferred:** push a new commit (PR) or comment (issue) — the watcher
   will pick it up naturally.
2. **Fallback:** edit the ledger to remove or invalidate the row. Don't do
   this casually; it will cause a duplicate comment to land on the upstream
   PR/issue.

Never wipe `ledger.jsonl` unless you genuinely want every open PR re-reviewed
on the next pass. Same for `reviews/` — it's the audit trail.

Because the ledger and the review folders now live outside the code repo,
**pointing `AUDIT_DIR` somewhere new silently resets idempotency** — the
watcher will see an empty ledger and re-review (and re-post on) every open
PR. After any change to `AUDIT_DIR`, run
`uv run atlas-watcher reconcile-ledger`; it cross-checks every terminal row
against `posted.json` / `blocked.json` under the resolved paths, so
`dropped 0` is proof the record and the code agree.

---

## Configuration

Everything tunable lives in `config.env`. Override locally with
`config.local.env` (gitignored). Notable knobs:

- `REPOS` — comma-separated list of `owner/name` repos to watch. Default
  `sandialabs/atlas-ui-3`. The first entry is the default repo for
  single-target CLI commands. Put any other repos in `config.local.env`,
  never in `config.env` (see editing rule 14).
- `AUDIT_REPO` — GitHub remote of the audit-record repo. Default
  `garland3/ATLAS-WATCHER-LOGS`. **Must stay private** — it contains
  verbatim upstream diffs.
- `AUDIT_DIR` — local path of that repo. Relative paths resolve against the
  code repo root; empty means `<code root>/audit-data`. Default
  `../ATLAS-WATCHER-LOGS`.
- `BOT_NAME` — signature line on every comment. Default `AGENT-REVIEW-BOT-3`.
- `MANAGER_AGENT` — which CLI runs the manager (the orchestrator that writes
  the combined summary). One of the built-ins (`dclaude`, `codex`, `cline`,
  `opencode`) or a name registered in `AGENT_COMMANDS`. Switching it requires
  no code change. The tracked default is `dclaude`.
- `DCLAUDE_CMD`, `CODEX_CMD`, `CLINE_CMD`, `OPENCODE_CMD` — the agent CLIs.
  Override if your binary names differ. `dclaude` defaults to
  `claude --dangerously-skip-permissions --print`; `opencode` defaults to
  `opencode run --model openrouter/openrouter/auto --auto` (OpenRouter Auto
  Router).
- `AGENT_COMMANDS` — optional JSON object mapping extra agent names to CLI
  commands, e.g. `{"gemini": "gemini --approval-mode yolo"}`. A key may also
  shadow a built-in to repoint it. This is how "something else" becomes an
  orchestrator with a config-only change.
- `AGENT_TIMEOUT_SECS` — per-agent wall-clock cap. Default 1800.
- `POLL_INTERVAL_SECS` — sleep between passes in `loop` mode. Default
  900 (15 min — matches the recommended cron cadence).
- `MAX_RETRIES` — attempts per PR/issue key before giving up. Failures
  are appended to the ledger as `failed` rows; the counter resets when
  the key changes. Default 3.
- `RETRY_BACKOFF_BASE_SECS`, `RETRY_BACKOFF_MAX_SECS` — per-item retry
  backoff: after a failure, the next attempt waits
  `base * 2^(failures-1)` seconds (capped). Defaults 900 / 86400.
- `AGENT_LIMIT_BACKOFF_SECS`, `AGENT_LIMIT_BACKOFF_MAX_SECS` — global
  cooldown set when an agent reports a rate/session limit. The agent's
  stated reset time is honored when it parses; otherwise the pause backs
  off exponentially, capped at the max. Defaults 3600 / 21600.
- `SECURITY_BLOCK_ABOVE`, `SECURITY_WARN_ABOVE` — risk tiers (`low`,
  `medium`, `high`, `critical`) that gate the swarm. Defaults: block at
  `high`, warn at `medium`. Set both to `critical` to disable.
- `SECURITY_REQUIRE_CORROBORATION` — when true (default), a `block` needs
  more than one weak signal: a *decisive* heuristic (bidi overrides,
  repeated prose directives), two heuristic families agreeing, or the
  model independently at/above the warn tier. Otherwise the block is
  downgraded to `warn` and recorded as `block_downgraded_to_warn` in
  `security.json`.
- `SECURITY_TIMEOUT_SECS` — wall-clock cap for the security triage
  agent. Default 600.
- `AGENT_INHERIT_API_KEY` — default false. Each generated runner does
  `unset ANTHROPIC_API_KEY anthropic_key` so agents use their own CLI
  login. See rule 13 below.
- `SECURITY_TEXT_MAX_BYTES` — cap on bytes of upstream text fed into
  the triage prompt. Default 100_000.
- `DIFF_CONTEXT_MAX_BYTES` — diff size cap fed into prompts. Default 200_000.

The full schema lives in `src/atlas_watcher/config.py`. Add new settings
there (with a default) before referencing them.

---

## Editing rules for future Claude sessions

1. **Use `uv` for everything.** `uv add <pkg>` to add a dep, `uv sync` after
   pulls, `uv run atlas-watcher ...` to run the CLI. Don't pip-install into
   the venv directly. Don't bring in poetry, pip-tools, conda, etc.
2. **Identity invariants are tested in three places** — change all three
   when adding a forbidden term (see "Identity rules" above).
3. **Don't add new agents lightly.** The swarm size is fixed at 9 by user
   spec: 7 dclaude on specific perspectives (security, correctness,
   architecture, testing, performance, ux, evidence) + 1 codex + 1 cline
   running the shared generic prompt. If you change the count, update this
   file, `swarm.py::PERSPECTIVES`, and add/remove the corresponding
   `prompts/*.md` together.
4. **Keep specific-perspective prompts strictly scoped.** Cross-cutting
   findings are the synthesizer's job, not any single specialist's. The
   generic reviewers (codex, cline) are explicitly exempt — they should
   flag anything they find. The **evidence** reviewer is scoped too, but
   to a different axis: it judges *sufficiency of demonstration* (does the
   PR prove it works, proportionate to its size?), not code defects, and
   emits a 1–10 score the manager folds into the verdict.
5. **Issue comments must stay short and human-sounding.** The long analysis
   is internal. Resist the urge to "be helpful" by dumping it into the
   comment posted upstream.
6. **Never bypass the scrubber when posting.** `post.py` is the only route
   to `gh ... comment`. Do not call `gh` directly from `pr.py` / `issue.py`
   / anywhere else.
7. **Ledger writes must go through `ledger.append`.** Hand-written rows
   that miss the timestamp will trip schema-aware tooling later.
8. **Audit trail is sacred, and it is a different repo.** `audit.sync()`
   runs at the end of every pass and operates on `$AUDIT_DIR`, never on the
   code repo. Force-pushing to `origin/main` of *either* repo is forbidden by
   default — confirm with the user first. Never compute an audit path from
   `paths.ROOT`; use `paths.data_root()` / `reviews_dir()` / `ledger_file()`.
   Adding a new audit path means adding a function there, not a constant.
9. **No bash scripts.** If you need a new operation, add a Typer subcommand
   in `cli.py`. The shell scripts that used to live in `scripts/` are gone
   on purpose.
10. **Multi-repo invariants.** Every public function in `gh.py`, `git.py`,
    `pr.py`, `issue.py`, `post.py`, and `ledger.py` takes `repo: str` as
    its first argument. There is no module-level "current repo". When
    adding new functionality, keep it that way — never reach back to
    `settings.default_repo` from inside a processing function. The CLI
    layer is the only place that resolves "no `--repo`" → default.
11. **One persistent clone per repo.** The orchestrator clones each
    upstream repo into `workdir/<owner>/<name>/` once and reuses it
    across PRs. Never `rm -rf` a workdir as part of normal flow — only
    `git.ensure_clone` deletes (and only if the path exists but is not a
    git repo). Concurrent reviews on the same repo would race on this
    single working tree, so processing is sequential within a scan; do
    not parallelize across PRs of the same repo without adding a per-repo
    lock.
12. **Security triage is a tripwire, not an oracle.** Three rules to
    keep it useful: (a) the model verdict is *one of two signals* —
    don't drop the heuristic checks even if they look redundant, the
    model can be subverted by the very content it's classifying;
    (b) the `warning_banner` posted to upstream **must not** quote
    upstream content, leak categories beyond the severity tier, or hint
    at the agent identities — only the audit repo gets specifics;
    (c) when raising a threshold, lean toward false positives over
    false negatives — a missed clarification comment is cheap, an
    exfiltrated identity reveal is not. New forbidden phrases go into
    `_JAILBREAK_PHRASES` in `security.py` AND into the categories list
    in `prompts/security-triage.md` so the model knows to look for them.
    Note the asymmetry behind `_corroborated`: a `warn` costs a banner, a
    `block` silently drops a PR nobody hears about. So heuristics should be
    *precise* enough to block (bidi overrides, repeated prose directives)
    or explicitly marked non-decisive — don't add a broad new pattern at
    block weight. Zero-width chars are deliberately non-decisive: emoji
    ZWJ sequences made them fire on ordinary PRs.
13. **Never let the harness's own credentials reach the agents.** tmux
    sessions inherit the *tmux server's* environment, not the launching
    process's, so a stale `ANTHROPIC_API_KEY` in a long-lived tmux server
    shadows each CLI's login and 401s the whole swarm — with the harness
    still reporting "9 agents ran". Both `_write_runner` and `run_single`
    unset it. If you add a third agent-launch path, unset it there too.
14. **Never name any other repo — anywhere in this repo or its git history.**
    This codebase is public. The only repos that may appear by name are
    `sandialabs/atlas-ui-3` (the default upstream), `garland3/atlas-watcher`
    (this repo) and `garland3/ATLAS-WATCHER-LOGS` (the audit repo). Every
    other watched repo — its owner, name, PR/issue numbers, or quoted
    content — must never land in tracked files *or* in commit messages,
    branch names, tags or PR descriptions. In practice:
    - The real `REPOS` list goes in `config.local.env` (gitignored).
      `config.env` keeps only the default.
    - When a commit or comment needs an example, write "an upstream PR" or
      use the default repo — never the item that actually triggered the
      change.
    - Before every commit, grep both the staged diff and the commit message
      for the owner/repo names in `config.local.env`. A leak is not fixed by
      a follow-up commit, only by rewriting history and force-pushing
      (confirm that with the user first).

---

## Safety notes

- The watcher checks out arbitrary upstream PR code and runs AI agents over
  it. Those agents typically run with broad filesystem access (`dclaude`
  defaults to `--dangerously-skip-permissions`; `opencode` runs with
  `--auto`). Do **not** add untrusted
  upstream repos to `REPOS` — every entry there is treated as a
  trust-equivalent target.
- The bot posts as whatever GitHub user `gh` is authenticated as. The
  `AGENT-REVIEW-BOT-3` persona is a comment-body signature, **not** a
  separate GitHub account. If you want a real bot account, create one,
  authenticate `gh` as that account, and the persona naming will line up.
