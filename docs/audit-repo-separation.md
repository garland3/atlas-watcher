# Separating the durable audit record from the atlas-watcher codebase

Status: **investigation + recommendation. No code changed, nothing committed,
nothing pushed, no visibility changed.**
Date: 2026-09-01

Goal: the durable audit/history record (`reviews/`, `ledger/`) should live in
its own Git repo with a GitHub backup, so `atlas-watcher` itself (code +
prompts + config schema) can become open-source later without dragging the
audit trail — which contains third-party (`sandialabs/atlas-ui-3`) diffs and
raw agent transcripts — into a public repo.

---

## 1. What is actually coupled today

Measured, not inferred:

| Fact | Value |
|---|---|
| `git ls-files reviews \| wc -l` | **28,641 tracked files** |
| tracked `reviews/**/.logs/*` | **7,104 files** |
| tracked `reviews/**/*.log` | **8,756 files** |
| tracked `diff.patch` (verbatim upstream code) | **405 files** |
| `du -sh reviews` | **450 MB** working tree |
| `du -sh .git` | **101 MB** |
| `git log --oneline \| wc -l` | **2,166 commits**, overwhelmingly `audit: sync …` |
| `ledger/ledger.jsonl` | 688 rows |
| tracked non-`reviews/` files | **36** (the entire actual codebase) |

So: 36 files of code are buried under 28,641 files of audit data, and the
commit history is ~99% audit noise. That alone justifies the split
independent of the open-source goal.

### Code-level coupling points (exhaustive)

Everything flows from one assumption: **the audit tree is a subdirectory of
the code repo, and the code repo's git remote is the audit backup.**

1. **`src/atlas_watcher/paths.py:16-21`** — the single root cause.
   `ROOT = repo_root()` walks up to the directory containing `pyproject.toml`
   (i.e. the *code* repo), then hardcodes
   `REVIEWS = ROOT / "reviews"` and `LEDGER_FILE = ROOT / "ledger" / "ledger.jsonl"`.
   `repo_reviews_dir()` / `pr_dir()` / `issue_dir()` all derive from `REVIEWS`.
   `WORKDIR` and `LOGS` also hang off `ROOT` (both already gitignored).

2. **`src/atlas_watcher/audit.py:8,13,21-28`** — `_git()` runs
   `git -C ROOT …`; `sync()` does `git add reviews ledger`,
   `git commit`, `git push origin main`. The audit repo *is* the code repo's
   `origin`.

3. **`src/atlas_watcher/pr.py:73,83`** — the manager agent is handed
   `` `REPO_ROOT` (audit repo working dir): `{ROOT}` `` in its runtime prompt
   and is launched with `cwd=ROOT`.

4. **`prompts/manager.md` § "Step 2 — commit and push"** — the *agent* is
   instructed to run `git add reviews ledger && git commit && git push --quiet
   origin main` itself. This is a second, agent-driven write path into the same
   repo, duplicating `audit.sync()` (which `pr.py:211` calls immediately after
   anyway). It also says "Never alter files outside `reviews/` and `ledger/`" —
   a guardrail that only exists *because* code and data share a tree.

5. **`src/atlas_watcher/ledger.py:9,21-32,42-44,144-163`** — six direct uses of
   the `LEDGER_FILE` module constant, plus `_has_terminal_evidence()` which
   reads `pr_dir()/posted.json` and `blocked.json`. This means **idempotency
   is a function of the audit tree's location**: move the tree without moving
   the ledger and the watcher re-reviews everything.

6. **`src/atlas_watcher/config.py:9,14`** — `env_file=(ROOT/"config.env",
   ROOT/"config.local.env")`. Config correctly belongs with the *code*; noted
   only because it also imports `paths.ROOT`, which constrains how `paths.py`
   may be refactored (see §4, circular-import note).

7. **`src/atlas_watcher/cli.py:84,117`**, **`pr.py:175,211`**,
   **`issue.py:133`** — five `audit.sync()` call sites. All become correct for
   free once `audit.py` points at a different directory; none need signature
   changes.

8. **`.gitignore`** — ignores `workdir/`, `logs/`, `config.local.env`. Does
   **not** ignore `reviews/` or `ledger/`.

Not coupled (good news): `swarm.py`, `security.py`, `issue.py`, `gh.py`,
`git.py`, `post.py`, `scrub.py` all receive the target directory as a `Path`
argument (`sha_dir` / `snap_dir` / `out_dir`) and never compute it. The
per-agent runner scripts write to absolute paths handed to them. So the
blast radius is genuinely `paths.py` + `audit.py` + two lines of `pr.py` +
one section of `manager.md`.

### Open-source blockers found while reading

- `reviews/**/diff.patch` (405 files) is **verbatim upstream source** from a
  third-party repo. This must never become public via this repo.
- `reviews/**/.logs/*.std{out,err}.log` are **raw agent transcripts** — they
  are never scrubbed (`scrub_identity` runs only on `summary.md`,
  `analysis.md`, `comment.md`). They contain vendor/model identity, CLI
  banners, and occasional environment strings. The largest is 869 KB
  (`PR-839/…/.logs/generic-a.stderr.log`). These are debug artifacts, not
  audit record, yet they are all committed.
