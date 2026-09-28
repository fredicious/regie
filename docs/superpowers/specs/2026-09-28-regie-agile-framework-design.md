# Régie as an agile framework — Design

**Date:** 2026-09-28
**Status:** Draft for review
**Author:** Frédéric Cabassut + Claude (brainstorming session)
**Supersedes:** the adaptive-tier workflow (auto/fast/standard/critical) described in
`2026-07-29-regie-v1-design.md`, `docs/ARCHITECTURE.md`, and the README.

## Problem

Régie grew into a chief orchestrator. A deterministic engine routes briefs with
keyword matching, dispatches a fixed chain of role agents, and, when reviewers and
builders disagree, calls a Product Owner *agent* to arbitrate. Each benchmark run
added another rule or flag. The engine now makes judgment calls about the work
(risk inference from substrings, title-word similarity to detect repeated findings,
keyword-triggered specialists), and those calls are where both the bugs and the cost
live.

The goal remains: **a stakeholder request in, reviewed, tested, CI-green work out**.
The change is how Régie gets there: **as a framework for a self-organizing team of
coding agents, the way Scrum/Kanban is a framework for a human team.** No agent and
no heuristic is the boss; the framework's rules are.

## Evidence (regie-bench spike, 2026-09-28)

Nine trials at `72d3d9f`, Codex `gpt-5.6-sol`, current evaluators. Each implementation
commit was also re-evaluated against the hidden acceptance test to see whether a
review revision ever changed the outcome.

| Arm (3 repeats each) | Hidden acceptance | Median time | Median fresh tokens | Revisions that changed the outcome |
|---|---|---|---|---|
| storage-migration, forced direct | 3/3 | 200 s | 89k | 0 of 1 |
| storage-migration, planned | 3/3 | 624 s | 185k | 0 of 1 |
| review-trap-duplicates, direct | 3/3 | 483 s | 143k | 0 of 6 |

What it established:

- The current planned path (research, planner, plan-review panel, test-writer/builder
  split, specialists) reaches the same outcome as one owner at ~3× the time and ~2× the
  tokens on today's cases. **But every bench case is a small change to a five-file app**,
  so this says nothing about work that genuinely needs decomposition. The bench lacks
  that range; Phase 0 adds it.
- Review found real defects the evaluators do not encode (a `null` list entry crashing
  add, Greek final sigma escaping case-insensitive matching, a rejected add overwriting
  unsupported stored data; historically a `trim()` crash). It also never stopped: every
  trap run found a new edge case per round until Product Owner arbitration.
- Historical "correct code rejected" results were measured against a weaker evaluator;
  commit `78c431f` in regie-bench tightened it and silently made the history
  incomparable. Under the current evaluator none of the historical migration commits
  pass, and some reviewer findings dismissed as out of scope were exactly what the
  stricter contract now requires.
- The Product Owner agent's two consequential scope rulings in run `8586446` went one
  for two against the stakeholder's later brief.

## Principles

1. **The engine is the framework, not the manager.** It enforces rules (Definition of
   Ready, Definition of Done, timeboxes, WIP limits, working agreements) mechanically.
   It never makes a judgment call about the work.
2. **Minimize the workflow, never the understanding** (Ponytail). Refinement,
   acceptance criteria, and complexity estimation are understanding and always happen.
   Ceremony (committees, arbitration, extra reviewers) must earn its place on the bench.
3. **Small increments and feedback.** Work is delivered as demonstrable increments,
   each shaped by stakeholder feedback on the previous one.
4. **Complexity is measured by the tool, not guessed or hand-labelled**, and the
   measurement is calibrated against what actually happens.
5. **Authority stays with the human.** Agents do the Product Owner's work; the
   stakeholder keeps the Product Owner's authority.

## Roles and authority

