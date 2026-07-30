# Using Régie

## What works today (Plan B complete)

The full pipeline runs end-to-end: **brief in → spec (planner agent) → your
approval → tasks (TDD with author separation, cross-model review) → finalize →
squashed, pushed PR with CI watch and debugger rounds**. Everything is verified
by a fake-adapter end-to-end suite (109 tests); the **real Claude/Codex
adapters are wired but not yet validated against live CLI output** — do the
supervised smoke test below before trusting an unattended run.

## Install

No Python knowledge needed — [uv](https://docs.astral.sh/uv/) manages everything:

```bash
cd ~/Code/regie && uv tool install --editable .
regie --help
```

`uv tool install` puts a `regie` command on your PATH with its own isolated
environment. When published to PyPI this becomes `uv tool install regie`.

## One-time setup per target repo

Create `regie.toml` at the target repo's root:

```toml
test_globs = ["tests/**", "apps/*/tests/**", "**/*.test.*"]
eval_trigger_globs = ["apps/api/**"]                       # optional
binding_strength = ["codex:gpt-5.x", "claude:strongest"]   # weakest → strongest
base_branch = "main"                                       # optional, default main

[commands]
test = "make test"        # gates call ONLY these commands
lint = "make lint"
typecheck = "make typecheck"   # optional
eval = "make eval"             # optional, runs when eval_trigger_globs match
```

**The target repo needs a proper `.gitignore`** (at minimum `__pycache__/`,
caches, build artifacts): Régie stages with `git add -A`, so anything a gate
command generates and the repo doesn't ignore will be committed — the first
smoke test shipped `.pyc` files into its PR this way.

Agent profiles (role prompts + model bindings + budgets) live in this repo's
`profiles/`: planner, test-writer, builder, reviewer, debugger. The `.md` files
are the practice documents — edit them to bake in your coding values; every
attempt records the prompt hash so changes are measurable against outcomes.

## Running

```bash
# 1. Write a brief: goal, in scope, OUT of scope, acceptance signals
$EDITOR feature.md

# 2. Start — Régie pins origin/main, creates an isolated worktree + branch
#    (regie/<run-id>), and dispatches the planner
regie run feature.md --repo ~/Code/ai-search-platform

# 3. It stops at the approval checkpoint: read the spec it printed, then
regie approve 2026-07-30-feature
regie resume 2026-07-30-feature --repo ~/Code/ai-search-platform

#    (or skip the checkpoint entirely — earn this once specs prove good:)
regie run feature.md --repo ... --autonomous

# 4. Watch / inspect
regie status 2026-07-30-feature
tail -f ~/.regie/runs/2026-07-30-feature/events.jsonl | jq

# 5. On halt (budgets exhausted, blocked question, quota, rebase conflict):
#    read the reason, fix what it names, then
regie resume 2026-07-30-feature --repo ...

# 6. Cleanup after a run you're done with (keeps the run dir for the record)
regie clean 2026-07-30-feature --repo ...
```

Per task: **test-writer** (tests must fail honestly — assertion or
NotImplementedError, verified) → **builder** (tests/lint/typecheck green, and a
git-diff guard proves it never touched a test file) → **reviewer** (always the
other model family; blocker/major findings loop back, minors go to the PR as
review notes). 3 attempts per stage (2 on the binding, 1 escalated), then halt.
Finalize runs the full gates once and rebases onto fresh origin/main. The PR
stage squashes to one commit per task (backup ref + tree-identity check first),
pushes, opens the PR via `gh`, watches CI, and dispatches up to 2 gated
debugger rounds if red. Halts and completion fire a desktop notification.

## The run directory

`~/.regie/runs/<run-id>/` (override root with `$REGIE_HOME`) — outside the
target repo, so transcripts can never be committed:

```
state.json        # authoritative state — regie status pretty-prints it
events.jsonl      # append-only observability log (dispatches, gates, usage)
intent.jsonl      # write-ahead log powering crash-safe resume
brief.md  spec/spec.md  decisions.md  pr-body.md
tasks/T1/         # context.md (exact packet sent), attempt-N.out (transcripts),
                  # note-*.md (gate failures / findings fed to retries),
                  # findings.json / minor-findings.json
tasks/PLAN/  tasks/SCRIBE/  tasks/DEBUG-1/   # pseudo-task artifacts
```

Worktrees live under `~/.regie/worktrees/<run-id>` — your checkout is never
touched. One live run per target repo is enforced.

## First real run (supervised smoke test) — do this before trusting it

The adapters implement the CLI contracts documented mid-2026; real output may
have drifted. On a **sandbox repo** (not ai-search-platform):

1. Write a trivial brief (one tiny function) and a `regie.toml` with fast
   commands. Run `regie run … ` WITHOUT `--autonomous`.
2. Verify the planner's real `claude -p` output parsed: `regie status` shows
   stage "approve", `spec/spec.md` exists, tasks look sane. If the run halts
   with parse errors, compare `tasks/PLAN/attempt-1.out` against
   `src/regie/agents/claude.py`'s expected fields and adjust.
3. Approve, resume, and watch `events.jsonl`: each dispatch should show real
   turns/usage numbers (Claude) — zeros mean parsing drifted.
4. Confirm the diff guard fires if you ask for it (edit a test in the brief's
   scope deliberately) and that the PR lands with squashed per-task commits.
5. Check quota behavior once you hit a limit organically: the run should halt
   cleanly with a resumable state, not burn retries.
6. For ai-search-platform specifically: add the `make test-api`-style canonical
   targets first, and time the full suite before choosing gate commands.

## Known v1 limits

`--tasks-file <json>` bypasses the planner (testing escape hatch). Codex
exposes no usage telemetry in exec mode. Provider quota *failover* (vs clean
halt) and local models are roadmap items — see `docs/ROADMAP.md`.

Two more real-`gh` races to validate during the smoke test (both known v1
gaps, unexercised by the fake-adapter suite):

7. Immediately after `gh pr create`, `gh pr checks` may report no checks yet
   at all — Régie's `ci_status` currently reads an empty check list as
   green. Run the smoke test against a repo **with CI configured** and watch
   the first `_ci_loop` poll right after PR creation; if it reports "done"
   before any check has actually started, a grace period (e.g. requiring at
   least one non-empty poll, or a short delay before the first poll) needs to
   be added before this is safe unattended.
8. (Also a known crash-window gap, parked with ruling at final review: a hard
   process kill between the PR stage's push and its state write leaves
   `pushed=false`; `regie resume` then errors on the non-fast-forward push
   instead of halting cleanly. Recoverable by hand; fix candidate: WAL intent
   before push or an idempotent branch/PR probe on re-entry.)
9. After a debugger-round push, `gh pr checks` may still report the
   pre-fix commit's stale `FAILURE` states for a beat before CI re-triggers
   on the new sha — watch for a round being "burned" twice (halting sooner
   than `CI_MAX_DEBUG_ROUNDS` should allow) because a stale red was read as
   the fix's own result.