- `pyproject.toml` `description` names `sandialabs/atlas-ui-3`;
  `config.env` defaults `AUDIT_REPO=garland3/atlas-watcher` and
  `REPOS=sandialabs/atlas-ui-3`. Cosmetic, but should be genericized before
  any publication.
- `.git` history already contains all of the above, so **publishing this repo
  would require history rewriting or a fresh repo regardless.** The split
  should therefore be designed so that the *code* repo is the one that gets a
  clean start.

---

## 2. Candidate architectures

### Option A — Two Git repos, data dir configured by setting (**recommended**)

- Code repo: `atlas-watcher` (this checkout), containing only `src/`,
  `prompts/`, `config.env`, docs. `reviews/` and `ledger/` become
  **gitignored** here.
- Data repo: a separate private Git repo, cloned to a path given by a new
  `AUDIT_DIR` setting (default `<ROOT>/audit-data`, gitignored). It contains
  `reviews/` + `ledger/` at its own root and has its own GitHub remote
  (`AUDIT_REPO`).
- `audit.py` runs `git -C AUDIT_DIR add reviews ledger && commit && push`.
- Manager agent stops doing git entirely; the harness's existing
  `audit.sync()` call (already on the very next line) covers it.

Tradeoffs: one new setting, one new bootstrap step (`atlas-watcher audit-init`),
operator now manages two clones. In exchange: code repo becomes ~36 files with
a clean history, audit repo stays private forever, and the "agent runs git push"
path disappears.

### Option B — Keep one working tree, push audit to a second remote via a
dedicated `git worktree` / orphan branch

Data stays physically inside the code checkout but on an orphan branch pushed
to a different remote. Tradeoffs: no new path plumbing at all, but the two
histories stay in one `.git` (101 MB and growing), so publishing the code repo
still requires surgery; branch juggling in `audit.py` is far more fragile than
a second clone; and concurrent `scan` passes could fight over the index.
**Rejected.**

### Option C — Audit record out of Git entirely (object store / rsync target)

Cheapest for size, but the requirement explicitly asks for GitHub backup, and
the ledger's `_has_terminal_evidence()` reconciliation wants a plain local
filesystem. **Rejected.**

### Sub-decision (applies to A): split *audit record* from *runtime logs*

The 7,104 `.logs/` files and the `.runners/`/`.sentinels/`/`.prompt.md`
scaffolding are **not** audit record — they are debug transcripts, they are
unscrubbed, and they are ~most of the 450 MB. Recommend the data repo carry a
`.gitignore` for:

```
**/perspectives/.logs/
**/perspectives/.runners/
**/perspectives/.sentinels/
**/perspectives/.*.prompt.md
**/*.log
```

They stay **on disk** (so `manager did not produce …; see {log_path}` debugging
still works) but stop being pushed. The durable record — `metadata.json`,
`context.md`, `diff.patch`, `perspectives/*.md`, `security.json`/`.md`,
`summary.md`, `analysis.md`, `comment.md`, `posted.json`, `blocked.json`,
`ledger.jsonl` — is what gets committed. Nothing in `ledger.py`'s
`_has_terminal_evidence()` depends on a log file, so idempotency is unaffected.

---

## 3. Recommendation