| Role | Who | Owns | May not |
|---|---|---|---|
| Stakeholder | the human | the request; answers questions only they can answer; approves the spec; accepts or rejects each increment; decides follow-ups, open blockers, and releases | — |
| Product Owner | agent | interviewing the stakeholder; the spec, backlog order, and acceptance criteria; answering developers' questions from what is agreed; folding feedback into the backlog; proposing follow-ups and releases | decide scope *against* the stakeholder: it may defer an item to a proposed follow-up, never drop or permanently reject it; hide an assumption |
| Developer | agent | estimating (planning poker); splitting an increment into tasks; writing tests and code for a task | edit outside its task's worktree; weaken existing tests |
| Reviewer | agent (another model when enabled) | checking every acceptance criterion with evidence; blocking only under the review contract | edit anything; block without evidence |
| Debugger | agent | fixing red CI on a released PR (existing CI shepherd) | — |
| Framework | the engine | state transitions, rules, timeboxes, WIP, git, gates, budgets, provider failover | judge the work |

## The flow

```text
request ──► Product Owner interview ◄──► stakeholder (only what the PO cannot answer)
               │
               ▼
      spec: goal · ordered increments · criteria for the next increment · assumptions · out of scope
               │  stakeholder approves (skipped on the tiny-request fast path)
   ┌───────────▼──────────────────── per increment ───────────────────────────────┐
   │ refine   planning poker → size S–XL; L+ split into tasks                      │
   │ build    developer(s), tests first → Definition of Done → peer review         │
   │ review   packet + demo ──► stakeholder: accept · request changes · decisions  │
   │ fold     PO folds feedback and follow-ups into the backlog                    │
   └────────────────────────────────────────────────────────────────────────────────┘
               │
               ▼
      release: PR of accepted increments, one commit per increment
```

## 1. Intake — the Product Owner

1. The Product Owner reads before it asks: the request, Régie's deterministic
   repository research (`research.md`), and promoted project knowledge.
2. It drafts the spec and lists only the questions it cannot answer from those sources.
   Questions are asked **in batches**; each states why it matters and a **proposed
   default**. A skipped question takes its default and is recorded as a visible
   assumption. Intake is timeboxed to three question rounds; after that, remaining
   questions take their defaults.
3. During refinement, developer questions go to the Product Owner first. It answers
   from the spec, the request, or the repository, and escalates to the stakeholder only
   when the answer depends on what the stakeholder wants. Escalated questions join the
   next batch.
4. **Spec Definition of Ready** (checked mechanically by the framework):
   - a product goal;
   - an ordered list of increments, each a vertical slice of value with a title and goal;
   - for the next increment: Given/When/Then acceptance criteria with stable IDs
     (`INC-<n>/AC-<m>`); later increments may carry draft criteria;
   - an explicit out-of-scope list;
   - a visible assumptions list (every assumed default, every inferred requirement);
   - no open questions.
5. **Stable criterion IDs.** When criteria change, unchanged criteria keep their IDs,
   new criteria get new IDs, removed IDs are retired and never reused.
6. **Stakeholder approval of the spec** covers the goal, the increment list and order,
   the next increment's criteria, the assumptions, and the out-of-scope list. The
   stakeholder may approve or request changes (free-text feedback the Product Owner
   turns into a revised spec, shown as a diff).
7. **Tiny-request fast path.** For a single-increment spec, refinement (§2) runs
   before approval. If the Product Owner has no questions and no assumptions and
   planning poker sizes the increment S or M, spec approval is skipped and building
   starts. The stakeholder still sees the
   criteria in the review packet and still accepts the result.

## 2. Refinement — planning poker

1. **Just-in-time.** Only the next increment is refined in detail. Refinement of the
   next increment runs while the stakeholder reviews the current one.
2. **Estimation rubric.** Each estimator rates, with cited repository evidence:
   - *scope*: modules and layers touched, predicted files;
   - *contracts crossed*: APIs, schemas, persisted data formats;
   - *uncertainty*: unfamiliar code, missing tests around the change;
   - *risk*: data loss, security, irreversibility.

   The result is a size: S, M, L, or XL.
