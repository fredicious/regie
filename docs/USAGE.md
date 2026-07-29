# Using Régie

## What works today (Plan A)

The orchestrator core is complete and tested end-to-end: state machine, crash-safe
run directories, gates (TDD-red, diff guard, lint/test commands), escalation
ladder, and the `regie` CLI. **Real Claude/Codex adapters are not wired yet**
(that's Plan B) — today every profile binds the built-in fake adapter, so you can
exercise the full pipeline mechanics but Régie does not yet drive real coding
agents. This doc describes the workflow as it will be used, flagging Plan A
stand-ins where they exist.

## Install

No Python knowledge needed — [uv](https://docs.astral.sh/uv/) manages everything:

```bash
# from the repo (dev install, editable):
cd ~/Code/regie && uv tool install --editable .

# check:
regie --help
```

`uv tool install` puts a `regie` command on your PATH with its own isolated
environment — the Python-ecosystem equivalent of a brew-installed binary. When
the project is published to PyPI this becomes `uv tool install regie` anywhere.
(A literal single-file binary via PyInstaller/PyApp is possible later if we
distribute widely; see ROADMAP.)

## One-time setup per target repo

Create `regie.toml` at the target repo's root:

```toml
test_globs = ["tests/**", "apps/*/tests/**", "**/*.test.*"]
eval_trigger_globs = ["apps/api/**"]          # optional
binding_strength = ["codex:gpt-5.x", "claude:strongest"]  # weakest → strongest

[commands]
test = "make test"        # must exist — gates call ONLY these commands
lint = "make lint"
```

The `[commands]` entries are the deterministic gates. If the repo has no
canonical `make test`/`make lint` targets, add them first — Régie hardcodes
nothing else.

Agent profiles (who does what, on which model, with what budgets) live in this
repo's `profiles/` directory: `planner`, `test-writer`, `builder`, `reviewer` —
a `.yaml` (binding + budgets) and `.md` (role prompt) each. Edit the prompts to
bake in your coding values; every attempt records the prompt hash so you can
correlate prompt versions with outcomes.

## Running

```bash
# 1. Write a brief (goal, in scope, OUT of scope, acceptance signals)
$EDITOR feature.md

# Plan A stand-in: the planner stage isn't wired yet, so tasks come from a
# tasks.json next to the brief (Plan B replaces this with the spec-writing agent):
# [{"id": "T1", "title": "...", "profile": "builder",
#   "criteria": ["Given ... When ... Then ..."], "depends_on": []}]

# 2. Start the run
regie run feature.md --repo ~/Code/ai-search-platform

# 3. Watch it
regie status 2026-07-29-feature
tail -f ~/.regie/runs/2026-07-29-feature/events.jsonl | jq

# 4. If it halts (budget exhausted, blocked question, quota):
#    read the halt reason, fix the spec/answer the question, then
regie resume 2026-07-29-feature --repo ~/Code/ai-search-platform
```

Per task, the pipeline runs: **test-writer** (tests must fail honestly —
assertion/NotImplementedError, verified) → **builder** (tests pass, lint clean,
and a git-diff guard proves it never touched a test file) → **reviewer**
(findings with blocker/major/minor severity; only blockers/majors loop back;
minors are recorded for the PR). Each stage gets 3 attempts (2 on its binding,
1 escalated to the next-stronger binding), then the run halts and tells you.

## The run directory

Everything lives under `~/.regie/runs/<run-id>/` (override root with
`$REGIE_HOME`) — outside the target repo, so transcripts can never be committed:

```
state.json        # authoritative state — regie status pretty-prints this
events.jsonl      # append-only observability log (dispatches, gates, usage)
intent.jsonl      # write-ahead log powering crash-safe resume
brief.md  decisions.md
tasks/T1/         # context.md (exact packet sent), attempt-N.out (transcripts),
                  # note-*.md (gate failures / review findings fed to retries),
                  # findings.json / minor-findings.json
```

Debugging a baffling agent decision starts at `tasks/<id>/context.md` (exactly
what it was told) and `attempt-N.out` (exactly what it did).

## Current limitations (removed in Plan B)

- Fake adapter only — no real `claude`/`codex` dispatch yet.
- No planner stage (tasks.json stand-in), no git branch/squash/push/PR stages,
  no desktop notifications, no `regie approve` checkpoint yet.
- Reviewer binding-flip (cross-model rule) enforced at dispatch arrives with
  real adapters.
