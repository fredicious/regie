# Milestone 1: live run notes

**Date:** 2026-11-19
**Binary:** `regie 0.1.0`, built from `3be3325`, installed with `cargo install --path crates/regie`.
**Repo:** `fredicious/regie-bench` (uv, GitHub CI job `verify`: ruff, pytest, smoke, validate).
**Task:** T1 "Add --version flag", level light (Claude Sonnet 5.5, high effort).

## What happened

| Step | Result |
|---|---|
| `regie session new`, `regie project set`, `regie task new` | Worked from the Régie folder; repo given by path. |
| Worktree and setup | `~/.regie/worktrees/t1-add-version-flag` on `regie/t1-add-version-flag`, `uv sync --frozen` ran. |
| Builder (Initial) | Added the flag and a test, ran the checks, committed with the `Regie-Agent: builder, claude, claude-sonnet-5-5, light` trailer, ended `Status: done`. |
| Reviewer (fresh Claude) | Round 1, no findings (`{"findings": []}`), so review closed. |
| Builder (OpenPr) | Pushed and opened fredicious/regie-bench#1 with `gh pr create`; didn't merge. |
| CI | `verify` passed; merge state CLEAN. |
| Ready to merge | Reached after CI. |
| Frédéric merged on GitHub | Régie showed Merged within a poll, removed the worktree and the local branch. |

**Timing:** about 3 minutes from `task new` to Ready to merge. Agent run time 83 s over 3 runs; 5.4k output tokens and 677k cache-read tokens.
**Nobody was asked anything:** no human question, no fix round, no escalation.

## Issues seen

1. **Transient Ready to merge before CI registered.** The first poll after the PR opened saw no checks and a CLEAN merge state, so the card showed Ready to merge for one poll while `verify` was in fact starting. The next poll moved it back to CI. Cause: the "no CI and clean means ready" rule added in the final fix wave. This was the parked minor "transient Ready before checks register". **Fix first:** only treat "no checks" as ready after the PR has been seen with no checks for a few minutes, or when the repo has no workflows.
2. **Two sessions named "bench".** A first `regie session new` had already created session 1. Harmless, but there's no way to delete or rename a session yet.
3. **Old Python Régie shared `~/.regie`.** The old tool's run folders sit next to the new worktrees in `~/.regie/worktrees`. No clash, but worth cleaning up.
4. **Installing.** The old Python `regie` (a uv tool in `~/.local/bin`) shadowed the new binary until it was uninstalled; the terminal also needed `source ~/.cargo/env` and `rehash` after the Rust install.

## Not exercised in this run

- Standby and Resume during a real Claude run, and killing `regie serve` mid-run (plan step 5). Covered by tests with the fake runner only.
- A fix round, a builder question, a CI failure or a conflict on a real repo.
- The 60-second git limit on a large repo (parked; this repo is small).

## Next

- Fix issue 1 before relying on the Ready lane.
- Exercise pause and kill-and-restart on a real run.
- Then milestone 2 (the lead), after discussing whether the lead picks models for agents itself.

## Deferred review findings (from the execution ledger)

Carried here so they outlive the ledger. The final review triaged them; "first after the live run" ones are marked in its report summary above.

