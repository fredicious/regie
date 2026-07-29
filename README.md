# Régie

**Product brief in → reviewed, tested, CI-green PR out.**

Régie is a multi-model agent orchestration harness. Like the *régie* of a TV
production — the control room where the director watches every feed and cues
specialized operators — it takes a product spec and dispatches specialized agent
profiles (spec-er, decomposer, test-writer, builder, reviewer, debugger) across
model providers (Claude Code, Codex CLI; local models later), each task to the
best tool, with no human orchestration in between.

Quality is enforced deterministically, not by vibes: spec-driven development,
TDD with mechanically separated test/implementation authors, cross-model
adversarial review with severity triage (Red/Blue/Green gate protocol), and
bounded retry/escalation ladders. Everything a run does lives on disk —
inspectable, resumable, auditable.

## Status

Design phase. See:

- `docs/superpowers/specs/2026-07-29-agent-harness-v1-design.md` — v1 design
- `docs/ROADMAP.md` — deferred v1.5/v2/v3 ideas

## Planned CLI

```
regie run brief.md      # start a run: brief → spec → tasks → build → PR
regie watch <run>       # live terminal dashboard of the state machine
regie resume <run>      # resume after crash, halt, or quota limit
```
