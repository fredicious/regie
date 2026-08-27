# Régie v1 — Design

**Date:** 2026-07-29 (rev 2, after four-agent adversarial review)
**Status:** Draft for review
**Author:** Frédéric Cabassut + Claude (brainstorming session)

## Problem

Seven months of agent-assisted coding surface one dominant pain: **the human is the
orchestrator.** Tell model A to build, ask model B to review the PR, feed the review
back to model A, referee the nitpicks, repeat. Every existing harness tried
(Conductor, Nymbalist, opencode) automates the *agent*, not the *workflow between
agents*.

**Goal:** product brief in → reviewed, tested, CI-green PR out — with the human
touching the run at exactly two points by default (approve the spec, review the
final PR), and a flag to go fully autonomous once specs prove reliable.
Multi-model by design: each task done by the best tool, dispatched automatically.

## Users and scope

- **Primary (v1):** Frédéric, daily driver on `ai-search-platform` (ARTE) —
  feature-sized specs in an existing polyglot monorepo (FastAPI + Next.js + eval
  harness). Spec in, PR out.
- **Later (v2+):** other repos, other users, possible open-source release. See
  `docs/ROADMAP.md` for everything consciously deferred.

## Non-goals (v1) — deferred, not rejected (see ROADMAP)

- Parallel task execution. v1 runs tasks **serially** in topological order in one
  worktree on one branch. The task DAG exists from day one (ordering now, the seed
  of parallelism later), but there is no integration branch, no per-task worktree,
  no merge-conflict machinery. Concurrency was capped at 2–3 by quota anyway; the
  constraint v1 relieves is attention, not wall-clock.
- Complexity-based routing. One binding per profile; the escalation ladder
  discovers difficulty by evidence ("it failed, go stronger") instead of asking a
  model to predict it.
- Automated `blocked`-question routing. `blocked` halts and notifies; the human
  edits the spec or `decisions.md` and runs `regie resume`. Two minutes of human
  time instead of a second dispatch subsystem.
- Provider failover automation, local/OSS models, TUI/web dashboard, Jira intake,
  separate Blue-triage agent, dynamic workflows.

## Core principles

1. **Deterministic first.** Everything checkable by a command is a hard gate.
   Agent judgment is layered on top, never instead.
2. **Gate decisions come only from our own commands.** Verified: agent CLIs exit 0
   and report `is_error: false` meaning "the agent finished its turn", not "the
   task succeeded" — and Claude's permission denials are invisible in
   machine-readable output. The harness never trusts an agent's exit status or
   self-report; it runs tests/lint/diff checks itself.
3. **Spec-driven.** No code without a spec that passed its gates. Specs use the
   repo's existing OpenSpec format.
4. **TDD with author separation.** Tests are written (and must fail honestly —
   see TDD mechanics) before implementation, by a different agent than the
   builder. The builder is blocked from editing test files by a post-hoc diff
   gate the harness enforces itself.
5. **Cross-model review.** The reviewer binding is a different model family than
   the builder binding. Shared blind spots are the enemy.
6. **Bounded everything.** Every loop has a retry budget and an escalation
   ladder; every agent invocation has enforced turn and wall-clock budgets.
   Honest statement of the convergence guarantee: **retry budgets terminate
   loops; the severity rubric only prevents nitpick churn.** Runs halt and
   notify rather than spin.
7. **Everything on disk, outside the target repo.** A run is a directory under
   `~/.regie/runs/` — never inside the target repo, so transcripts (which may
   contain sensitive content) can never be swept into a commit.
