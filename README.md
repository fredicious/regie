# Régie

**Product brief in → reviewed, tested, CI-green PR out.**

Régie is a multi-model agent orchestration engine. Like the *régie* of a TV
production — the control room where the director watches every feed and cues
specialized operators — it takes a product spec and dispatches specialized agent
profiles (planner, test-writer, builder, reviewer, debugger, and risk specialists) across
model providers (Claude Code, Codex CLI; local models later), each task to the
best tool, with no human orchestration in between.

Quality is enforced deterministically, not by vibes: spec-driven development,
TDD with mechanically separated test/implementation authors, cross-model
adversarial review with severity triage (Red/Blue/Green gate protocol), and
bounded retry/escalation ladders. Everything a run does lives on disk —
inspectable, resumable, auditable.

The workflow adapts to risk. Fast changes retain the mechanical TDD and quality
gates without paying for review committees; standard and critical runs add
repository research, selective knowledge priming, independent plan reviewers,
risk-triggered design/code specialists, planned human checkpoints, parallel DAG
execution in isolated worktrees, and a final cross-task integration review.

## Status

The full pipeline runs brief→research→reviewed plan→TDD task DAG→integration
review→PR shepherd→reflection. The Token Governor adds stage-specific context,
normalized token telemetry,
profile-level effort/tool/sandbox policies, smarter escalation, and an optional
direct OpenAI Responses API adapter. See:

- `docs/superpowers/specs/2026-07-29-agent-harness-v1-design.md` — v1 design
- `docs/USAGE.md` — install (via `uv tool install`) and usage
- `docs/ROADMAP.md` — deferred v1.5/v2/v3 ideas

## CLI

```
regie run brief.md --repo <path>   # start a run: brief → spec → tasks → build → PR
regie run brief.md --repo <path> --workflow critical  # force full-risk workflow
regie init --repo <path>            # detect tooling and create regie.toml
regie approve <run>                 # release a spec or planned risk checkpoint
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
