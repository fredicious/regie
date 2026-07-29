# Roadmap — deferred ideas parking lot

Everything consciously deferred from v1. Nothing here is a commitment; it's the
memory of good ideas so they don't get lost. Source: 2026-07-29 brainstorming
session (see `docs/superpowers/specs/2026-07-29-agent-harness-v1-design.md`).

## v1.5

- **Web dashboard** — Next.js app over the run directories (pure reader of
  `state.json` + `events.jsonl`): real-time state-machine view, drill into any
  agent transcript, full run history. Planned as the harness's own first
  assignment: write the brief, let the harness build it.

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
