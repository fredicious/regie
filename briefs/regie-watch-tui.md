# regie watch — live TUI control room

Add a `regie watch` command: a Textual TUI that live-monitors runs by reading
`$REGIE_HOME` (state.json + events.jsonl only — it is a pure reader and must
never write anything under the run directories).

## In scope

- New module `src/regie/watch.py` with a Textual `App` and a `regie watch`
  CLI command (optional argument: a run id to open directly; default opens
  the most recently modified run).
- Layout, three zones:
  - Left rail "CHANNELS": every run under `$REGIE_HOME/runs`, newest first,
    each row showing run id and a status lamp (see tally semantics). Arrow
    keys / click select a run.
  - Main panel "SIGNAL CHAIN" for the selected run: one row per task in
    ordered_task_ids order (plus PLAN when planner_attempts exist), each row
    showing the task id + title and three stage lamps (test, build, review)
    with the attempt count per stage; under it a compact attempts table for
    the selected task: stage, binding (cli:model), outcome, turns.
  - Bottom strip "LOG": the last 12 lines of events.jsonl rendered as
    `HH:MM:SS  task/stage  outcome  turns=N`, appending live.
- Live updates by polling mtimes every 2 seconds (no watchdog dependency);
  a poll must never crash the app on partially-written JSON (skip and retry —
  state.json writes are atomic but events.jsonl can grow mid-read).
- Tally semantics (the control-room signature; encode in one place):
  - running / stage in progress: red lamp ● labelled ON AIR (a régie's red
    tally means live, not error)
  - done: dim green lamp
  - halted / blocked: amber lamp, halt reason shown in the main panel header
  - pending: gray hollow lamp ○
- Visual identity via Textual CSS: dark slate console background (#14181D
  panels #1D232B, hairlines #2E3742), monospace everywhere (terminal-native),
  vendor color accents in the bindings column: claude entries in clay
  (#D97757), codex entries in teal (#10A37F). Uppercase, letter-spaced zone
  titles (CHANNELS / SIGNAL CHAIN / LOG).
- `q` quits; `r` forces refresh. Footer shows key bindings.

## Out of scope

- Any write/mutation of run state; no resume/approve actions from the TUI.
- Web serving (textual serve works out of the box later; do not add code for
  it), transcripts viewer, scrollback/search in the log, config files.
- No new runtime dependencies beyond textual (already added to the project).

## Acceptance signals

- With two seeded fake run dirs, launching the app shows both in CHANNELS,
  newest first, with correct lamps (one done → green, one halted → amber).
- Selecting a halted run shows its halt reason and per-stage attempt counts
  matching state.json; the attempts table shows binding strings colored by
  vendor.
- Appending a line to the selected run's events.jsonl is reflected in LOG
  within one poll interval (use Textual's Pilot test framework with the poll
  timer driven manually or a short interval).
- A truncated/partial state.json read never crashes the app (test by writing
  garbage then valid content).
- `regie watch` exits cleanly on `q`; all existing tests stay green.

## Testing notes

Textual apps are tested headlessly with `App.run_test()` / Pilot (pytest,
asyncio). Test the pure helpers (run scanning, event-line formatting, lamp
mapping) as plain functions, and the app itself with Pilot: mount, assert
widgets/text, simulate key presses. pytest-asyncio is available as a dev
dependency.
