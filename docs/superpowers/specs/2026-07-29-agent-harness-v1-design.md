# Agent Harness v1 — Design

**Date:** 2026-07-29
**Status:** Draft for review
**Author:** Frédéric Cabassut + Claude (brainstorming session)

## Problem

Seven months of agent-assisted coding surface one dominant pain: **the human is the
orchestrator.** Tell model A to build, ask model B to review the PR, feed the review
back to model A, referee the nitpicks, repeat. Every existing harness tried
(Conductor, Nymbalist, opencode) automates the *agent*, not the *workflow between
agents*.

**Goal:** product brief in → reviewed, tested, CI-green PR out — with no human in the
loop between those two points. Multi-model by design: each task done by the best
tool, dispatched automatically.

## Users and scope

- **Primary (v1):** Frédéric, daily driver on `ai-search-platform` (ARTE) —
  feature-sized specs in an existing polyglot monorepo (FastAPI + Next.js + eval
  harness). Spec in, PR out.
- **Later (v2+):** other repos, other users, possible open-source release with
  configurable workflows/profiles and a web UI.

## Non-goals (v1)

- Dynamic/re-planning workflows — one fixed pipeline, workflow modeled as data (a
  DAG of stages) so per-project workflows are a config change later, not a rewrite.
- Local/OSS models (Ollama via opencode) — modeled in the profile schema, not wired.
- Provider failover on quota exhaustion — modeled (fallback bindings), wired in v2.
- Infra agent — deploy is already `merge → CI`.
- Web dashboard — v1.5 (see Observability).
- A messaging bus or "context holder" agent — see Cross-agent context.

## Core principles

1. **Deterministic first.** Everything checkable by a command (tests, lint,
   typecheck, build, schema validation, diff guards) is a hard gate. Agent judgment
   is layered on top, never instead.
2. **Spec-driven.** No code without a spec that passed its gates. Specs use the
   repo's existing OpenSpec format.
3. **TDD with author separation.** Tests are written (and must fail) before
   implementation, by a different agent than the builder. The builder is
   mechanically blocked from editing test files.
4. **Cross-model review.** The reviewer is always a different model family than the
   builder. Shared blind spots are the enemy.
5. **Bounded everything.** Every loop has a retry budget and an escalation ladder;
   every agent has turn/time budgets. Runs halt and notify rather than spin.
6. **Everything on disk.** A run is a directory. State, prompts, transcripts, gate
   results, events — all inspectable, all resumable.

## Architecture overview

A standalone Python CLI (`harness`) — no orchestration framework. The orchestrator
is a state machine + subprocess manager. Agents are vendor CLIs run headless
(`claude -p --output-format json`, `codex exec`), each in an isolated git worktree.
The harness's IP is the workflow, profiles, context packets, and gates — not agent
plumbing: each vendor CLI brings its own battle-tested harness, and each model
performs best inside its own vendor's harness.

**Stack:** Python 3.12+, `uv`, `typer` (CLI), `pydantic` (state/config schemas),
`asyncio` + `subprocess` (parallel agent runs), `rich` (live TUI).

