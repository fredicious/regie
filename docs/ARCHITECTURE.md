# Régie architecture

Régie selects the smallest bounded workflow that can safely carry a product
brief to a PR. Models provide judgment; the engine owns state transitions,
validation, git, budgets, retries, and recovery.

The Textual control room is an operator projection over those same artifacts.
It never becomes a second orchestration authority: run creation/resume execute
through the CLI engine in managed child processes, while approval uses the same
shared transition as `regie approve`. A bare `regie` session projects only runs
whose target repository matches its launch folder; explicit `regie watch <run>`
navigation can cross that workspace boundary. If the launch folder is not yet
configured, a first-run setup screen exposes deterministic project detection for
operator review before writing `regie.toml` and opening the brief composer.
Provider enablement is project policy in `[providers].enabled`. Configuration
loading filters every profile ladder and its hard-task override before dispatch,
then rejects any selection that leaves a role without a viable binding. Provider
changes never mutate an attempt already in flight; they take effect when the
engine next loads the project for a run or resume.
Every mandatory review panel walks its enabled profile ladder before reporting
the reviewer unavailable. A binding blocked by the shared quota circuit produces
a synthetic `skipped` activity record without spawning its CLI.
Provider routing and plan convergence have separate budgets: quota skips may
advance the provider ladder, but they do not consume the bounded opportunities
for a planner to repair preflight or review-panel findings.

After three rejected plan contracts, a configured `product-owner` profile is
invoked once as a recovery advisor. Its immutable packet contains the brief,
latest plan, deterministic failures, review findings, and attempt/provider
evidence. The structured decision may direct one final revision, accept only
scope/alignment review findings, ask the human, or halt. The engine validates and
applies that recommendation; the advisor cannot mutate the repository, dispatch
agents, waive deterministic validation or gates, change provider policy, or
increase budgets. A failed recovery reaches a real halt instead of recursively
invoking more management agents.

Execution review uses the same bounded pattern without turning the Product
Owner into an orchestrator. Successful test/build/review revisions each receive
a fresh provider-failover ladder while their history remains in telemetry. If
the same serious finding repeats, or more than two distinct revision requests
accumulate, the Product Owner receives the brief, accepted spec, task contract,
acceptance evidence, complete finding history, and current diff. It may reject
out-of-contract feedback, direct exactly one final implementation revision, ask
one human question, or halt. Any further serious finding after that recovery is
a deterministic halt.

```text
brief → deterministic route
  ├─ direct: one owner (implementation + focused tests)
  │    → mechanical gates → focused adversarial review
  │    → discovered risk/coordination evidence may escalate to planned
  └─ planned
  → deterministic repository research + selective knowledge prime
  → spec and task DAG
  → mechanical plan preflight
  → feasibility / completeness / scope reviews
  → risk-selected design reviews
  → on non-convergence: bounded Product Owner decision → one final revision
  → human spec approval
  → dependency layers
      → test author → red gate → builder → deterministic gates
      → fresh reviewer evidence matrix → selected specialists
      → on review non-convergence: bounded Product Owner decision
      → optional planned checkpoint
  → full mechanical gates + cross-task integration review when tasks interact
  → safe history rewrite + PR
  → CI/review shepherd
  → learning candidates → explicit knowledge promotion
```

## Adaptive workflow tiers

- `fast`: forces one direct owner; keeps mechanical gates and adversarial task
  review, and skips upfront planning, a separate test-writer, review panels,
  coverage expansion, and final integration review.
- `standard`: multi-task work; adds plan reviews, coverage, selective specialists,
  cross-task integration review, and safe parallel DAG layers.
- `critical`: security, migrations, credentials/external services, or explicitly
  hard tasks; activates the relevant design specialists. Planned checkpoints
  are reserved for actual human-authority boundaries such as credentials,
  destructive operations, billing, publishing, or deployment.
- `auto`: starts direct when the brief contains no material risk signal. Security,
  migration, public-API, architectural, destructive-data, and external-dependency
  evidence routes to planning before implementation. A direct owner can upgrade
  the route after inspecting the repository; it cannot downgrade explicit policy.

The planner supplies risk hints, but Régie also derives risks from criteria,
paths, dependencies, and the committed diff. A hint can add scrutiny; it cannot
disable a mechanically triggered reviewer or gate.

## Environment and gate failures

Each isolated worktree runs an operator-configured `[commands].setup` before its
first agent dispatch. When absent, Régie infers a lockfile-respecting bootstrap
for pnpm, npm, Yarn, Bun, uv, Cargo, or Go. Setup failure halts before any model
tokens are spent and retains its output under the run's `environment/` artifacts.

When a build command exists, build precedes the authoritative test suite so
preview-based E2E configurations have runnable artifacts. Gate output is
classified as code or infrastructure evidence. Missing executables, registries,
browser runtimes, and build artifacts halt with the agent's edits preserved;
they never consume another implementation ladder rung. Code failures remain
eligible for bounded repair and clean rollback.

Direct owners return a schema-validated `completed`, `clarify`, or
`needs_planning` outcome. Natural-language clarification detection remains a
defensive adapter fallback, not the primary control protocol.

## Sources of truth

- `state.json`: authoritative resumable state, atomically replaced.
- `intent.jsonl`: timestamped write-ahead dispatch log for crash reconciliation
  and live agent identity/start-time projection.
- `events.jsonl`: append-only observability and normalized telemetry, including
  completed attempt binding, duration, turns, tokens, and outcome.
- `tasks/*/attempt-*.out`: provider stream retained as evidence and used only as
  a live output heartbeat; it never becomes orchestration state.
- `research.json` / `research.md`: bounded repository facts used for planning.
- `product-owner-decision.json` / `.md`: machine-readable recovery decision and
  its operator-facing projection.
- `tasks/*/context.md`: exact progressive-disclosure packet sent to an agent.
- `criterion-evidence.json`: PASS/FAIL evidence for every acceptance criterion.
- `knowledge-candidates.json`: proposed learnings; never promoted silently.
- `pr-body.md`, `checkpoint.md`, `handoff.md`: human-readable state projections.

## Parallelism and recursion

Independent tasks at the same DAG depth may fan out into task-specific branches
and worktrees. Predicted overlapping scopes or `parallel_safe = false` force
serialization. Completed branches are integrated deterministically in task-ID
order; any cherry-pick conflict halts with evidence. Parent/child run links
provide the durable foundation for multi-repository epics without making an
external tracker the execution authority.

## Project knowledge

Knowledge is kept outside the target repository under `$REGIE_HOME/knowledge`.
Retrieval is deterministic and bounded by task paths, terms, risk tags, and work
type. Reflection produces candidates from decisions, repeated failures, and
review findings. `regie knowledge-approve <run>` is required to promote them.

This design was materially inspired by MetaSwarm and Ponytail. The precise credit and the
architectural boundary are documented in [Acknowledgements](ACKNOWLEDGEMENTS.md).
