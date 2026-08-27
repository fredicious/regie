# Using Régie

## What works today

The default low-risk pipeline runs end-to-end as **brief → one direct owner →
mechanical gates → focused independent review → squashed PR**. When policy or
evidence requires it, Régie expands to **repository research → reviewed spec/task
DAG → approval → dependency-aware TDD tasks → integration review → PR shepherd**.
Claude and Codex adapters have both been exercised live; every new
target repository should still start with the supervised smoke test below.

## Install

No Python knowledge needed — [uv](https://docs.astral.sh/uv/) manages everything:

```bash
cd ~/Code/regie && uv tool install --editable .
regie
```

`uv tool install` puts a `regie` command on your PATH with its own isolated
environment. When published to PyPI this becomes `uv tool install regie`.

## The control room

Run `regie` with no arguments to enter the terminal application. The control
room is scoped to the folder where it was launched: it opens that repository's
most recently updated run and does not mix in runs from other projects. On the
first launch without a `regie.toml`, Régie enters setup mode. It detects the
project language and proposes test, lint, typecheck, build, coverage, and visual
gate settings. It also shows Claude Code and Codex CLI readiness and lets you
enable either or both providers. Review or edit the choices, then **Save &
continue** to create the configuration. Régie then focuses the in-app product
brief editor. Type the brief and launch without first creating an input file;
the run name is optional and is inferred from the brief's first meaningful line.

- `N` opens the same product brief composer at any time and lets you select the
  repository/workflow tier.
- `L` opens the repository-scoped run picker. Run history stays out of the main
  dashboard until it is needed; selecting a row switches the control room.
- The dependency table shows task state, stage, prerequisites, attempts, and
  title; selecting a task shows its scope, risks, criteria, and review evidence.
- The top overview keeps run identity/progress on the left and compact lifetime
  usage, PR/CI state, provider readiness, and quota telemetry on the right.
  Token usage separates fresh/cache-write/output work from cached-input reads;
  Agent Activity exposes both values per attempt.
- The two full-width lower panes stream the event ledger and a live Agent
  Activity table. Every
  planner, Product Owner recovery advisor, plan reviewer, test writer, builder,
  specialist, integration reviewer, and debugger attempt is shown separately
  with provider, model, state, runtime, output heartbeat, turns, and tokens.
  Both panes receive enough horizontal room for their detailed columns.
- `A` approves the current spec or reached checkpoint; `S` resumes the selected
  run through the normal lock-protected CLI engine.
- When a direct owner finds a material ambiguity, the run exposes its question
  as a clarification halt. Press `C`, answer inside the control room, and Régie
  records the decision before resuming the owner. Questions are reserved for
  choices that would materially change the implementation.
- `O` reads the brief, accepted spec, research, checkpoint, handoff, PR body, and
  knowledge candidates inside Régie.
- `P` enables or disables Claude and Codex for the current project. The change
  applies to the next run or resume; an already-active attempt keeps its route.
- `R` refreshes immediately and `Q` exits. The application also refreshes once
  per second. These actions, including **Runs**, are available from the command
  palette as well.

Engine work runs in a managed child process so planning/building never freezes
the interface. Failures are summarized in the event ledger, while full output
is retained in `$REGIE_HOME/control-room.log`; the
authoritative state and event ledgers remain the source of everything displayed.
Desktop notifications are suppressed for child runs while the control room is
visible, avoiding duplicate UI and operating-system alerts.

All subcommands remain available for shell scripts, CI, recovery, and direct
automation. `regie watch <run-id>` opens the same control room focused on a
specific run even when it belongs to another workspace; `regie --help` lists
the complete command surface.

## One-time setup per target repo

The first-run control-room setup handles this interactively. For scripts, run
`regie init --repo <path>` to detect the repository's language, package manager,
tests, lint, types, build, coverage, and UI tooling. Both paths create a starter
`regie.toml`; review and commit it with the project. You can also write it
manually:

```toml
test_globs = ["tests/**", "apps/*/tests/**", "**/*.test.*"]
eval_trigger_globs = ["apps/api/**"]                       # optional
base_branch = "main"                                       # optional, default main

[workflow]
default_tier = "auto"       # auto | fast | standard | critical
direct_execution = true     # auto may start with one owner and no planner
max_parallel_tasks = 3      # independent DAG tasks get isolated worktrees
plan_reviews = true
design_reviews = true
final_review = true
knowledge = true
reflection = true
max_task_usd = 0             # 0 disables this cost circuit
max_run_usd = 0

[commands]
setup = "pnpm install --frozen-lockfile"  # before the first agent in each worktree
test = "make test"        # gates call ONLY these commands
lint = "make lint"
typecheck = "make typecheck"   # optional
eval = "make eval"             # optional, runs when eval_trigger_globs match
coverage = "make coverage"     # standard/critical task + final gate
build = "make build"           # optional mechanical gate

[providers]
enabled = ["claude", "codex"]  # either provider may be disabled per project

[gates.visual]
command = "npx playwright test"
stages = ["finalize"]
trigger_globs = ["apps/web/**/*.tsx", "apps/web/**/*.css"]
tiers = ["standard", "critical"]
```

The shipped role profiles use cross-provider ladders: Codex leads direct ownership,
test writing, implementation, and debugging; Claude leads planning and most reviews.
Both are enabled by default for quota failover and independent review. Disabling
one filters it from every role ladder; Régie rejects a configuration if that
would leave any required role without a viable binding. Press `P` in the control
room to change the project preference without editing TOML.

Failover applies to the entire workflow, including planning panels, specialist
reviews, and final integration review—not only the main planner/build stages.
When a provider circuit is already open, Agent Activity records the bypass as
`skipped`; this is an audit entry and does not launch another provider process.
Planner drafts receive up to three contract-validation attempts independently of
provider failover. If those reviewed drafts still do not converge, the shipped
`product-owner` profile receives the brief, latest plan, validation failures,
review findings, and attempt evidence. It can issue one binding set of planner
directives, accept only scope/alignment review findings, or escalate to the operator.
Its decision is retained as `product-owner-decision.json` and `.md` and appears
in the control room. A final failed revision halts; deterministic validation,
tests, security/destructive checkpoints, provider configuration, and budgets
cannot be overridden by the Product Owner.

Any `[gates.<name>]` entry is a deterministic plugin gate. It runs only at its
declared stages, tiers, and changed-path triggers; its process exit code is the
verdict. This supports visual regression, security scanning, API compatibility,
migration checks, mutation tests, or repository-specific evals without teaching
the engine a framework.

**The target repo needs a proper `.gitignore`** (at minimum `__pycache__/`,
caches, build artifacts): Régie stages with `git add -A`, so anything a gate
command generates and the repo doesn't ignore will be committed — the first
smoke test shipped `.pyc` files into its PR this way.

Agent profiles (role prompts + model bindings + budgets) live in this repo's
`profiles/`: planner, Product Owner, test-writer, builder, reviewer, debugger,
and specialist reviewers. Each profile's `.yaml` carries an ordered `bindings:`
list (retry escalation and quota failover walk it in order, weakest/cheapest
first) — a one-element list is fine for a profile that never escalates:

```yaml
bindings:
  - { cli: claude, model: sonnet, auth: subscription }
  - { cli: claude, model: opus, auth: subscription }
budgets: { turns: 40, wall_minutes: 25, stall_minutes: 5 }
token_policy:
  effort: medium
  tools: [list, read, search, patch, shell]
  sandbox: workspace-write
  context_chars: 12000
  tool_result_chars: 12000
```

`token_policy` is optional. It controls reasoning effort, exposed tools,
read-only versus writable execution, stage-packet size, tool-result size, and
review-output bounds. Stable role instructions are sent separately from the
dynamic packet so providers can cache the stable prefix more effectively.

### Optional direct OpenAI API binding

Set `OPENAI_API_KEY`, then use `cli: openai-api` in a profile binding:

```yaml
bindings:
  - { cli: openai-api, model: gpt-5.6-terra, auth: api }
```

This dependency-free Responses API adapter exposes bounded repository tools:
file listing, paged reads, search, validated `git apply` patches, and only the
exact checks declared in `regie.toml`. It does not expose unrestricted
model-authored shell commands. Subscription and API bindings can coexist in
one escalation ladder. Live API access and model availability still require a
supervised smoke test for the configured account.

The legacy singular `binding:` key is still read when `bindings:` is absent,
expanding to a one-entry list. The `.md` files are the practice documents —
edit them to bake in your coding values; every attempt records the prompt hash
so changes are measurable against outcomes.

Quota failures activate a global circuit breaker under `$REGIE_HOME`. Claude
Code and Codex reset timestamps are retained when their CLI reports them;
otherwise Régie uses a conservative provider/auth-specific cooldown. Every run
consults the shared state before dispatch, skips all unavailable same-account
model rungs, and tries the next configured provider without spending a call.
After the reset, one run receives a half-open recovery probe while other runs
continue using fallbacks. Direct API bindings remain opt-in profile entries and
are never added automatically, because they can incur usage-based charges.

## Running

The control room is the primary interactive workflow. The equivalent explicit
commands are:

```bash
# 1. Write a brief: goal, in scope, OUT of scope, acceptance signals
$EDITOR feature.md

# 2. Start — Régie pins origin/main, creates an isolated worktree + branch
#    (regie/<run-id>), and dispatches the planner
regie run feature.md --repo ~/Code/ai-search-platform
regie run feature.md --repo ... --workflow critical  # explicit rigor tier

# 3. It stops at the approval checkpoint: read the spec it printed, then
regie approve 2026-07-30-feature
regie resume 2026-07-30-feature --repo ~/Code/ai-search-platform

#    (or skip the checkpoint entirely — earn this once specs prove good:)
regie run feature.md --repo ... --autonomous

# 4. Watch / inspect (no dotfolder spelunking needed)
regie status 2026-07-30-feature
regie spec 2026-07-30-feature       # print the spec
regie open 2026-07-30-feature       # list all artifact paths
regie doctor 2026-07-30-feature     # diagnose a halt + suggested action
regie handoff 2026-07-30-feature    # durable human/new-session continuation
regie knowledge-approve 2026-07-30-feature  # promote reviewed learnings
regie providers                      # binary/key readiness for configured bindings
regie stats                          # cross-run binding telemetry + suggestions
regie stats --tokens                 # new/cache/output/reasoning/tool bytes/cost
regie provider-status                # unavailable/probing/reset state across all runs
regie provider-reset claude --auth subscription  # manual recovery override
tail -f ~/.regie/runs/2026-07-30-feature/events.jsonl | jq

# 5. On halt (budgets exhausted, blocked question, quota, rebase conflict):
#    read the reason, fix what it names, then
regie resume 2026-07-30-feature --repo ...

# 6. Cleanup after a run you're done with (keeps the run dir for the record)
regie clean 2026-07-30-feature --repo ...
```

Before tasks, Régie builds bounded `research.json`/`research.md`, validates the
plan mechanically, then runs independent feasibility, completeness, and
scope/alignment reviews. Security, UX, and architecture design reviewers join
only when plan risks justify them. The accepted plan resolves `auto` into:

- `fast` for a single low-risk change;
- `standard` for ordinary multi-task work;
- `critical` for security, migrations, external dependencies, or hard tasks.

Per task: **test-writer** (tests must fail honestly — assertion or
NotImplementedError, verified) → **builder** (tests/lint/typecheck green, and a
git-diff guard proves it never touched a test file) → **reviewer** (always the
other model family; blocker/major findings loop back, minors go to the PR as
review notes). Security, migration, public-API, UI/accessibility, and
architecture reviewers activate only when deterministic task/diff triggers say
their expertise is relevant. Normal stages get two same-binding chances before
escalation; a wall, stall, or turn-budget death escalates immediately instead
of repeating the same doomed envelope.

Packets are stage-specific: criteria, planned tests, predicted file scope, a
review change manifest, and only relevant decision/convention paragraphs are
inline. Full spec and repository-rule artifacts remain linked for progressive
disclosure. Agents run narrow checks while working; Régie still runs the
authoritative full gates. Commit messages and PR copy are derived
deterministically, so finalization does not spend a planner call.

Independent tasks at one DAG depth run concurrently only when their predicted
file scopes do not overlap and all declare `parallel_safe`. Each gets its own
branch/worktree. Passing commits converge in stable task-ID order; integration
conflicts halt rather than being guessed through. Task checkpoints planned for
schema, security, external-service, or irreversible boundaries are true blocking
states and use the same `regie approve` command as spec approval.

Finalize runs the full gates once and rebases onto fresh origin/main. The PR
stage squashes to one commit per task (backup ref + tree-identity check first),
commits the run's spec into the repo as `specs/<run-id>.md`,
pushes, opens the PR via `gh`, and persists a PR shepherd state covering CI,
review decisions, unresolved threads, bounded fix rounds, ready, and merged.
After readiness it extracts candidate decisions/gotchas/anti-patterns; candidates
remain outside the repository until explicitly promoted. Halts and completion
fire a desktop notification. Set `REGIE_NOTIFICATIONS=0` to suppress them for a
shell or CI process. Régie's own test suite sets this automatically so tests can
never produce real operating-system notifications.

## The run directory

`~/.regie/runs/<run-id>/` (override root with `$REGIE_HOME`) — outside the
target repo, so transcripts can never be committed:

```
state.json        # authoritative state — regie status pretty-prints it
events.jsonl      # append-only observability log (dispatches, gates, usage)
intent.jsonl      # write-ahead log powering crash-safe resume
brief.md  research.json  research.md  knowledge-prime.md
spec/spec.md  decisions.md  checkpoint.md  handoff.md  pr-body.md
tasks/T1/         # context.md (exact packet sent), attempt-N.out (transcripts),
                  # note-*.md (gate failures / findings fed to retries),
                  # findings.json / minor-findings.json / criterion-evidence.json
tasks/PLAN/  tasks/DEBUG-1/  tasks/T1-SECURITY-REVIEWER/  # pseudo-task artifacts
```

The primary worktree lives under `~/.regie/worktrees/<run-id>`; parallel task
worktrees are temporary children of the run directory. Your checkout is never
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
5. Check quota behavior once you hit a limit organically: `provider-status`
   should show its reset and the run should continue on its next configured
   provider without spending calls on other models from the exhausted account.
6. For ai-search-platform specifically: add the `make test-api`-style canonical
   targets first, and time the full suite before choosing gate commands.

## Known limits

`--tasks-file <json>` bypasses research and plan review (testing escape hatch).
Parent/child run links are durable, but a full multi-repository epic scheduler
is not yet built. Local models remain a roadmap item. Reset timestamps depend
on provider CLI output; when a timestamp is absent, Régie probes after a
conservative cooldown rather than
claiming exact knowledge of the provider's window.

Real-`gh` races to validate during the smoke test:

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