**Providers (v1):** Claude Code and Codex CLI, both flat-rate team subscriptions —
so routing optimizes *quota* (don't burn Claude limits on tasks Codex handles well)
and *fit*, not dollars.

## The pipeline: brief → PR

### Stage 0 · Intake
Input: a product brief (markdown file; Jira later). Creates `runs/<id>/`.

### Stage 1 · Spec (spec-er profile — strongest model)
Produces an OpenSpec change proposal: requirements, BDD-style acceptance criteria
(Given/When/Then — these seed the tests), non-goals, risks.
**Gates:** `openspec validate` → gate protocol (see below). Repeated spec questions
from later stages send the spec back here via the escalation ladder.

### Stage 2 · Decompose (decomposer profile — strongest model)
Spec → task DAG. Per task: acceptance criteria, predicted file scope, assigned
profile, `complexity: trivial | standard | hard`, and a reviewer checklist.
**Gates:** schema-valid, DAG acyclic, file-scope overlap check — overlapping tasks
are serialized by rule, not by hope. Routing = profile × complexity → binding, so
trivial tasks take cheaper bindings (quota optimization made concrete).

### Stage 3 · Per task (parallel where the DAG allows, each in its own worktree)
- **3a Test-writer:** acceptance criteria → tests. **Gate: tests must FAIL when
  run** (TDD red, verified deterministically), lint/typecheck clean.
- **3b Builder** (binding per profile × complexity): implement until green.
  **Gates:** tests pass, lint, typecheck, build, **diff guard — builder cannot
  modify test files** (test author ≠ implementation author, enforced mechanically).
- **3c Reviewer** (always the other model family from the builder): gate protocol
  against the spec + task checklist + repo conventions (CLAUDE.md).

### Stage 4 · Integration
Task branches merge sequentially onto an integration branch in DAG order; full test
suite after each merge; the repo's eval harness runs when search-relevant code
changed. Only fully-green integrations proceed.

### Stage 5 · PR
History rewritten into clean conventional commits; PR body generated, linking the
spec; pushed. CI is the final deterministic gate; the harness watches it and
dispatches one debugger round if red, then halts if still red.

## The gate protocol (Red / Blue / Green)

Every quality gate has the same shape:

1. **Deterministic checks** — commands; failure short-circuits back to the author.
2. **Red Team** — adversarial reviewer, *other model family*, fresh context,
   mandate to attack (ambiguity, edge cases, spec violations, risk), no authority
   to fix.
3. **Blue triage** — findings ranked against a fixed severity rubric:
   - `blocker` — violates spec, breaks correctness or security → must fix
   - `major` — real risk → must fix before merge
   - `minor` — style/polish → recorded, attached to the PR as notes, **never
     re-enters the loop**
   In v1, Blue is the reviewer's structured output validated against a schema — not
   a separate agent (v2 experiment). The rubric is the convergence guarantee: loops
   cannot run on taste. This kills the "strongest model challenged on simple stupid
   things" churn.
4. **Green** — the *original author profile* fixes (spec-er or builder), never the
   reviewer. Judge and fixer stay separate.
5. **Loop**, bounded by the escalation ladder.

## Agent profiles

A profile is a versioned package: YAML config + markdown system prompt.

```yaml
# profiles/builder.yaml
binding:        { cli: codex, model: gpt-5.x }        # primary
fallback:       { cli: claude, model: sonnet }         # quota failover (v2)
prompt:         profiles/builder.md                    # role, values, definition-of-done
context_packet: [spec, task, decisions, conventions, repo_map]
permissions:    { write: task.file_scope, deny: [tests/**] }   # diff guard, declarative
budgets:        { turns: 40, wall_minutes: 30 }
```

**v1 roster:**

| Profile | Family | Rationale |
|---|---|---|
| spec-er | Claude (strongest) | Intelligence concentration point |
| decomposer | Claude (strongest) | DAG quality decides parallelism safety |
| test-writer | Claude | Criteria → honest failing tests |
| builder | Codex | Preserves Claude quota; hypothesis, revisit on evidence |
| reviewer (Red) | always ≠ builder family | Cross-model review as a rule |
| debugger | Claude | Summoned only on red CI/integration |

Bindings are hypotheses, not beliefs: the run telemetry (retry counts, review
findings, CI failures per profile×binding) makes reassignment evidence-based.

## Cross-agent context

- **`decisions.md`** (per run): any agent making a choice worth knowing appends a
  structured one-line entry + rationale ("used httpx, repo already depends on it";
  "reviewer rejected route-layer caching — spec requires per-user variance"). The
  orchestrator injects the whole log into every later context packet. Bounded by
  structure; no vector DB.
- **`blocked` outcome, not a chat channel:** any agent may finish `blocked:
  <question>` instead of `done`. The orchestrator (state machine, not an agent)
  routes intent questions to the owning profile (spec questions → spec-er,
  one-shot), appends the answer to `decisions.md`, re-dispatches. Blocked-retries
  count against the task budget. Repeated spec questions in one run = spec failed
  its gate → escalation sends the spec back for revision.
- **No context-holder agent.** Agents are full CLI harnesses inside the repo; for
  questions of fact the repo is ground truth — a Q&A hub is hearsay with latency
  and a central point of flakiness.

## Orchestrator core

**A run is a directory; the state machine replays it.**

```
runs/2026-07-29-feat-facet-cache/
  state.json          # source of truth: stages, tasks, statuses, attempts
  brief.md
  spec/               # proposal + gate verdicts per iteration
  decisions.md
  tasks/T1/           # context packet, transcripts, diffs, gate results per attempt
  events.jsonl        # append-only: dispatches, gates, retries, durations
```

**Loop (deliberately boring):** read `state.json` → find tasks with satisfied
dependencies → dispatch each to its profile binding as a subprocess in its own
worktree → run gates → write state → repeat. Task statuses:
`pending → running → gated → done | blocked | failed`. Every transition appends to
`events.jsonl`.

**Resumability:** state on disk + stateless agents (context packet rebuilt per
attempt) ⇒ `harness resume <run>` recovers from any crash — and is the v2 quota
failover for free (catch quota error, flip to fallback binding, resume).

**Escalation ladder (config):** 2 retries same binding → 1 retry stronger binding →
halt + desktop notification, run directory as the case file.

**Concurrency:** asyncio + semaphore (default 2–3 parallel tasks, quota-friendly);
worktree per task; integration merges serialized in DAG order.

## Observability

- **v1 — `harness watch`:** live terminal dashboard (`rich`): DAG with task states,
  running agents, gate results, event tail. Pure reader of `state.json` +
  `events.jsonl` — zero coupling to the orchestrator.
- **v1.5 — web dashboard:** Next.js app over the same run directories: real-time
  state machine view, drill into any agent transcript, full run history. Planned as
  the harness's own first assignment (brief in, harness builds it).

## Testing the harness itself

- Unit: state machine transitions, gate evaluation, escalation ladder, schema
  validation (pydantic models make this cheap).
- Integration: fake agent CLIs (scripted stubs emitting canned JSON) drive full
  pipeline runs in a fixture repo — fast, deterministic, no tokens.
- End-to-end: one real small brief against a sandbox repo before first use on
  `ai-search-platform`.

## v2+ roadmap (explicitly deferred)

Provider failover on quota · local models via opencode/Ollama · separate Blue agent
experiment · per-project workflow selection (spec-er emits the workflow) · web
dashboard → configurable workflows/profiles UI · Jira intake · open-source
packaging.

## Open questions (to resolve during implementation)

- Exact headless invocation contracts (`claude -p` / `codex exec` flags, JSON
  output schemas, permission flags) — pin down in the first spike.
- Diff-guard mechanics: enforce via CLI permission config where supported, always
  verify via `git diff` post-hoc (belt and braces).
- Whether Stage 5 history rewriting is agent work or deterministic scripting
  (likely: script squashes per task, agent writes messages).