3. **Poker.** Two developer estimators run independently, on different providers when
   more than one is enabled (otherwise two independent calls on the same provider).
   - Same size: it stands.
   - Adjacent sizes (e.g. S and M): the larger stands; no discussion round.
   - Two or more sizes apart: one discussion round. Each estimator sees the other's
     evidence and re-estimates once. If they converge (same or adjacent), the larger
     stands. If not, the increment is **uncertain**: a timeboxed, read-only spike task
     investigates and appends findings, then both re-estimate once; if still apart, the
     larger size stands.
4. **Split rule.** An increment is Ready when its size is S or M, or when developers
   have split it into tasks that are each estimated S or M, with predicted file scope
   and dependencies. The initial threshold (split at L and above) is calibrated in
   Phase 4. If developers judge that an increment is really two separately
   demonstrable slices of value, they propose a re-slice to the Product Owner instead
   of splitting into tasks; the Product Owner re-slices and the change appears in the
   next stakeholder touchpoint.
5. **Increment Definition of Ready:** criteria with IDs, a recorded poker estimate,
   the split rule satisfied, no open questions.
6. **Calibration record.** Every estimate is stored with its evidence and later
   compared with the actual outcome (files touched, revisions, elapsed time, tokens)
   in `regie stats --calibration`.

## 3. Working agreements — building an increment

1. **Cross-functional developers.** One developer agent writes the tests and the code
   for a task. The separate test-writer and builder roles are removed.