- Task 3: minor (deferred): RawKind/RawEvidenceType duplicate core enums; fence open marker only exact lowercase `json`.
- Task 3: minor (deferred, carry to Task 4/11): parse_verify doesn't check every finding id was answered — consumer must treat a missing id as not fixed.
- Task 3: minor (deferred): parse_builder_status doc comment stale after R7 (findings.rs:23).
- Task 5: minor (deferred): no tests at exactly weekly_floor / 10 points ahead; pick sort has no task-id tie-break; mid-file `use` in usage.rs.
- Task 5: minor (carry to Task 11): pick treats a provider missing from Capacity.openness as Open — engine must fill every provider.
- Task 6: minor (deferred): Provider list hardcoded in views usage(); LiveView.active unused.
- Task 7: minor (carry to Task 13): set busy_timeout when the server opens its second read-only connection. Minor (deferred): synchronous=NORMAL comment; HOME unset falls back to ./.regie; no corrupt-row test.
- Task 8: minor (deferred, final review should triage as likely-fix): partial-failure cleanup in create_worktree; git can hang on credential prompt while holding the lock (GIT_TERMINAL_PROMPT=0, ssh BatchMode); `git remote show origin` fallback is locale-dependent (Frédéric may run a French locale) — use LC_ALL=C or `ls-remote --symref`; empty slug for non-ASCII/empty titles; run_setup has no timeout/kill_on_drop; remove_worktree not idempotent; RepoLocks keyed by raw path; no lock-contention test.
- Task 8: minor (deferred): symlink escape in copy_files; test writes `x` into the temp dir's parent (stray file outside TempRepo); git.rs:95 line length.
- Task 9: minor (deferred, final review should triage — "never say ready when it isn't"): unlisted failing CheckRun conclusions (e.g. STARTUP_FAILURE) and unknown StatusContext states count as Success; explicit JSON null for rollup fields fails parse; bad timestamps become Millis(0); aggregate_checks tests cover only FAILURE.
- Task 10: minor (carry to Task 11): MessageDelivered only when delivery is confirmed (a message racing the end of a turn stays pending).
- Task 10: minor (carry to Task 12/14): agents run in their own process group — recover() signals the group (-pid); `regie serve` installs a SIGINT/SIGTERM handler that stops every run.
- Task 10: minor (deferred): select drain race can drop the final message (fails safe → Crashed); interrupt racing a successful result; run-file flush before Finished; orphaned grandchildren after normal exit; stdin writes inline; no timeout after stdout EOF; limit detection relies on rate_limit_event status only; stop test doesn't prove grandchild death; reviewer.md uses origin/HEAD; probe runs in regie's cwd.
- Task 11: minor (deferred, final review should triage): delivery confirmed by output already in flight (needs a runner ack line); FixNotPushed flashes NeedsMe on the board until the post-fix look returns (and stays 30 s if the look errors); hung gh stops polling for the task (no timeout); in-memory state changed before a failing append (RunFinished, WorktreeCreated) can strand a task; advance stops the whole pass on one task's append error; canonicalize on the loop; real 5 s ticker runs in tests; missing-PR count in memory only.
- Task 12: minor (deferred, final review should triage): recover kills group only when leader alive (group members may survive); pause relies on Interrupt being honoured (no escalation to Stop); runner SIGKILL only while leader unreaped; Retry accepted while a run is active (lost); setup still runs for a task cancelled during worktree creation; idle_ticks 100 ms sleep; cosmetic Purpose::Review in a builder test.
- Task 12: minor (deferred): Régie killed between init and RunAttached resends the input; resent CiFix uses current checks; engine tests only cover Initial/Message resends; real Interrupt-before-attach not modelled by the fake.
- Task 13: minor (deferred, final review should triage): sync SQLite in async tasks (spawn_blocking); EngineHandle::task uses Store::open without busy_timeout and swallows errors (switch to open_reader); run-output reads whole file / non-UTF8 → 404; malformed command body gives 415/422 not 400 JSON; no X-Frame-Options/frame-ancestors or Referrer-Policy; server.json written in place; wrong Bearer blocks a valid cookie; test gaps (Origin null, other port, real TCP Host check); serve has no graceful shutdown and leaves server.json.
- Task 14: minor (deferred): token in `open` argv briefly (plan-mandated URL); health() may send the token to a reused port; signal registration failure silent; detached child not checked in the 5 s poll; quit timeout leaves a stuck agent until next recover.
- Task 15: minor (deferred): SSE backoff not reset on open; PR link scheme unchecked; raise offered on finished tasks; tests don't cover TopBar commands/tally edge cases.
- Task 16: minor (deferred): web_is_built checks only index.html; first src= split brittle; 404 lacks headers.
- Final: parked — 60 s limit on every git call can kill a legitimate fetch/worktree add on a large or LFS repo, leaving a branch that makes Retry fail — Ruling: real, deferred (no second fix wave); pick a small repo for the live run; first fix after it.
- Final: parked minors — transient Ready before checks register; no-CI + required approval stays in CI; is_error with no text shows no reason; RunCrashed may show an older message; ChangesRequested shows the last comment; dev proxy rewrites Origin; GIT_SSH ignored; cancelled calls kill only the leader; run_setup has no timeout; gh --head matches forks; HAS_HOOKS → Unknown; run-anyway during a real block retries each tick.
