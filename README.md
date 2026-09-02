# Régie

**Product brief in → reviewed, tested, CI-green PR out.**

Régie is an adaptive agent execution engine. Like the *régie* of a TV
production — the control room where the director watches every feed and cues
specialized operators — it takes a product spec and dispatches specialized agent
profiles (direct owner, planner, Product Owner, test-writer, builder, reviewer, debugger, and
risk specialists) across model providers (Claude Code, Codex CLI; local models
later), each task to the best tool, with no human orchestration in between.

Quality is enforced deterministically, not by vibes: an end-to-end owner for
low-risk changes, mechanically separated test/implementation authors when planning is
earned by scope or risk, cross-model
adversarial review with severity triage (Red/Blue/Green gate protocol), and
bounded retry/escalation ladders. Everything a run does lives on disk —
inspectable, resumable, auditable.

The workflow adapts to risk. Auto and fast changes begin with one direct owner,
mechanical gates, and one focused review—without an upfront planner or test-writer.
The owner escalates only when repository evidence requires planning. Standard and critical runs add
repository research, selective knowledge priming, independent plan reviewers,
risk-triggered design/code specialists, planned human checkpoints, parallel DAG
execution in isolated worktrees, and a final cross-task integration review.

## Status

The default pipeline runs brief→direct owner→gates→focused review→PR. Risky or
explicitly planned work expands to research→reviewed plan→TDD task DAG→integration
review→PR shepherd→reflection. The Token Governor adds stage-specific context,
normalized token telemetry,
profile-level effort/tool/sandbox policies, smarter escalation, and an optional
direct OpenAI Responses API adapter. Running `regie` opens the terminal control
room: compose briefs, switch runs, inspect the task DAG/evidence/event ledger,
watch every sub-agent's provider, model, runtime, heartbeat, turns, and tokens,
approve checkpoints, resume work, and read artifacts without leaving the app.
On an unconfigured project, its first screen detects and reviews the local
test/lint/build setup and enabled Claude/Codex providers before creating
`regie.toml`. Provider choices remain editable from the control room. See:

Every isolated worktree is dependency-bootstrapped before the first agent call.
Unavailable tools, registries, browsers, or build artifacts halt as
infrastructure problems without consuming another model attempt or discarding a
valid patch. Build-dependent suites run only after the build gate.

When reviewed plans or implementation feedback fail to converge, Régie invokes
a bounded, read-only Product Owner advisor. It may consolidate feedback into
one final revision, reject advisory findings that do not follow from the brief,
or ask for human
authority. The state machine still owns transitions and never lets that
decision waive mechanical validation, tests, security gates, provider policy,
or budgets.

- `docs/superpowers/specs/2026-07-29-regie-v1-design.md` — v1 design
- `docs/USAGE.md` — install (via `uv tool install`) and usage
- `docs/ROADMAP.md` — deferred v1.5/v2/v3 ideas

## Run Régie

```
regie                             # control room scoped to the current folder
regie run brief.md --repo <path>   # start a run: brief → spec → tasks → build → PR
regie watch <run>                  # open the control room focused on one run
regie run brief.md --repo <path> --workflow critical  # force full-risk workflow
regie init --repo <path>            # detect tooling and create regie.toml
regie approve <run>                 # release a spec or planned risk checkpoint
regie answer <run> "decision"       # answer a material clarification
regie resume <run> --repo <path>    # resume after crash, halt, quota limit, or approval
regie status <run>                  # pretty-print state.json
regie handoff <run>                 # render an exact continuation packet
regie knowledge-approve <run>       # promote reviewed learning candidates
regie providers                     # check configured adapter availability
regie provider-status               # show provider quota cooldowns/reset times
regie provider-reset <cli>          # manually clear a verified-recovered provider
regie clean <run> --repo <path>     # remove a finished run's worktree and branch
```

## Inspiration and credit

Régie's expanded lifecycle was materially inspired by
[MetaSwarm](https://github.com/dsifry/metaswarm) by Dave Sifry and its
contributors. In particular, MetaSwarm demonstrated the value of independent
plan-review lenses, selective design specialists, fresh adversarial reviewers,
planned checkpoints, final integration review, knowledge priming and reflection,
and a PR shepherd. Régie re-expresses those ideas in its own architecture: a
provider-neutral Python state machine, schema-validated contracts, mechanically
separated test and implementation authors, orchestrator-owned git, and an
external auditable run ledger. See [Acknowledgements](docs/ACKNOWLEDGEMENTS.md)
for the detailed credit and boundary.

Régie's lean execution ladder is also inspired by
[Ponytail](https://github.com/dietrichgebert/ponytail) by Dietrich Gebert: minimize
the implementation and the workflow, never the understanding, safety, or
validation. Régie applies that principle to orchestration itself—complexity must
be earned by evidence.

## Benchmarking

[Régie Bench](https://github.com/fredicious/regie-bench) provides repeatable
product briefs, isolated fixture repositories, hidden acceptance checks, and
fresh/cached token, cost, routing, and latency comparisons. Benchmark targets set
`workflow.submit_pr = false` so every implementation and review gate runs without
creating real branches or pull requests.
