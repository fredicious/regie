# Roadmap — deferred ideas parking lot

Everything consciously deferred from v1. Nothing here is a commitment; it's the
memory of good ideas so they don't get lost. Source: 2026-07-29 brainstorming
session (see `docs/superpowers/specs/2026-07-29-agent-harness-v1-design.md`).

## v1.5

- **Web dashboard** — Next.js app over the run directories (pure reader of
  `state.json` + `events.jsonl`): real-time state-machine view, drill into any
  agent transcript, full run history. Planned as the harness's own first
  assignment: write the brief, let the harness build it.

## Moved out of v1 by the 2026-07-29 adversarial review

- **Parallel task execution** — per-task worktrees, integration branch, file-scope
  overlap serialization, merge handling. v1 is serial in one worktree; the task
  DAG already exists, so parallelism is additive.
- **Complexity-based routing** (`profile × complexity → binding`) — replaced in v1
  by the escalation ladder (evidence beats prediction).
- **Automated `blocked`-question routing** — v1 halts and notifies instead of
  machine-mediated Q&A dispatch.
- **`harness watch` TUI** — v1 ships `regie status` + `tail -f events.jsonl | jq`.
- **Six-profile roster** — v1 merges spec-er+decomposer into planner and makes
  debugger a builder variant; split again if telemetry says so.
- **Autonomous-by-default** — v1 defaults to a human spec checkpoint
  (`regie approve`), with `--autonomous` to earn back once specs prove reliable.

## v1.x — DX (user-confirmed 2026-07-30)

- **Spec lands in the PR (must-have)** — the PR stage writes the run's spec
  into the target repo as `specs/<run-id>.md` (committed before the squash, or
  as its own commit), so every Régie PR carries its own spec and the reviewer
  sees intent + implementation in one diff. Mirrors the ARTE `specs/`
  convention.
- **`regie spec <run>` / `regie open <run>`** — print or open run artifacts
  (spec, status, transcripts) without knowing the hidden `~/.regie` layout.
  General principle: no workflow step should require navigating a dotfolder.

## Surfaced by the overnight dogfood run (2026-07-30/31)

Fixed inline (twelve harness bugs; see git log): inline `--json-schema`, strict
`PLAN_SCHEMA`, scribe count contract, `.gitignore` prerequisite, planner
process-step decomposition, empty-commit crash, TDD-red whitelist (→ minimal
contract), spent-escape-never-reset, `stream-json` for the stall detector, WAL
reset markers, blocked/review leftover discards, budget-death naming.

Still open, worth briefs:
- **Gate runner must check exit codes, not tail output.** A ruff F841 shipped
  to main and reddened CI because the overnight process eyeballed `tail -1`
  ("No fixes available") instead of the non-zero exit. Régie's own gates DO
  check exit codes; the lesson is for any human/agent operating around it —
  but a `regie preflight` command (run the repo's gate commands, report
  pass/fail by exit code) would make this impossible to get wrong.
- **Re-plan on stale base.** A long run whose `main` moved underneath halts on
  a rebase conflict at the PR stage; today that needed hand-resolution. Option:
  detect base drift at finalize and offer a re-plan-against-current-main path
  rather than only halting.
- **Worktree-per-writer + real process supervision for the *builder* of Régie**
  (the meta-orchestration around dogfooding): tonight's subagents collided in
  one worktree and "stand-down" was a polite message, not a kill. Régie itself
  already solves this for its agents; the harness *building* Régie should too.
- **`max_turns`/budget as a first-class outcome** (partially done — named now;
  make it its own `Attempt.outcome` with budget-aware escalation).

## Done 2026-07-31 (features 2-4)

- **`regie preflight`** — runs the repo's gate commands, verdict strictly by
  exit code. Shipped.
- **Idempotent finalize rebase** — a manually-resolved rebase resumes cleanly;
  conflict halts name the files + base-drift count. Shipped.
- **Codex adapter validated live** (codex-cli 0.146.0). Corrected three
  doc-based bugs: usage telemetry IS on `turn.completed`; `turn.failed`
  handled; `stdin=DEVNULL` in dispatch (codex blocks on stdin otherwise).
  Cross-vendor run proven end-to-end: claude plans/tests/reviews, codex
  builds → clean PR. Note: `gpt-5-codex` is rejected on ChatGPT auth; use the
  account's real model (e.g. `gpt-5.5`). Shipped.

## v2

- **Provider failover on quota exhaustion** — profiles already model `fallback`
  bindings; wire it: catch the CLI quota error, flip binding, `harness resume`.
  Lets a run finish on provider B when provider A hits its session limit instead
  of waiting for the window to reset.
- **Local / OSS models** — third binding target via opencode/Ollama; slot exists
  in the profile schema. Candidate first use: trivial-complexity tasks and Red
  reviewers (cheap diversity).
- **Drive modes** — config flag per run: `autonomous` (v1 default) vs
  `checkpointed` (human gates at spec approval and/or pre-PR). Preference is
  autonomous; the flag exists for higher-stakes changes and for other users.
- **Separate Blue-team agent** — v1 folds triage into the reviewer's structured
  output; experiment with an independent triage agent (different model) and
  measure whether verdicts change.
- **Per-project workflow selection** — the spec-er emits the workflow (stages,
  parallelism, profiles) as data; the router executes it dumbly. The v1 pipeline
  is already modeled as a DAG of stages to make this a config change, not a
  rewrite.
- **Per-app builder flavors** — the target monorepo is polyglot (FastAPI +
  Next.js + eval harness); builder/test-writer profiles may split into per-stack
  variants with tailored prompts and gates.
- **Jira intake** — Stage 0 accepts a Jira ticket (ARTE workflow; tickets are in
  French — intake includes translation/normalization into a brief).
- **Telemetry-driven routing** — turn run telemetry (retry counts, review
  findings, CI failures per profile×binding) into periodic evidence-based
  reassignment of profile bindings. The "each task to the best tool" promise,
  grounded in own data.
- **Per-task skill injection** — the planner tags tasks with needed capabilities;
  the harness attaches matching agent skills to the dispatch (Claude Code skills
  dir / marketplace like skills.sh). Security first: curated allowlist with
  pinned versions only — never auto-install from open search into agents holding
  write access (supply-chain surface).
- **Conflict-resolver agent ("git surgeon")** — v1 halts on rebase conflicts;
  a cheap-model agent could own conflict resolution (judgment work), while all
  mechanical git stays orchestrator-scripted (agents for judgment, scripts for
  mechanics).
- **Quota-aware scheduling** — beyond failover: route proactively based on
  remaining quota per provider (e.g. drain Codex before touching Claude limits
  late in the day).

## v3+

- **Open-source release** — generic packaging: configurable workflows/profiles,
  docs, multi-repo support, the web dashboard grown into a workflow/profile
  editor UI ("define your own workflows/agents").
- **Rust rewrite** — only if the OSS/product path demands single-binary
  distribution and the design has stabilized. Explicitly a nice problem to have.
- **Mutation testing as a gate** — score test-writer output by mutation kill
  rate instead of trusting green suites; expensive, revisit when the basics are
  boring.
