# Régie architecture

Régie compiles a product brief and repository evidence into a bounded,
auditable workflow. Models provide judgment; the engine owns state transitions,
validation, git, budgets, retries, and recovery.

```text
brief
  → deterministic repository research + selective knowledge prime
  → spec and task DAG
  → mechanical plan preflight
  → feasibility / completeness / scope reviews
  → risk-selected design reviews
  → human spec approval
  → dependency layers
      → test author → red gate → builder → deterministic gates
      → fresh reviewer evidence matrix → selected specialists
      → optional planned checkpoint
  → full mechanical gates + final integration review
  → safe history rewrite + PR
  → CI/review shepherd
  → learning candidates → explicit knowledge promotion
```

## Adaptive workflow tiers

- `fast`: one low-risk task; keeps mechanical gates and adversarial task review,
  skips review panels, coverage expansion, and final integration review.
- `standard`: multi-task work; adds plan reviews, coverage, selective specialists,
  final integration review, and safe parallel DAG layers.
- `critical`: security, migrations, credentials/external services, or explicitly
  hard tasks; activates the relevant design specialists and planned checkpoints.
- `auto`: resolves deterministically to one of the above from the accepted plan.

The planner supplies risk hints, but Régie also derives risks from criteria,
paths, dependencies, and the committed diff. A hint can add scrutiny; it cannot
disable a mechanically triggered reviewer or gate.

## Sources of truth

- `state.json`: authoritative resumable state, atomically replaced.
- `intent.jsonl`: write-ahead dispatch log for crash reconciliation.
- `events.jsonl`: append-only observability and normalized telemetry.
- `research.json` / `research.md`: bounded repository facts used for planning.
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

This design was materially inspired by MetaSwarm. The precise credit and the
architectural boundary are documented in [Acknowledgements](ACKNOWLEDGEMENTS.md).