2. **Tests prove the change** (Definition of Done). The framework checks that the
   task added or changed at least one test file (per `test_globs`), that the test
   command **fails** when those test changes are applied to the pre-task commit, and
   **passes** on the task's result. A task may instead declare `behavior_change:
   false` with a one-sentence justification (refactors); then the check is that
   existing tests pass and no existing test was weakened (the current diff guard), and
   the reviewer must confirm the claim as a DoD item.
3. **Tasks are internal; criteria are not.** Independent tasks run in parallel in
   isolated worktrees within the WIP limit (`max_parallel_tasks`), using the existing
   worktree and integration machinery. Each task must pass the gates. Peer review
   happens **once per increment**, on the integrated result, against the increment's
   acceptance criteria. That review also covers cross-task integration.
4. **Review contract.** The reviewer must return a verdict with evidence for every
   criterion ID. A **blocker** must be one of:
   - `criterion`: a failed criterion, citing an existing criterion ID, with concrete
     input, expected, and actual;
   - `regression`: an existing behavior broken, with a reproduction;
   - `dod`: a Definition of Done item violated.

   The framework validates blockers mechanically (the cited ID exists, required fields
   are non-empty). Anything else, including invalid blockers, becomes a **proposed
   follow-up** for the Product Owner. Follow-ups never block the current increment and
   are never silently dropped.
5. **One revision round.** Blockers return to the developer once, followed by one
   re-review. If a blocker remains, the increment goes to the stakeholder marked
   **open blocker**, with the dispute visible (the blocker, the developer's response,
   and the evidence). No loop and no arbitration.
6. **Risk shapes the review, not the headcount.** The risk dimension of the poker
   estimate adds matching checks (data safety, security, public contracts) to the one
   reviewer's checklist. Separate specialist reviewers are removed from the flow until
   the bench shows they catch what the single reviewer misses.
7. **Timeboxes.** Existing per-profile budgets (turns, wall, stall) and provider
   ladders apply to every dispatch; run and task cost budgets (`max_run_usd`,
   `max_task_usd`) apply to cumulative spend and are checked before every dispatch.

## 4. Increment review, feedback, and release

1. **When an increment is ready for the stakeholder:** Definition of Done met and peer
   review passed, or its one revision round ended with an open blocker. Only one
   increment is built at a time, on the run worktree, which stays stable during review.
2. **Review packet:** the increment's goal; each criterion with its evidence (tests,
   gate results, reviewer verdict); a change summary and the worktree path; any open
   blocker with both sides; proposed follow-ups; the estimate against the actual
   outcome; and the next increment's refined criteria.
3. **Demo.** `[commands].demo` in `regie.toml` (for example `npm run dev`) is launched
   from the run worktree on request. Régie owns the process and stops it when the
   stakeholder leaves the packet or decides. Without a demo command, the packet stands
   alone. `regie init` proposes a demo command when it can detect one.
4. **Stakeholder decisions:**
   - **accept**, with optional feedback for later increments. Accepting also approves
     the next increment's refined criteria shown in the packet;
   - **request changes**, with required feedback. The Product Owner turns it into
     updated or added criteria for this increment (shown as a diff), and the increment
     returns to building for a fresh cycle. The one-revision limit applies to reviewer
     blockers only;
   - per follow-up: **next** (the Product Owner adds it to the backlog) or **never**;
   - per open blocker: **fix** (one more revision), **defer** (becomes a follow-up), or
     **accept** (recorded as accepted with the known issue).
5. **On acceptance:** the increment's internal commits are squashed into one commit
   titled after the increment with its criteria in the body; the Product Owner folds
   feedback into the backlog; the next increment starts building. Until the stakeholder
   responds, the run waits at *awaiting stakeholder* and notifies; there is no timeout.
6. **Release.** By default the release is one PR containing every accepted increment,
   opened after the last increment is accepted, through the existing finalize path
   (full gates, rebase, PR carrying the spec) and the existing CI shepherd.
7. **Early release.** The Product Owner may propose releasing after increment *k*.
   If the stakeholder accepts, Régie opens a PR from a snapshot branch at increment
   *k*'s commit, and the run continues. The next release PR is opened only after the
   previous release is merged; until then the run waits at *awaiting merge*. At the
   next release, Régie rebases the unreleased increment commits onto the current base,
   excluding released increments (recorded in state).

## 5. Control room

Three equal tabs, switched with `1`, `2`, `3`:

1. **Inbox** — everything waiting for the stakeholder: question batches (answered
   inline, defaults pre-filled), spec approval (assumptions highlighted), increment
   review packets (with demo launch and all decisions), follow-up and blocker
   decisions, release proposals, and merge waits.
2. **Board** — a Kanban board of increments: *refining, ready, building, review,
   for you, done*, with tasks shown on their increment's card and WIP visible. Opening
   a card shows its criteria and evidence, the poker estimate with each round's
   evidence, and its Ready/Done checklists.
3. **Engine** — today's view for operating and debugging Régie itself: task table,
   evidence, event ledger, agent activity. Agent rows show roles (Product Owner,
   developer, reviewer) instead of stage names.

The tab bar always shows the number of inbox items (`1 Inbox (3)`). The app opens on
the Inbox when something is waiting, on the Board otherwise. CLI parity:
`regie inbox <run>` lists pending items; `regie decide <run> <item> <decision>
[--feedback TEXT]` records any decision; `regie demo <run>` launches the demo command.

## 6. Bench and calibration (regie-bench)

1. **Measurement fixes:**
   - every `result.json` records a SHA-256 of the case's evaluator and brief;
   - the per-commit acceptance trajectory (hidden acceptance of each increment or
     implementation commit) becomes a standard metric;
   - `result.json` records questions asked, defaults taken, and stakeholder decisions.
2. **Feature ladder track** (public, synthetic): a mid-size FastAPI + Next.js
   application with realistic conventions and some deliberate tech debt, SQLite for
   storage, pytest API tests and frontend unit tests (no browser). Six to ten
   stakeholder-style briefs from S to XL on that app, including at least: one bug fix,
   one end-to-end field (migration, API, UI), one deliberately ambiguous request, one
   behavior-preserving refactor, one multi-increment feature, and one greenfield case.
3. **Reference solutions, validated automatically.** Each case ships a reference
   solution. A case is valid only if its hidden tests fail on the fixture and pass on
   the reference solution, and the reference solution passes the fixture's public
   tests and lint. Claude authors the fixture, briefs, and reference solutions; the
   validation is mechanical.
4. **Stakeholder simulator.** Each case has a hidden intent document. A simulator
   agent on a fixed binding (configured per bench run and recorded in `result.json`)
   answers Product Owner questions faithfully from it and answers "no preference"
   when it is silent, which makes the Product Owner take its default. Other
   stakeholder decisions are deterministic: increments are accepted unless the case
   scripts feedback after a given increment, open blockers are accepted (and counted),
   follow-ups are marked *never*, and early-release proposals are declined. The final
   result is judged by the hidden evaluator.
5. **Empirical difficulty.** Régie's baseline mode (§7) runs each ladder case three to
   five times. Success rate, time, tokens, and revisions define the case's empirical
   difficulty. The baseline measures whether a task fits one agent; poker estimates are
   calibrated against it.
6. **Calibration questions:** does the poker size predict the actual outcome? Above
   the split threshold, does splitting beat the baseline? Below it, is refinement
   without splitting at least as good as the baseline?
7. **Overfitting guard** (unchanged): Taskboard is the fast regression track; the
   Python CLI holdout and at least two ladder cases are held out from day-to-day
   tuning; nothing is accepted on Taskboard results alone. Public datasets (a
   SWE-bench Verified slice) are a later outside check.

## 7. Architecture

### State model

A **request** (one run) holds the spec, the question-and-answer log, follow-ups,
release records, and increments. An **increment** holds its criteria, estimate (all
poker rounds with evidence), tasks, review result, stakeholder decisions, and its
accepted commit. A **task** holds file scope, dependencies, start commit, and attempts.

Each entity has an explicit transition table:

- request: `intake → awaiting-spec-approval → active → releasing → done`, with
  `releasing → awaiting-merge → active` after an early release, and `halted` reachable
  from any state (resume returns to the state it halted in);
- increment: `draft → refining → ready → building → in-review → awaiting-stakeholder → accepted`,
  with `in-review → building` for the one revision round and
  `awaiting-stakeholder → building` on *request changes* or *fix*;
- task: `pending → building → gated → integrated`, with `building → failed` and
  `failed → pending` on resume.

Every change goes through one `transition(entity, event)` function that rejects
events the table does not allow and appends an event to `events.jsonl`. No other code
mutates lifecycle fields. The persisted format is `state.json` schema v2; runs created
before v2 are not resumable (there are no users to migrate).

### Framework rules

A deterministic `framework` module owns: spec and increment Definition of Ready,
Definition of Done (gates plus the tests-prove-change check), review-contract
validation, poker agreement logic, timeboxes and budgets, and WIP limits. The engine
loop reads state, asks the rules which events are allowed and which role acts next,
dispatches that role, and applies the resulting transition. This replaces the linear
`plan → tasks → finalize → pr` stage chain.

### Roles and profiles

Four profiles: `product-owner` (new prompt), `developer` (building and estimating),
`reviewer`, and `debugger`. Every dispatch goes through one shared **ladder helper**
that walks the profile's bindings, records an `Attempt` for every call, and applies
quota and budget rules, replacing today's six copies of that loop.

### Kept

Agent dispatch and the write-ahead intent log (adapted to the new entity IDs),
gates, git operations (worktrees, integration, squash, PR), provider health and quota
circuits, provider adapters, repository research, knowledge and reflection (the
retrospective), `regie stats` (extended with calibration), notifications, and
onboarding (extended to detect a demo command).

### Deleted

The workflow tiers and `--workflow` flag; keyword routing and risk inference
(`route_brief`, `infer_risks`, specialist triggers); the plan stage, planner, and
plan-review panels; the Product Owner as arbiter and its recovery paths;
finding-similarity matching; revision counters and recovery flags; specialist,
design, and integration reviewers; checkpoints; the test-writer/builder split; the
`regie approve` and `regie answer` commands (replaced by `regie decide`); and the
corresponding profiles.

### Baseline mode

`regie run --baseline` runs request → one developer → the configured gates (setup,
build, test, lint) → done, with no Product Owner, no review, and no increments. It exists only to measure
empirical difficulty; regie-bench exposes it as `--baseline`, replacing the spike's
`--workflow` passthrough.

## 8. Delivery phases

Régie's redesign is itself delivered in small increments. Each phase gets its own
implementation plan and ends with a working Régie, a green test suite, Taskboard at
3/3 over three repeats, (from Phase 1) no regression on the feature ladder, and the
README, `docs/ARCHITECTURE.md`, and `docs/USAGE.md` updated for what changed.

- **Phase 0 — bench foundations** (regie-bench, plus Régie's baseline mode):
  measurement fixes, stakeholder simulator, the feature ladder with validated
  reference solutions, and the ladder's empirical difficulty.
- **Phase 1 — framework core and Product Owner intake:** state model v2 with
  transition tables and property tests; the Product Owner interview, spec Definition
  of Ready, and the tiny-request fast path; single-increment requests only, executed
  by the existing direct-owner loop adapted to the new model; the Inbox tab for
  questions and spec approval. Deletes tiers, keyword routing, the plan stage, and
  plan-review panels.
- **Phase 2 — working agreements:** developer role, tests-prove-change check, review
  contract with follow-ups and open blockers, the shared ladder helper. Deletes the
  Product Owner arbiter, similarity matching, recovery flags, specialists, the
  integration reviewer, and the test-writer split.
- **Phase 3 — increments and feedback:** multi-increment specs, just-in-time
  refinement, review packets, the demo command, stakeholder decisions, one commit per
  increment, release and early release; the Board tab; `regie inbox`/`decide`/`demo`.
- **Phase 4 — refinement:** planning poker, spikes, the split rule, parallel tasks
  within an increment, and calibration of the split threshold on the ladder.

## 9. Testing

- **Unit tests** for every rule: both Definitions of Ready, Definition of Done
  including tests-prove-change, review-contract validation, poker agreement outcomes,
  and each transition table.
- **Stateful property tests** (Hypothesis `RuleBasedStateMachine`, new dev dependency)
  per lifecycle, asserting invariants: only table-allowed transitions occur; an
  accepted increment's commit is reachable from the run branch; building never starts
  before the previous increment is accepted; a blocker always cites an existing
  criterion ID; cumulative spend never decreases; every intent has at most one attempt.
- **Flow tests** with the fake adapter and a scripted stakeholder, covering the fast
  path, multi-increment acceptance, request changes, open blockers, follow-ups, early
  release, and resume after a crash at each state.
- **Bench** as system-level evidence at the end of every phase.

## Risks

- **Product Owner asks too much or too little.** Measured on the ladder: questions per
  case, defaults taken, and acceptance outcome.
- **Planning poker costs more than it saves.** Measured in Phase 4 against a single
  estimator; if poker does not beat it, the single estimator replaces it.
- **Simulator noise.** Fixed binding, answers logged, repeated trials.
- **The synthetic ladder is too easy.** Empirical difficulty reveals it; the ladder is
  extended until it has cases the baseline fails.
- **Rewrite risk.** Phased delivery with a working, benched Régie after every phase.

## Non-goals

- Migrating or resuming pre-v2 runs.
- Multi-repository requests (parent/child run links remain the foundation).
- Jira or other ticket-system intake.
- Screenshots or recordings in the review packet.
- A web dashboard.
- Separate specialist reviewers, unless the bench shows they add value.
- A public-dataset track (SWE-bench slice) before the feature ladder is in place.
