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
