# Roadmap — deferred ideas parking lot

Everything consciously deferred from v1. Nothing here is a commitment; it's the
memory of good ideas so they don't get lost. Source: 2026-07-29 brainstorming
session (see `docs/superpowers/specs/2026-07-29-regie-v1-design.md`).

## v1.5

- **Web dashboard** — Next.js app over the run directories (pure reader of
  `state.json` + `events.jsonl`): real-time state-machine view, drill into any
  agent transcript, full run history. The terminal control room now covers the
  local operator experience; a web version would target remote/shared access.

## Reconsidered after the 2026-07-29 adversarial review

- **Parallel task execution** — shipped 2026-08-17: dependency-layer fan-out,
  task worktrees, predicted-scope overlap serialization, stable integration,
  and conflict halts.
- **Complexity-based routing** (`profile × complexity → binding`) — replaced in v1
  by the escalation ladder (evidence beats prediction).
- **Automated `blocked`-question routing** — v1 halts and notifies instead of
  machine-mediated Q&A dispatch.
- **`harness watch` TUI** — superseded by the shipped `regie` control room; the
  plain `status` and JSONL interfaces remain automation-friendly fallbacks.
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

## Done 2026-08-14 — Token Governor

- Provider-normalized token/cache/reasoning/tool-output/cost telemetry and
  `regie stats --tokens`, including failed attempts.
- Stage-specific progressive-disclosure context packets; full artifacts stay
  on disk and are referenced instead of copied into every attempt.
- Per-profile effort, tool, sandbox, context, tool-result, and review-output
  policies for Claude, Codex, and direct API bindings.
- Deterministic PR copy and narrow agent-side validation; authoritative gates
  remain unchanged.
- Risk-triggered security, migration, API, UI, and architecture reviewers.
- Failure classification/signatures and immediate escalation after turn,
  stall, or wall-budget deaths.
- Optional dependency-free `openai-api` Responses adapter with bounded local
  repository tools. Live account smoke test remains operator-owned.
- Global, concurrency-safe quota circuits for Claude Code and Codex: reset-time
  parsing, provider-wide cross-run failover, synthetic no-call skips, half-open
  recovery probes, debugger/reviewer fallback, and operator status/reset CLI.

## Done 2026-08-17 — adaptive orchestration expansion

Materially inspired by [MetaSwarm](https://github.com/dsifry/metaswarm); see
`docs/ACKNOWLEDGEMENTS.md`.

- Deterministic repository research and selective external project knowledge.
- Auto/fast/standard/critical workflow tiers with explicit cost circuits.
- Mechanical plan preflight; independent feasibility, completeness, and
  scope/alignment reviewers; selective security, UX, and architecture design
  reviewers.
- Task risk tags, external-dependency declarations, planned checkpoints, and
  criterion-by-criterion review evidence.
- Configurable exit-code gate plugins, including changed-path and tier triggers.
- Parallel DAG layers in isolated worktrees with overlap serialization and
  deterministic integration.
- Final cross-task integration review, persistent PR lifecycle state, review
  feedback fix rounds, reflection candidates, explicit knowledge promotion,
  guided `regie init`, provider readiness reporting, and generated handoffs.
- Parent/child run linkage as the durable foundation for recursive,
  multi-repository orchestration.

## Done 2026-08-17 — terminal control room

- Bare `regie` launches a live Textual application; every existing subcommand
  remains available for scripts and CI.
- In-app brief composer and run launcher with repository, workflow-tier, and
  autonomous-mode controls.
- On-demand historical run picker, expanded dependency/task status table,
  acceptance-evidence detail, live event ledger, provider health, normalized
  usage/cost, and PR-shepherd telemetry. Compact telemetry lives in the top
  overview so the bottom is reserved for two wider scrolling evidence panes.
- Lock-safe approval and resume actions backed by the same durable transitions
  and CLI engine used outside the interface.

## Done 2026-08-17 — bounded Product Owner recovery

- Three rejected reviewed plan drafts invoke one read-only Product Owner agent
  before Régie halts.
- Structured `revise`, `accept`, `ask_human`, and `halt` decisions are persisted
  as run evidence and projected in Agent Activity and the artifact browser.
- `revise` grants exactly one additional planner contract attempt; `accept` is
  limited to scope/alignment findings and cannot waive deterministic checks or
  mandatory technical reviews.
- The state machine remains the sole orchestration authority and provider
  failover remains deterministic.
- In-app Markdown/JSON artifact reader for briefs, specs, research, checkpoints,
  handoffs, PR bodies, and reflection candidates.
- Managed background engine processes keep the interface responsive and retain
  a diagnostic log under `$REGIE_HOME`.

## v2

- **Local / OSS models** — third binding target via opencode/Ollama; slot exists
  in the profile schema. Candidate first use: trivial-complexity tasks and Red
  reviewers (cheap diversity).
- **Policy-driven custom workflow compiler** — tiers and risk-selected stages
  now ship; the remaining step is allowing projects to define arbitrary stage
  graphs rather than the bounded built-in compiler.
- **Separate Blue-team agent** — v1 folds triage into the reviewer's structured
  output; experiment with an independent triage agent (different model) and
  measure whether verdicts change.
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