**Option A + the log/record split.** It is the only option that leaves the code
repo publishable, it is confined to two modules plus two lines of `pr.py`, and
it removes rather than adds a moving part (the agent's git push).

---

## 4. Implementation plan (exact files)

**Step 1 — `src/atlas_watcher/config.py`.** Add one setting:

```python
# Root of the durable audit record (reviews/ + ledger/). A separate Git
# repo so the codebase can be published without the audit trail.
audit_dir: str = ""     # empty -> <code repo>/audit-data
```

and a resolver property. Add `AUDIT_DIR=` (commented) to `config.env`.

**Step 2 — `src/atlas_watcher/paths.py`.** Keep `ROOT` (code root; `config.py`
imports it, so it must stay a module constant). Convert the data paths from
constants to functions:

```python
def data_root() -> Path:
    from .config import settings            # deferred: breaks the import cycle
    p = Path(settings.audit_dir).expanduser() if settings.audit_dir else ROOT / "audit-data"
    return p.resolve()

def reviews_dir() -> Path:   return data_root() / "reviews"
def ledger_file() -> Path:   return data_root() / "ledger" / "ledger.jsonl"
```

`repo_reviews_dir()` calls `reviews_dir()`. `PROMPTS`, `WORKDIR`, `LOGS` stay
on `ROOT`. **Note the deferred import** — `config.py` imports `paths.ROOT` at
module scope, so `paths.py` must import `settings` *inside* the function body.

**Step 3 — `src/atlas_watcher/ledger.py`.** Mechanical: replace the 6 uses of
the `LEDGER_FILE` constant with `ledger_file()`. No logic change.

**Step 4 — `src/atlas_watcher/audit.py`.** `_git()` targets `data_root()`
instead of `ROOT`. `sync()` gains a guard: if `data_root()/.git` is absent, log
once and return `False` rather than raising (keeps `scan` resilient on a fresh
box). Add `ensure_repo()` that `git init`s + sets the `AUDIT_REPO` remote.

**Step 5 — `src/atlas_watcher/pr.py:73,83`.** Drop `REPO_ROOT` from the manager
runtime block; set `cwd=sha_dir` (the manager only needs to read
`PERSPECTIVES_DIR` and write `SUMMARY_PATH`, both absolute).

**Step 6 — `prompts/manager.md`.** Delete "Step 2 — commit and push" and the
`REPO_ROOT` input bullet; state that the harness publishes. Renumber; adjust
the closing "print one line" example. This also removes the agent's ability to
run `git push` at all — a security improvement given the swarm reviews
untrusted upstream text.

**Step 7 — `src/atlas_watcher/cli.py`.** New `audit-init` subcommand
(per CLAUDE.md rule 9: a Typer subcommand, not a shell script) that creates
`AUDIT_DIR`, `git init`s it, writes the data-repo `.gitignore` from §2, sets
the remote, and prints next steps. Extend `atlas-watcher config` output to show
the resolved `data_root()`.

**Step 8 — `.gitignore` (code repo).** Add `reviews/`, `ledger/`, `audit-data/`.

**Step 9 — `CLAUDE.md` / `README.md`.** Update the repo-layout tree, the
"Audit trail is sacred" rule (rule 8), and the idempotency section to say the
ledger lives in the data repo.

Explicitly **out of scope** for the first change: making anything public,
rewriting history, deleting any existing data.

---

## 5. Migration plan (non-destructive, operator-run, reversible)

1. `uv run atlas-watcher audit-init` → creates the data repo skeleton.
2. **Copy, do not move:** `cp -a reviews ledger "$AUDIT_DIR"/`. The originals
   stay in the code checkout until verification passes.
3. In the data repo: `git add -A && git commit` (first commit; the `.gitignore`
   from §2 keeps logs out). Do **not** push until the operator has confirmed the
   GitHub repo exists and is **private**.
4. Verify (§6), then and only then `git rm -r --cached reviews ledger` in the
   code repo and add the gitignore entries — the working-tree copies can be
   deleted by the operator at leisure. History rewriting in the code repo is a
   separate, later decision; the cheapest path to open source is a *fresh* repo
   seeded from the post-split tree.

Rollback at any point before step 4: unset `AUDIT_DIR`, and everything resolves
back to the in-repo paths.

## 6. Verification plan

- `uv run atlas-watcher config` shows the expected resolved data root.
- **Idempotency is the thing that can silently break.** After migration,
  `uv run atlas-watcher scan --dry-run` must report `skipped:ledger` for every
  already-reviewed PR/issue. A single unexpected `Processing …` line means the
  ledger or `posted.json` lookup did not follow the move — stop and fix before
  any non-dry run. (`--dry-run` writes no ledger rows and posts nothing, so
  this probe is free.)
- `uv run atlas-watcher reconcile-ledger` must report `dropped 0` — it
  cross-checks every terminal row against `posted.json`/`blocked.json` under
  `pr_dir()`/`issue_dir()`, i.e. it is exactly an end-to-end test that the new
  paths resolve to the migrated data.
- `git -C "$AUDIT_DIR" status --porcelain` clean after a `sync`; `git -C .`
  (code repo) shows no `reviews/` churn.
- Grep the data repo's staged set for log files: `git -C "$AUDIT_DIR" ls-files
  | grep -c '\.log$'` must be `0`.
- Confirm no agent-initiated push remains: `grep -rn "git push" prompts/`
  returns nothing.
- Existing scrub invariants untouched — no change to `scrub.py` is proposed.

## 7. Proposed next-step scope (one change)

Steps 1–8 above, as a single uncommitted working-tree change (~120 lines net,
touching `config.py`, `paths.py`, `ledger.py`, `audit.py`, `pr.py`, `cli.py`,
`prompts/manager.md`, `.gitignore`, `config.env`), plus the doc updates in
step 9. Default `AUDIT_DIR` resolves inside the current checkout, so **behavior
is unchanged until the operator sets it** — the migration is opt-in and the
diff can be reviewed with the data still in place.

## 8. Open questions for the operator

1. Name/owner for the private data repo (`AUDIT_REPO` currently
   `garland3/atlas-watcher`, which is the *code* repo — the two will need
   different values).
2. Keep `diff.patch` in the durable record? It is verbatim third-party source
   and reconstructible from the upstream SHA. Dropping it would cut the record
   substantially, at the cost of offline reproducibility.
3. Is dropping the manager's git step acceptable? It is redundant with
   `audit.sync()` today, but it does mean a crash between manager and sync
   leaves the commit for the next pass rather than the current one.
4. For eventual open-sourcing: fresh repo (clean history, loses 2,166 commits)
   or filter-repo on this one? Recommend fresh.