8. **The orchestrator owns git.** Agents edit files; the harness does all
   `git add/commit/rebase/push`. (Also load-bearing: Codex cannot perform git
   operations inside linked worktrees — verified open issues openai/codex#14338,
   #23661, #15505.)

## Verified CLI contracts (2026-07, tested)

- **Claude Code** (`claude -p --output-format json`): confirmed working, incl.
  inside linked worktrees. Use `--max-turns` (enforces turn budgets),
  `--json-schema` (schema-validated structured output — used for reviewer
  findings and planner DAGs), `--allowedTools`/`--disallowedTools`/`--settings`
  for defense-in-depth permissions. Do **not** use `--bare` (forces API-key auth,
  incompatible with subscription OAuth); use `--setting-sources`. Result JSON
  exposes `usage`, `modelUsage`, `num_turns`, `total_cost_usd`,
  `api_error_status`, `terminal_reason` — the telemetry source.
- **Codex** (`codex exec --json`): JSONL event stream, `--output-schema`,
  `-m/--model`, `--sandbox workspace-write`, per-invocation config via `-c` or a
  synthesized `CODEX_HOME`. **No programmatic quota telemetry** (`rate_limits`
  null in exec mode). Path-permission config exists but `apply_patch` has been
  shown to bypass path restrictions (openai/codex#24214) → **treat Codex
  permission config as advisory only; the harness's `git diff` gate is the real
  guard.**
- **Auth risk (external):** subscription use of `claude -p` is explicitly
  permitted today, but Anthropic announced-then-paused moving headless usage off
  subscription limits. Mitigation baked in: a binding is `{cli, model, auth}` —
  flipping any profile to API-key billing is a config change, not code.

## The pipeline: brief → PR

Five Python functions called in order (no stage-DAG interpreter): `plan`,
`implement` (loops over tasks), `finalize`, plus `intake` before and `pr` after.

### Stage 0 · Intake
Input: a brief written from a template — goal, in scope, **explicitly out of
scope**, acceptance signals, known-involved areas, do-not-touch. Creates the run
directory and the run worktree (`regie/<run-id>` branch off a pinned
`origin/main` SHA recorded in state).

### Stage 1 · Plan (planner profile — strongest model, one context)
One agent produces both the OpenSpec change proposal (requirements, BDD
Given/When/Then acceptance criteria, non-goals, risks) and the task DAG — merging
spec-er and decomposer removes a lossy handoff. Per task: acceptance criteria
(each criterion must parse as Given/When/Then), file scope (advisory — see diff
gates), profile, reviewer checklist.
**Gates:** `openspec validate` (structure) → criteria parse check + each
criterion maps to ≥1 named planned test (mechanical) → DAG schema-valid and
acyclic → adversarial review (other family) with severity triage →
**human checkpoint: `regie approve <run>`** prints the spec and waits.
`--autonomous` skips it; default is on until specs prove reliably good. This is
the highest-leverage 3 minutes in the pipeline.

### Stage 2 · Per task, serial, in the run worktree
- **2a Test-writer:** acceptance criteria → tests (plus, where the language
  requires it, interface stubs — signatures/types that raise NotImplemented — so
  tests can typecheck; stubs are part of the test contract and live in scope).
  **Gates (TDD red, precisely):** test collection succeeds (`pytest
  --collect-only` / suite equivalent); new tests **fail with assertion failures
  or NotImplemented** — not ImportError/SyntaxError; failure reasons recorded in
  the gate result; lint clean. Full-repo typecheck is deferred to 2b (tests
  against not-yet-existing code cannot typecheck clean; the stubs keep the local
  contract typed). Harness commits the tests (`test(scope): …`).
- **2b Builder** (Codex binding): implement until green. **Gates, run by the
  harness:** scoped test command for the touched app passes (new tests + that
  app's suite); lint; typecheck; build; **diff gate:** `git diff --name-only`
  against the repo's configured `test_globs` (e.g. `apps/*/tests/**`,
  `**/*.test.*`) — any builder edit to a test file fails the attempt. File-scope
  is advisory: out-of-scope edits are flagged in the gate result for the
  reviewer, not auto-failed (predictions are wrong in boring ways — import
  registrations, configs). Harness commits (`feat|fix(scope): …`).
- **2c Reviewer** (always the other family from the builder; for spec review the
  direction reverses — Codex attacks Claude's plan; the asymmetry is
  acknowledged and backstopped by the human spec checkpoint): reviews **the
  committed diff** in the run worktree (may run tests; no authority to edit).
  Input packet: spec, task + checklist, diff, conventions, decisions log.
  Output: `--json-schema`-validated `findings[]`, each with severity.
- **Wrong-test escape hatch (deadlock fix):** if the builder reports the test
  itself is defective, or the reviewer flags a test as blocker, the fix routes to
  the **test-writer** (author fixes own artifact; separation preserved). A
  builder claim of "bad test" that the test-writer rejects counts against the
  builder's budget — no ping-pong.

### Stage 3 · Finalize
Full test suite + lint + typecheck once, on the completed branch. The repo's
eval harness runs iff the diff touches configured `eval_trigger_globs`
(deterministic predicate, e.g. `apps/api/**`). Rebase onto fresh `origin/main`;
conflict → halt + notify (v1 does not auto-resolve rebase conflicts).

### Stage 4 · PR
History cleanup is **scripted** (squash to one commit per task; backup ref
`refs/regie/backup/<run-id>` created first; post-rewrite tree hash must equal
pre-rewrite tree hash or the harness restores and halts); an agent writes the
commit messages and PR body (linking the spec, minor findings attached as a
"review notes" section). Push, open PR, watch CI. If red: debugger rounds — the
debugger is a builder-profile variant, **its patches pass the same 2b gates and
a 2c review before push** (no ungated third author), same 2+1 ladder, then halt.

## Gate protocol (every agent-judged gate)

1. **Deterministic checks** first; failure short-circuits to the author.
2. **Adversarial reviewer** — other model family, separate session, mandate to
   attack, no authority to fix. Returns schema-validated `findings[]` with
   severity per the fixed rubric:
   - `blocker` — violates spec, breaks correctness or security → must fix
   - `major` — real risk → must fix before merge
   - `minor` — style/polish → recorded, attached to the PR, **never re-enters
     the loop** (kills nitpick churn)
3. **Author fixes** (planner/builder/test-writer — never the reviewer).
4. Loop, bounded: **2 retries same binding → 1 retry stronger binding (per the
   config's explicit binding-strength order; profiles already at the top rung
   skip to halt) → halt + notify** with the run directory as the case file.

## Agent profiles (v1: four)

| Profile | Binding (family) | Notes |
|---|---|---|
| planner | Claude (strongest) | Spec + task DAG in one context |
| test-writer | Claude | Criteria → honest failing tests + stubs |
| builder | Codex | Preserves Claude quota; debugger = prompt variant of this profile |
| reviewer | opposite of the authoring profile's family | Cross-model rule, enforced at dispatch |

```yaml
# profiles/builder.yaml
binding:  { cli: codex, model: gpt-5.x, auth: subscription }
prompt:   profiles/builder.md
budgets:  { turns: 40, wall_minutes: 30, stall_minutes: 5 }
```
Bindings are hypotheses; telemetry (below) makes reassignment evidence-based.
No `fallback` field until failover is wired (unused schema fields rot) — the
`auth` key is what keeps the paused-policy risk a config change.

## Context packets

Packet assembly is a first-class component — most output quality lives here.
Each packet is rendered markdown with per-section token budgets, **written to
`tasks/Tn/context.md` before dispatch** (the debugging surface for baffling
agent behavior). Sections: task + acceptance criteria + checklist · relevant
spec excerpt · decisions log · conventions = target repo's CLAUDE.md + AGENTS.md
verbatim (87 lines — free) · scope hints. **No `repo_map` in v1:** agents are
full CLI harnesses standing in the repo; letting them grep beats shipping a
stale map.

## Cross-agent context

- **`decisions.md`** (per run): structured one-line entries + rationale from any
  agent ("used httpx — repo already depends on it"). Injected into every later
  packet, hard-capped (~2k tokens; harness warns at 80%). Serial execution ⇒ no
  concurrent writes.
- **`blocked` outcome:** agent finishes `blocked: <question>` → run halts,
  notifies, human answers by editing spec or decisions.md → `regie resume`.
  Blocked-halts are telemetry: recurring ones indict the spec gate.

## Orchestrator core

```
~/.regie/runs/2026-07-29-facet-cache/
  run.lock            # exclusive flock; second process (or resume-while-alive) refuses
  state.json          # authoritative; written atomically (tmp + rename)
  intent.jsonl        # write-ahead dispatch log: append intent BEFORE spawn
  events.jsonl        # observability only — engine never reads it
  brief.md  spec/  decisions.md
  tasks/T1/           # context.md, transcripts, gate results per attempt
```

- **Task state:** `{stage: test|build|review, status, attempts: [{binding,
  prompt_hash, gate_results, usage}]}` — enough for resume and `regie status`
  to know "2a done, 2b attempt 2 running".
- **Dispatch:** append intent → spawn agent in its own **process group**, pgid
  recorded → on completion run gates → atomic state write. Resume reconciles
  intent vs state: any intended-but-unrecorded attempt is treated as failed.
- **Budget enforcement:** `--max-turns` where supported, plus harness-side
  wall-clock kill (SIGTERM → SIGKILL the process group) and a stall detector
  (no output for `stall_minutes`).
- **Resume semantics (task granularity):** in-flight attempt's uncommitted
  changes are discarded (`git checkout . && git clean -fd` in the worktree);
  the task re-runs from a rebuilt packet. Committed work is never touched.
- **Quota handling (v1 = detect, not failover):** Claude quota/API errors from
  `api_error_status`/`terminal_reason`; Codex from error events. On detection:
  halt cleanly (resumable), notify — never burn the retry ladder on a quota
  error.
- **Concurrent runs:** one lock per run; run worktrees/branches are namespaced
  by run-id. v1 additionally refuses two live runs against the same target repo
  (serial world; lift with parallelism work).

## Security

- Autonomous agents hold **write access to a worktree and nothing else**; push
  happens only from the harness, to `regie/*` branches, never to `main`
  (branch protection assumed on; the harness never force-pushes anything except
  its own `regie/*` branch after the tree-identity check).
- `runs/` lives outside the repo (principle 7): transcripts can't be committed.
  Transcripts stay local; they are not uploaded anywhere.
- Untrusted-input stance: the brief is authored by the operator (trusted); repo
  content and CI logs fed to agents are semi-trusted — prompt injection through
  them is a residual risk, mitigated by gates (an injected agent still can't
  pass tests/diff gates silently) and by the no-direct-push rule. Not solved,
  named.
- Agent sandboxing: Claude via permission settings (defense in depth), Codex via
  `--sandbox workspace-write`. Network egress by agents is allowed in v1
  (needed for package installs) — revisit before any multi-user story.

## Observability & telemetry

- `regie status <run>` pretty-prints state.json; `tail -f events.jsonl | jq`
  is the live view. No TUI in v1 (a well-designed events.jsonl makes it
  unnecessary; dashboard ambitions live in the ROADMAP).
- Per attempt: binding, **prompt file hash**, turns, usage/cost fields, gate
  results, duration. Run-level: outcome (green-PR-unattended / halted-at /
  abandoned), human interventions count, quota events.
- The evidence loop is a script over events.jsonl answering "did builder-prompt
  v3 reduce review blockers?" — prompts are the biggest tunable and iteration on
  them must not be blind.

## Target-repo prerequisites (week zero, in ai-search-platform)

- Canonical commands the gates call — `make test-api`, `make test-playground`,
  `make test-eval`, `make lint`, `make typecheck` (none exist today; the harness
  hardcodes nothing else).
- Time the full suite per app before writing Stage 3 (552 test files — if the
  suite is 25 min, gate scoping matters).
- Flaky policy: gate runner reruns failures once (`pytest --lf`); a
  pass-on-rerun records `flaky: true`, doesn't charge the retry budget, and
  appends to a quarantine list in config.
- `regie.toml` in the target repo: test_globs, eval_trigger_globs, commands,
  binding-strength order.

## Testing the harness itself

- Unit: state transitions, gate evaluation, ladder, packet rendering, resume
  reconciliation (pydantic + fixture state files).
- Integration: **fake agent CLIs** (scripted stubs emitting canned JSON/JSONL)
  drive full runs in a fixture repo — fast, deterministic, no tokens; includes
  crash-injection (kill mid-dispatch, truncate state.json) to prove resume.
- End-to-end: one real small brief against a sandbox repo, then first supervised
  runs on `ai-search-platform`.

## Open questions (deliberately few)

- Exact Codex quota-error event shape in `--json` output (no telemetry
  documented; needs one empirical exhaustion or a forced-error test).
- Whether Claude spec-review (Codex attacking Claude's plan) produces useful
  findings in practice, or whether spec review should also bind to Claude with a
  different prompt (measure via telemetry, decide on evidence).
- pnpm/Next.js equivalents of the pytest red-gate mechanics (assertion-failure
  detection in vitest/jest output).
