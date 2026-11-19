# Milestone 1: one task, started by hand — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** a `regie` binary that takes one task, started from the CLI or the API, from a fresh worktree through a Claude builder, a Claude reviewer with fix rounds, the builder's PR, CI and merge to cleanup, with a live board in the browser.

**Architecture:** a Cargo workspace with a pure `regie-core` crate (events, the in-memory state built from them, the task flow, lanes, thresholds, scheduling, parsing of agents' final messages) and a `regie` crate that does all I/O (SQLite event log, git, `gh`, `claude` processes, HTTP server with an event stream, CLI). One engine task owns the state and reacts to commands, agent events and timers. The web page is Preact + TypeScript, built by Vite and embedded in the binary.

**Tech Stack:** Rust stable (tokio, axum, rusqlite with `bundled`, serde, ts-rs, clap, reqwest with rustls, rust-embed, tracing, uuid, anyhow, thiserror, async-trait), Node 26 with pnpm 12, Vite, Preact, `@preact/signals`, Vitest, `gh` 2.101, Claude Code 2.1.328.

**Spec:** `docs/superpowers/specs/2026-11-19-lead-agent-orchestrator-design.md` (milestone 1 in section 9). Requirements: `docs/requirements/2026-11-19-lead-agent-orchestrator.md` (revision 4).

## Global Constraints

- The server listens on `127.0.0.1` only, checks a per-launch token, and checks the request origin and host (R11).
- Data directory: `~/.regie/` by default, overridable with `--data-dir`. Layout: `regie.db`, `server.json`, `runs/<run id>.jsonl`, `worktrees/`.
- `regie-core` depends on no crate that does I/O (no tokio, rusqlite, reqwest, std::process). It may use serde, serde_json, ts-rs, thiserror.
- Claude agents: `claude -p` in the task's worktree with `--permission-mode auto` and Frédéric's full setup (no `--setting-sources`, no sandbox settings, no tool allowlist).
- Levels (Claude): light = `claude-sonnet-5-5` at `high`; standard (default) = `claude-opus-5-5` at `medium`; hard = `claude-opus-5-5` at `high`.
- Thresholds: a provider is open when its 5-hour window is under 75% used, and its 7-day window is at or under 20% used or no more than 10 points ahead of the week's elapsed share. Blocked after a real limit hit until the window resets.
- Caps (fixed in this milestone): at most 4 running agents overall, at most 2 tasks with a running agent per session.
- Review: a round is one review then one fix; at most 2 rounds, then a verify-only pass on the round-2 findings. A blocking finding without evidence (a command, a failing test, or a criterion with file and line) is advisory.
- Post-PR loop caps: 3 CI fixes, 3 conflict fixes, 3 PR-feedback fixes per task; at the cap the task waits for Frédéric.
- Branch name `regie/t<id>-<slug>`. Each task gets `REGIE_PORT_BASE` (first of a block of 10 ports from 41000, 200 blocks), `COMPOSE_PROJECT_NAME=regie-t<id>`, and `REGIE_TASK=T<id>`.
- Commit trailer the builder is told to add: `Regie-Agent: builder, claude, <model>, <level>`.
- Nobody but Frédéric merges. Régie never deletes remote branches.
- Usage probe: `claude -p` on `claude-haiku-5-5` at `low` effort, no tools, `--strict-mcp-config`, sent only when the latest Claude reading is older than 10 minutes and Claude work is about to start.
- Agent output files are kept 14 days.
- `caffeinate -i` is held while any agent runs.
- Web: every tally light has a text label; colour is never the only signal; light and dark follow the system; no blinking under `prefers-reduced-motion`.

## Review Focus

1. **A clone without `origin/HEAD`** (common for older clones): the default branch must still be found, from `git remote show origin`, not fail the task. Test: Task 8, `default_branch_without_origin_head`.
2. **A builder final message without a `Status: done` last line**, or with text after it: treated as a question for Frédéric, never as done. Test: Task 3, `status_line_must_be_last`.
3. **A reviewer message with no JSON block, or broken JSON:** the review is "unparsed" and waits for Frédéric with the raw text; it must never count as a clean review. Tests: Task 3, `review_without_json_is_unparsed`; Task 4, `unparsed_review_needs_human`.
4. **A `claude` process that dies without a `result` event:** the run ends as crashed and the task shows Needs me with a Retry, instead of sitting in Working forever. Tests: Task 10, `exit_without_result_is_crash`; Task 11, `crash_needs_me_then_retry`.
5. **Two tasks on the same repo starting at once:** `git worktree add` and `git fetch` on one repo are serialised so they don't trip over git's lock files. Test: Task 8, `parallel_worktrees_same_repo`.

---

### Task 1: Toolchain, workspace and repo notes

**Files:**
- Create: `Cargo.toml`, `rust-toolchain.toml`, `.cargo/config.toml`, `crates/regie-core/Cargo.toml`, `crates/regie-core/src/lib.rs`, `crates/regie/Cargo.toml`, `crates/regie/src/main.rs`, `CLAUDE.md`, `AGENTS.md` (a symlink to `CLAUDE.md`), `Makefile`
- Modify: `.gitignore` (add `target/`, `web/node_modules/`, `web/dist/`)

**Interfaces:**
- Produces: a workspace where `cargo test` runs both crates; `TS_RS_EXPORT_DIR` pointing at `web/src/api/gen`.

- [ ] **Step 1: Install Rust** (ask Frédéric first; it writes to `~/.cargo` and `~/.rustup` and his shell profile)

Run: `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable` then `source ~/.cargo/env && cargo --version`
Expected: a `cargo 1.x` version line.

- [ ] **Step 2: Create the workspace**

`Cargo.toml`: `[workspace] members = ["crates/regie-core", "crates/regie"]`, `resolver = "2"`, shared `[workspace.dependencies]` for the crates in the tech stack. `rust-toolchain.toml`: `channel = "stable"`. `.cargo/config.toml`: `[env] TS_RS_EXPORT_DIR = { value = "web/src/api/gen", relative = true }`. `regie-core` depends only on serde, serde_json, ts-rs, thiserror. The `regie` binary's `main` prints the version.

- [ ] **Step 3: Add a smoke test and run it**

`crates/regie-core/src/lib.rs`: `#[test] fn workspace_builds() { assert_eq!(2 + 2, 4); }`
Run: `cargo test`
Expected: `test workspace_builds ... ok`.

- [ ] **Step 4: Write `CLAUDE.md`**

Short: what Régie is (one line, link to the design), the two crates and the rule that `regie-core` does no I/O, where prompts and the web page live, and the commands: `make test` (`cargo test && pnpm -C web test && pnpm -C web typecheck`), `make web`, `make build`, `make dev`. `Makefile` targets match. `AGENTS.md` is a symlink to it.

- [ ] **Step 5: Commit**

```bash
git add Cargo.toml rust-toolchain.toml .cargo crates CLAUDE.md AGENTS.md Makefile .gitignore
git commit -m "build: Rust workspace for the lead-agent orchestrator"
```

---

### Task 2: Core types: ids, settings, project facts, events, commands

**Files:**
- Create: `crates/regie-core/src/{ids.rs, settings.rs, facts.rs, github.rs, findings.rs (types only), usage.rs (types only), event.rs, command.rs}`
- Test: in-module `#[cfg(test)]`

**Interfaces:**
- Produces (all `Serialize, Deserialize, TS, Clone, Debug, PartialEq`; ids also `Copy, Eq, Ord, Hash`):
  - `SessionId(#[ts(type = "number")] pub i64)`, `TaskId(..)`, `RunId(..)`, `EventId(..)`, `Millis(..)`, all `#[serde(transparent)]`. `TaskId` displays as `T<n>`, `RunId` as `R<n>`.
  - `enum Provider { Claude, Codex }`, `enum Level { Light, Standard, Hard }` (`Default = Standard`, ordered so `Light < Standard < Hard`), `enum Effort { Low, Medium, High, Xhigh, Max }`, `struct ModelChoice { model: String, effort: Effort }`.
  - `struct Settings { caps: Caps { overall: u32, per_session: u32 }, thresholds: Thresholds { five_hour_max: f64, weekly_floor: f64, weekly_ahead_max: f64 }, loops: LoopCaps { review_rounds: u32, ci: u32, conflict: u32, pr_feedback: u32 }, probe_after_ms: i64, output_retention_days: u32, port_base: u16, port_blocks: u16 }` with `Default` = the Global Constraints values, and `fn model_for(&self, provider: Provider, level: Level) -> ModelChoice`.
  - `struct ProjectFacts { setup: Option<String>, build: Option<String>, test: Option<String>, lint: Option<String>, copy_files: Vec<String>, definition_of_done: String }`.
  - In `github.rs`: `enum PrLifecycle { Open, Merged, Closed }`, `enum Checks { None, Pending, Success, Failure { failed: Vec<String> } }`, `enum MergeState { Clean, Blocked, Behind, Unstable, Dirty, Unknown }`, `enum ReviewDecision { Approved, ChangesRequested, ReviewRequired }`, `struct PrState { number: u64, url: String, lifecycle: PrLifecycle, checks: Checks, conflicting: bool, merge_state: MergeState, review_decision: Option<ReviewDecision>, head_sha: String }`, `struct PrComment { id: String, author: String, body: String, changes_requested: bool, at: Millis }`.
  - In `findings.rs` (types): `enum FindingKind { Bug, Security, MissingTest, UnmetCriterion, Design, Other }`, `enum EvidenceType { Command, Test, Criterion }`, `struct Evidence { r#type: EvidenceType, detail: String, file: Option<String>, line: Option<u32> }`, `struct Finding { id: u32, title: String, kind: FindingKind, blocking: bool, evidence: Option<Evidence> }`, `enum ReviewOutcome { Findings(Vec<Finding>), Unparsed { raw: String } }`, `enum VerifyStatus { Fixed, NotFixed, Disputed }`, `struct VerifyResult { finding: u32, status: VerifyStatus, note: String }`, `enum VerifyOutcome { Results(Vec<VerifyResult>), Unparsed { raw: String } }`, `enum BuilderStatus { Done, Question }`, `enum Decision { Fix, OutOfScope, Advisory }`, `struct Ruling { finding: u32, decision: Decision, note: String }`.
  - In `event.rs`: `enum Actor { Frederic, Lead, Run(RunId), Regie }`; `enum Role { Builder, Reviewer, Verify }`; `enum Purpose { Initial, Fix, OpenPr, CiFix, ConflictFix, Message, Resume, Review, Verify }`; `enum RunOutcome { Finished, Paused, Stopped, LimitHit, Crashed, Interrupted }`; `enum LoopKind { Ci, Conflict, PrFeedback }`; `struct RepoInfo { path: String, github: Option<String> }`; `struct NewEvent { at: Millis, session: Option<SessionId>, task: Option<TaskId>, actor: Actor, kind: EventKind }`; `struct Event { id: EventId, at, session, task, actor, kind }`; and `#[serde(tag = "type")] enum EventKind` with exactly these variants:
    `SessionCreated { name, repos: Vec<RepoInfo> }`, `ProjectFactsSet { repo: String, facts: ProjectFacts }`, `TaskCreated { repo, title, instruction, tickets: Vec<String>, blocked_by: Vec<TaskId>, level: Level, paths: Vec<String>, provider: Option<Provider> }`, `LevelRaised { level: Level }`, `WorktreeCreated { path: String, branch: String, port_base: u16 }`, `SetupFinished { ok: bool, output_tail: String }`, `RunStarted { run: RunId, role: Role, purpose: Purpose, provider: Provider, model: String, effort: Effort, resume_of: Option<String> }`, `RunAttached { run: RunId, native_session: String, pid: Option<u32> }`, `RunFinished { run: RunId, outcome: RunOutcome, final_message: Option<String> }`, `RunUsage { run: RunId, input_tokens: u64, output_tokens: u64, cache_read_tokens: u64, duration_ms: u64 }`, `MessageQueued { text: String }`, `MessageDelivered`, `ReviewReported { run: RunId, round: u32, outcome: ReviewOutcome }`, `FindingsRuled { round: u32, rulings: Vec<Ruling> }`, `VerifyReported { run: RunId, outcome: VerifyOutcome }`, `PrObserved { pr: PrState }`, `PrMissing`, `PrCommentObserved { comment: PrComment }`, `TaskCancelled { close_pr: bool }`, `TaskRetried`, `PrClosedByRegie`, `WorktreeCleaned`, `SessionPaused`, `SessionResumed`, `AllPaused`, `AllResumed`, `UsageReading { provider: Provider, reading: Reading }`, `LimitHit { provider: Provider, resets_at: Millis }`.
    In `usage.rs` (types only; Task 5 adds the functions): `struct Window { used: f64, resets_at: Millis }`, `struct Reading { five_hour: Window, seven_day: Window, read_at: Millis }`.
  - In `command.rs`: `#[serde(tag = "type")] enum Command { CreateSession { name, repos: Vec<String> }, SetProjectFacts { repo, facts }, CreateTask { session, repo, title, instruction, tickets, blocked_by, level, paths, provider }, MessageAgent { task, text, level: Option<Level> }, StopAgent { task }, RetryTask { task }, CancelTask { task, close_pr: bool }, PauseSession { session }, ResumeSession { session }, PauseAll, ResumeAll }` and `enum CommandResult { Session(SessionId), Task(TaskId), Ok }`, `#[derive(thiserror::Error)] enum CommandError` with a message per check in Task 6's `validate`.

- [ ] **Step 1: Write the failing tests**

```rust
#[test] fn event_kind_round_trips_with_type_tag() {
    let k = EventKind::TaskCreated { repo: "/r".into(), title: "t".into(), instruction: "i".into(),
        tickets: vec!["ABC-1".into()], blocked_by: vec![], level: Level::Light, paths: vec![], provider: None };
    let j = serde_json::to_value(&k).unwrap();
    assert_eq!(j["type"], "TaskCreated");
    assert_eq!(serde_json::from_value::<EventKind>(j).unwrap(), k);
}
#[test] fn default_settings_match_spec() {
    let s = Settings::default();
    assert_eq!((s.caps.overall, s.caps.per_session), (4, 2));
    assert_eq!((s.thresholds.five_hour_max, s.thresholds.weekly_floor, s.thresholds.weekly_ahead_max), (0.75, 0.20, 0.10));
    assert_eq!((s.loops.review_rounds, s.loops.ci, s.loops.conflict, s.loops.pr_feedback), (2, 3, 3, 3));
    assert_eq!((s.probe_after_ms, s.output_retention_days, s.port_base, s.port_blocks), (600_000, 14, 41000, 200));
}
#[test] fn levels_map_to_claude_models() {
    let s = Settings::default();
    assert_eq!(s.model_for(Provider::Claude, Level::Light), ModelChoice { model: "claude-sonnet-5-5".into(), effort: Effort::High });
    assert_eq!(s.model_for(Provider::Claude, Level::Standard), ModelChoice { model: "claude-opus-5-5".into(), effort: Effort::Medium });
    assert_eq!(s.model_for(Provider::Claude, Level::Hard), ModelChoice { model: "claude-opus-5-5".into(), effort: Effort::High });
}
#[test] fn ids_display() { assert_eq!(TaskId(12).to_string(), "T12"); assert_eq!(RunId(3).to_string(), "R3"); }
#[test] fn level_order() { assert!(Level::Light < Level::Standard && Level::Standard < Level::Hard); }
```

- [ ] **Step 2: Run them to see them fail**

Run: `cargo test -p regie-core`
Expected: compile errors for the missing types.

- [ ] **Step 3: Implement the types** in the files listed, with `#[ts(export)]` on every type the web page or the API uses (all of the above). Codex's model choice in `model_for` returns `gpt-6.1-sol` with low, medium, high per level (used from milestone 3).

- [ ] **Step 4: Run the tests and export the TypeScript types**

Run: `cargo test -p regie-core && ls web/src/api/gen | head`
Expected: all pass; `.ts` files such as `EventKind.ts` and `TaskId.ts` exist.

- [ ] **Step 5: Commit**

```bash
git add crates/regie-core web/src/api/gen
git commit -m "feat(core): ids, settings, events and commands"
```

---

### Task 3: Parsing agents' final messages

**Files:**
- Modify: `crates/regie-core/src/findings.rs`

**Interfaces:**
- Produces:
  - `pub fn parse_builder_status(final_message: &str) -> BuilderStatus`: the last non-empty line, trimmed, equal to `Status: done` (case-insensitive) is `Done`; anything else, including `Status: question`, is `Question`.
  - `pub fn parse_review(final_message: &str) -> ReviewOutcome`: takes the last fenced block marked ```` ```json ````, expects `{"findings":[{"title","kind","blocking","evidence":{"type","detail","file","line"}}]}`; numbers findings from 1 in order; a finding marked blocking whose evidence is missing, or has an empty `detail`, or is of type `criterion` without `file` and `line`, becomes non-blocking. No block or bad JSON: `Unparsed { raw }`.
  - `pub fn parse_verify(final_message: &str) -> VerifyOutcome`: last ```` ```json ```` block `{"results":[{"finding":1,"status":"fixed|not_fixed|disputed","note":""}]}`; otherwise `Unparsed`.
  - `pub fn auto_rule(findings: &[Finding]) -> Vec<Ruling>`: the rule used while there's no lead: blocking → `Fix`, others → `Advisory`.

- [ ] **Step 1: Write the failing tests**

```rust
#[test] fn done_status() { assert_eq!(parse_builder_status("Did X.\n\nStatus: done\n"), BuilderStatus::Done); }
#[test] fn status_line_must_be_last() {
    assert_eq!(parse_builder_status("Status: done\nThen one more thing?"), BuilderStatus::Question);
    assert_eq!(parse_builder_status("No status line at all"), BuilderStatus::Question);
    assert_eq!(parse_builder_status("Status: question"), BuilderStatus::Question);
}
#[test] fn review_findings_parsed_and_numbered() { /* two findings in a json block; assert ids 1 and 2, kinds, blocking kept for the one with command evidence */ }
#[test] fn blocking_without_evidence_is_advisory() { /* blocking:true, no evidence → blocking == false */ }
#[test] fn criterion_needs_file_and_line() { /* evidence type criterion without line → blocking == false */ }
#[test] fn review_without_json_is_unparsed() {
    assert!(matches!(parse_review("Looks fine to me."), ReviewOutcome::Unparsed { .. }));
    assert!(matches!(parse_review("```json\n{not json\n```"), ReviewOutcome::Unparsed { .. }));
}
#[test] fn empty_findings_is_a_clean_review() { assert_eq!(parse_review("```json\n{\"findings\":[]}\n```"), ReviewOutcome::Findings(vec![])); }
#[test] fn last_json_block_wins() { /* an example block in prose, then the real one; the real one is used */ }
#[test] fn verify_results_parsed() { /* fixed / not_fixed / disputed map to VerifyStatus */ }
#[test] fn auto_rule_fixes_blocking_only() { /* one blocking, one advisory → [Fix, Advisory] */ }
```

- [ ] **Step 2: Run them to see them fail**

Run: `cargo test -p regie-core findings::`
Expected: FAIL (functions not defined).

- [ ] **Step 3: Implement the four functions.** Find fenced blocks by scanning lines for a line that starts with ```` ```json ```` and the next line that is ```` ``` ````; deserialize with serde into private raw structs, then map.

- [ ] **Step 4: Run the tests**

Run: `cargo test -p regie-core findings::`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add crates/regie-core/src/findings.rs
git commit -m "feat(core): parse builder status, review findings and verify results"
```

---

### Task 4: State built from events, and the task flow

**Files:**
- Create: `crates/regie-core/src/state.rs`, `crates/regie-core/src/flow.rs`

**Interfaces:**
- Consumes: Task 2 types, Task 3 parsers.
- Produces:
  - `struct State { sessions: BTreeMap<SessionId, Session>, tasks: BTreeMap<TaskId, Task>, runs: BTreeMap<RunId, Run>, project_facts: BTreeMap<String, ProjectFacts>, paused_all: bool, readings: BTreeMap<Provider, Reading>, blocked_until: BTreeMap<Provider, Millis> }` with `fn apply(&mut self, e: &Event)`, `fn replay(events: &[Event]) -> State`, `fn next_session_id(&self) -> SessionId`, `fn next_task_id(&self) -> TaskId`, `fn next_run_id(&self) -> RunId` (max + 1, starting at 1), `fn active_run(&self, task: TaskId) -> Option<&Run>` (started and not finished).
  - `struct Session { id, name, repos: Vec<RepoInfo>, paused: bool }`.
  - `struct Task { id, session, repo, title, instruction, tickets, blocked_by, level, paths, created_at: Millis, worktree: Option<Worktree>, setup_ok: Option<bool>, setup_output: String, builder_native_session: Option<String>, last_builder_status: Option<BuilderStatus>, pending_messages: Vec<String>, reviews: Vec<ReviewRecord>, verify: Option<VerifyOutcome>, open_pr_requested: bool, pr: Option<PrState>, pr_missing: bool, comments: Vec<PrComment>, loops: LoopCounts, fixed_shas: BTreeMap<LoopKind, String>, cancelled: Option<bool /* close_pr */>, pr_closed_by_regie: bool, cleaned: bool, last_run_outcome: Option<RunOutcome> }` where `Worktree { path, branch, port_base }`, `ReviewRecord { round: u32, outcome: ReviewOutcome, rulings: Option<Vec<Ruling>>, fix_done: bool }`, `LoopCounts { ci: u32, conflict: u32, pr_feedback: u32 }`.
  - `struct Run { id, task: TaskId, role, purpose, provider, model, effort, native_session: Option<String>, pid: Option<u32>, started_at: Millis, outcome: Option<RunOutcome>, final_message: Option<String> }`.
  - `enum HumanReason { BuilderQuestion, SetupFailed, ReviewUnparsed, VerifyFailed, CapReached(LoopKind), ChangesRequested, PrNotFound, PrClosed, FixNotPushed(LoopKind), RunCrashed }`.
  - `enum PromptInput { Initial, Fix { findings: Vec<Finding> }, OpenPr, CiFix { failed: Vec<String> }, ConflictFix, Message { text: String }, Resume { note: String }, Review { round: u32 }, Verify { findings: Vec<Finding> } }`.
  - `enum NextStep { Nothing, StartRun { role: Role, purpose: Purpose, input: PromptInput }, AwaitRuling { round: u32 }, AwaitHuman(HumanReason), ClosePr, Cleanup }`.
  - `pub fn next_step(task: &Task, state: &State, settings: &Settings) -> NextStep`.

`apply` rules worth pinning (the rest is direct field updates):
- `RunFinished` of a builder run with `Finished` outcome sets `last_builder_status` from `parse_builder_status`; when that run's purpose was `Fix`, it marks the latest review's `fix_done`; when it was `OpenPr`, `open_pr_requested` stays true.
- `MessageQueued` pushes to `pending_messages`; `RunStarted` with purpose `Message`, or `MessageDelivered` (a message sent into a running builder), pops the first one.
- `RunStarted` with purpose `CiFix`, `ConflictFix`, or `Message` while the PR has changes requested increments the matching loop count and records the PR's current `head_sha` in `fixed_shas`.
- `TaskRetried` clears `last_run_outcome`, `setup_ok == Some(false)` (back to `None`) and `pr_missing`.

`next_step` order (the first rule that matches wins):
1. Cancelled: if the PR is open, `close_pr` was asked and not yet done → `ClosePr`; else if not cleaned → `Cleanup`; else `Nothing`.
2. PR merged: not cleaned → `Cleanup`; else `Nothing`.
3. A run is active → `Nothing`.
4. `setup_ok == Some(false)` → `AwaitHuman(SetupFailed)`.
5. Last run outcome `Crashed` → `AwaitHuman(RunCrashed)`. Outcome `Interrupted`, `Paused` or `LimitHit` → `StartRun` of the same role and purpose `Resume` with note "You were interrupted. Continue where you left off; check the state of the worktree first." (the scheduler holds it while paused or blocked).
6. A pending message → `StartRun { Builder, Message, Message { text } }`.
7. `last_builder_status == Some(Question)` → `AwaitHuman(BuilderQuestion)`.
8. No builder run yet → `StartRun { Builder, Initial, Initial }`.
9. Review phase, until review is closed:
   - no review yet, or the latest has `fix_done` and its round < `review_rounds` → `StartRun { Reviewer, Review, Review { round: n + 1 } }`;
   - latest `Unparsed` → `AwaitHuman(ReviewUnparsed)`;
   - latest has blocking findings and no rulings → `AwaitRuling { round }`;
   - latest ruled with at least one `Fix` and not `fix_done` → `StartRun { Builder, Fix, Fix { findings to fix } }`;
   - latest is round `review_rounds`, `fix_done`, and no verify yet → `StartRun { Verify, Verify, Verify { findings fixed in that round } }`;
   - verify `Unparsed` or any result not `Fixed` → `AwaitHuman(VerifyFailed)`.
   Review is closed when the latest review has no `Fix` rulings (clean, or all advisory or out of scope), or when the verify pass has every result `Fixed`.
10. Review closed and not `open_pr_requested` → `StartRun { Builder, OpenPr, OpenPr }`.
11. `pr_missing` → `AwaitHuman(PrNotFound)`. PR closed without a merge (and the task not cancelled) → `AwaitHuman(PrClosed)`.
12. PR open:
    - conflicting: if `fixed_shas[Conflict] == head_sha` → `AwaitHuman(FixNotPushed(Conflict))`; else if `loops.conflict < 3` → `StartRun { Builder, ConflictFix, ConflictFix }`; else `AwaitHuman(CapReached(Conflict))`.
    - checks `Failure { failed }`: the same three-way rule with `Ci` and `CiFix { failed }`.
    - `review_decision == ChangesRequested` → `AwaitHuman(ChangesRequested)`.
13. Otherwise `Nothing`.

- [ ] **Step 1: Write the failing tests** (table-driven, building a `Task` with a small builder helper `TaskB` in the test module)

```rust
#[test] fn new_task_starts_builder() { assert_eq!(step(TaskB::new()), start(Role::Builder, Purpose::Initial)); }
#[test] fn active_run_waits() { /* any active run → Nothing */ }
#[test] fn builder_question_needs_human() { /* last_builder_status Question → AwaitHuman(BuilderQuestion) */ }
#[test] fn message_resumes_builder_even_after_question() { /* pending message wins over Question */ }
#[test] fn done_builder_goes_to_review_round_1() { /* → StartRun Reviewer, Review { round: 1 } */ }
#[test] fn clean_review_opens_pr() { /* review round 1 Findings([]) → StartRun Builder OpenPr */ }
#[test] fn blocking_findings_await_ruling() { /* → AwaitRuling { round: 1 } */ }
#[test] fn ruled_fix_goes_to_builder_then_round_2() { /* rulings with Fix, !fix_done → Fix; fix_done → Review { round: 2 } */ }
#[test] fn round_2_fix_goes_to_verify() { /* round 2 ruled Fix, fix_done → StartRun Verify with the round-2 findings */ }
#[test] fn verify_all_fixed_closes_review() { /* → OpenPr */ }
#[test] fn verify_not_fixed_needs_human() { /* → AwaitHuman(VerifyFailed) */ }
#[test] fn unparsed_review_needs_human() { /* → AwaitHuman(ReviewUnparsed) */ }
#[test] fn ci_failure_fixes_then_caps() { /* failure, ci 0 → CiFix; ci 3 → CapReached(Ci) */ }
#[test] fn ci_failure_same_sha_after_fix_needs_human() { /* fixed_shas[Ci] == head_sha → FixNotPushed(Ci) */ }
#[test] fn conflict_fix() { /* conflicting → ConflictFix */ }
#[test] fn changes_requested_needs_human() { }
#[test] fn pr_green_waits() { /* checks Success, Clean → Nothing */ }
#[test] fn pr_closed_outside_regie_needs_human() { /* lifecycle Closed, not cancelled → AwaitHuman(PrClosed) */ }
#[test] fn merged_cleans_up_once() { /* merged, !cleaned → Cleanup; cleaned → Nothing */ }
#[test] fn cancel_closes_pr_then_cleans_up() { }
#[test] fn setup_failure_needs_human() { }
#[test] fn crash_needs_human_interrupt_resumes() { }
#[test] fn replay_equals_incremental_apply() { /* apply a scripted list of events one by one vs State::replay → equal */ }
#[test] fn next_ids_start_at_one() { }
```

- [ ] **Step 2: Run them to see them fail**

Run: `cargo test -p regie-core state:: flow::`
Expected: FAIL.

- [ ] **Step 3: Implement `State::apply`, `State::replay`, the id helpers, and `next_step`** following the rules above. `State` derives `PartialEq` for the replay test.

- [ ] **Step 4: Run the tests**

Run: `cargo test -p regie-core`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add crates/regie-core/src/state.rs crates/regie-core/src/flow.rs crates/regie-core/src/lib.rs
git commit -m "feat(core): state from events and the task flow"
```

---

### Task 5: Thresholds, scheduling and ports

**Files:**
- Modify: `crates/regie-core/src/usage.rs` (functions on Task 2's `Window` and `Reading`)
- Create: `crates/regie-core/src/scheduler.rs`, `crates/regie-core/src/ports.rs`

**Interfaces:**
- Produces:
  - `enum WindowKind { FiveHour, SevenDay }`, `enum Openness { Open, Threshold { window: WindowKind, used: f64, ahead: Option<f64> }, Blocked { until: Millis } }`.
  - `pub fn week_elapsed(resets_at: Millis, now: Millis) -> f64` = `1 - (resets_at - now) / 7 days`, clamped to 0..=1.
  - `pub fn openness(reading: Option<&Reading>, blocked_until: Option<Millis>, now: Millis, t: &Thresholds) -> Openness`: blocked until a future time → `Blocked`; no reading → `Open`; five-hour `used >= five_hour_max` → `Threshold(FiveHour)`; seven-day `used > weekly_floor` and `used - week_elapsed > weekly_ahead_max` → `Threshold(SevenDay, ahead)`; else `Open`.
  - `pub fn needs_probe(reading: Option<&Reading>, now: Millis, s: &Settings) -> bool` (no reading, or `now - read_at > probe_after_ms`).
  - `enum QueuedReason { Slot, Quota, Dependency, Paused }` (in `scheduler.rs`).
  - `struct Candidate { task: TaskId, session: SessionId, provider: Provider, started_before: bool, created_at: Millis, deps_merged: bool }`, `struct Capacity { running_total: u32, running_by_session: BTreeMap<SessionId, u32>, openness: BTreeMap<Provider, Openness>, paused_sessions: BTreeSet<SessionId>, paused_all: bool }`.
  - `pub fn pick(cands: &[Candidate], cap: &Capacity, s: &Settings) -> (Vec<TaskId>, BTreeMap<TaskId, QueuedReason>)`: order `started_before` first, then `created_at`; for each: paused → `Paused`; deps not merged → `Dependency`; provider not `Open` → `Quota`; session at `per_session` → `Slot`; total at `overall` → `Slot`; else start it and count it.
  - `pub fn allocate_port_base(in_use: &BTreeSet<u16>, s: &Settings) -> Option<u16>`: lowest `port_base + 10 * k`, `k < port_blocks`, not in use.

- [ ] **Step 1: Write the failing tests**

```rust
const DAY: i64 = 86_400_000;
#[test] fn todays_reading_is_open() { // 76% used, 71.5% of the week gone → 4.5 points ahead
    let now = 0; let r = reading(0.30, now + 5 * 3_600_000, 0.76, now + (0.285 * 7.0 * DAY as f64) as i64);
    assert_eq!(openness(Some(&r), None, now, &Thresholds::default()), Openness::Open);
}
#[test] fn eleven_points_ahead_is_threshold() { /* used 0.82, elapsed 0.71 → Threshold(SevenDay) */ }
#[test] fn low_weekly_use_ignores_pace() { /* used 0.15, elapsed 0.01 → Open */ }
#[test] fn five_hour_at_75_is_threshold() { /* 0.75 → Threshold(FiveHour); 0.749 → Open */ }
#[test] fn blocked_until_reset() { /* future → Blocked; past → Open */ }
#[test] fn probe_when_stale() { /* read 11 minutes ago → true; 9 → false; None → true */ }
#[test] fn started_work_goes_first() { }
#[test] fn caps_hold_with_slot_reason() { /* 4 running → Slot; session with 2 running → Slot */ }
#[test] fn quota_dependency_paused_reasons() { }
#[test] fn ports_lowest_free_block() { /* in use {41000, 41010} → 41020; all 200 used → None */ }
```

- [ ] **Step 2: Run them to see them fail**

Run: `cargo test -p regie-core usage:: scheduler:: ports::`
Expected: FAIL.

- [ ] **Step 3: Implement the functions.**

- [ ] **Step 4: Run the tests**

Run: `cargo test -p regie-core`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add crates/regie-core/src
git commit -m "feat(core): thresholds, scheduling and port blocks"
```

---

### Task 6: Lanes, command checks and board views

**Files:**
- Create: `crates/regie-core/src/lane.rs`, `crates/regie-core/src/views.rs`
- Modify: `crates/regie-core/src/command.rs` (add `validate`)

**Interfaces:**
- Consumes: `State`, `Task`, `next_step`, `NextStep`, `HumanReason` (Task 4); `QueuedReason`, `Openness`, `openness` (Task 5).
- Produces:
  - `enum Lane { Queued, Working, InReview, Ci, ReadyToMerge, Merged, Cancelled }` (that is also the board's column order).
  - `enum Flag { NeedsMe(HumanReason), Paused, Waiting(QueuedReason) }`.
  - `pub fn lane(task: &Task, active_role: Option<Role>, next: &NextStep, session_paused: bool, waiting: Option<QueuedReason>) -> (Lane, Vec<Flag>)`.
  - `pub fn validate(cmd: &Command, state: &State) -> Result<(), CommandError>`.
  - `struct LiveView { active: BTreeMap<TaskId, (RunId, Role)>, last_output: BTreeMap<TaskId, String>, waiting: BTreeMap<TaskId, QueuedReason> }`.
  - `struct TaskCard { id, title, lane, flags, level, model: Option<String>, tickets, pr: Option<(u64, String)>, last_output: Option<String> }`, `struct BoardView { session: Session, cards: Vec<TaskCard>, running: u32, usage: Vec<UsageRow> }`, `struct UsageRow { provider, reading: Option<Reading>, openness: Openness }`, `struct TaskDetail { card: TaskCard, instruction, worktree: Option<Worktree>, runs: Vec<Run>, timeline: Vec<Event>, reviews: Vec<ReviewRecord> }`.
  - `pub fn board(state: &State, live: &LiveView, session: SessionId, settings: &Settings, now: Millis) -> Option<BoardView>`, `pub fn task_detail(state: &State, live: &LiveView, events: &[Event], task: TaskId, settings: &Settings, now: Millis) -> Option<TaskDetail>`.

`lane` rules (in order):
1. PR merged → `Merged`. Cancelled → `Cancelled`.
2. `active_role` Builder → `Working`; Reviewer or Verify → `InReview`.
3. No builder run ever started (no worktree) → `Queued`, with `Waiting(reason)` if given.
4. `next` is `StartRun` Builder → `Working`; `StartRun` Reviewer or Verify, or `AwaitRuling` → `InReview`. Add `Waiting(reason)` if given.
5. `AwaitHuman(r)`: `ReviewUnparsed`, `VerifyFailed`, `ChangesRequested` → `InReview`; every other reason → `Working`. Add `NeedsMe(r)`.
6. PR open: checks `Success`, not conflicting, merge state `Clean` → `ReadyToMerge`; merge state `Blocked` with checks `Success` → `InReview` (waiting for a required GitHub approval); otherwise → `Ci`.
7. Otherwise `Queued`.
Add `Paused` to the flags whenever the session is paused.

`validate` checks: `CreateTask` — the session exists; the repo is one of the session's repos and has a GitHub remote; project facts exist for the repo ("no project facts for <repo>; set them with `regie project set`"); every `blocked_by` task exists in the same session. `MessageAgent` — the task exists, isn't merged or cancelled, and `level`, if given, is higher than the task's. `StopAgent`, `RetryTask`, `CancelTask` — the task exists. `PauseSession`, `ResumeSession` — the session exists. `CreateSession` — at least one repo.

- [ ] **Step 1: Write the failing tests**

```rust
#[test] fn lane_precedence_table() {
    // one row per rule above: (task setup, active role, next step, paused, waiting) → expected (lane, flags)
}
#[test] fn needs_me_flag_on_await_human() { }
#[test] fn ready_to_merge_needs_clean_merge_state() { /* Success + Blocked → InReview; Success + Clean → ReadyToMerge */ }
#[test] fn paused_flag_added() { }
#[test] fn create_task_requires_project_facts() { }
#[test] fn create_task_rejects_repo_without_github() { }
#[test] fn blocked_by_must_exist_in_session() { }
#[test] fn level_can_only_go_up() { }
#[test] fn board_orders_cards_needs_me_first_then_oldest() { }
```

- [ ] **Step 2: Run them to see them fail**

Run: `cargo test -p regie-core lane:: views:: command::`
Expected: FAIL.

- [ ] **Step 3: Implement `lane`, `validate`, `board`, `task_detail`.** `board` computes each card's lane from `next_step` and the `LiveView`, sorts cards with `NeedsMe` first, then by `created_at`.

- [ ] **Step 4: Run the tests**

Run: `cargo test -p regie-core`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add crates/regie-core/src
git commit -m "feat(core): lanes, command checks and board views"
```

---

### Task 7: The event store

**Files:**
- Create: `crates/regie/src/store.rs`, `crates/regie/src/paths.rs`

**Interfaces:**
- Produces:
  - `struct DataDir { root: PathBuf }` with `fn default() -> Self` (`~/.regie`), `fn db(&self)`, `fn server_json(&self)`, `fn runs(&self)`, `fn run_file(&self, run: RunId)`, `fn worktrees(&self)`, and `fn ensure(&self) -> anyhow::Result<()>`.
  - `struct Store` with `fn open(path: &Path) -> anyhow::Result<Store>` (creates the schema, WAL mode), `fn append(&mut self, e: NewEvent) -> anyhow::Result<Event>`, `fn load_all(&self) -> anyhow::Result<Vec<Event>>`, `fn load_after(&self, after: EventId) -> anyhow::Result<Vec<Event>>`.
  - Schema: `CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER NOT NULL, session INTEGER, task INTEGER, actor TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL)`, where `actor` and `payload` are JSON and `kind` is the variant name.
- Decision for this milestone: the tables "kept from the log" (design section 3) live in memory as `regie_core::State`, rebuilt by replay at start. SQLite projection tables are added when a query needs them (the log page, milestone 4).

- [ ] **Step 1: Write the failing tests**

```rust
#[test] fn append_assigns_increasing_ids() { }
#[test] fn reopen_and_replay_gives_same_state() { /* append a session, facts and a task; reopen the file; State::replay(load_all) == the incremental state */ }
#[test] fn load_after_returns_only_newer() { }
```

- [ ] **Step 2: Run them to see them fail**

Run: `cargo test -p regie store::`
Expected: FAIL.

- [ ] **Step 3: Implement `DataDir` and `Store`** with rusqlite (`bundled`). The engine owns the only `Store`, so no connection pool.

- [ ] **Step 4: Run the tests**

Run: `cargo test -p regie store::`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add crates/regie/src/store.rs crates/regie/src/paths.rs
git commit -m "feat: SQLite event store"
```

---

### Task 8: Worktrees, setup and cleanup

**Files:**
- Create: `crates/regie/src/git/{mod.rs, worktree.rs, locks.rs}`, `crates/regie/tests/support/repo.rs` (a `TempRepo` helper shared with later tests)

**Interfaces:**
- Produces:
  - `pub fn slug(title: &str) -> String` (lowercase ASCII, other characters to `-`, runs collapsed, trimmed, at most 40 characters), `pub fn branch_name(task: TaskId, title: &str) -> String` → `regie/t<id>-<slug>`.
  - `pub fn task_env(task: TaskId, port_base: u16) -> Vec<(String, String)>` → `REGIE_TASK=T<id>`, `REGIE_PORT_BASE=<n>`, `COMPOSE_PROJECT_NAME=regie-t<id>`.
  - `pub async fn default_branch(repo: &Path) -> anyhow::Result<String>`: `git symbolic-ref --short refs/remotes/origin/HEAD` (strip `origin/`), falling back to the `HEAD branch:` line of `git remote show origin`.
  - `pub async fn github_remote(repo: &Path) -> Option<String>`: `owner/name` parsed from `git remote get-url origin` when the host is `github.com` (SSH or HTTPS forms).
  - `struct RepoLocks` (a map from repo path to `tokio::sync::Mutex<()>`) with `async fn lock(&self, repo: &Path) -> OwnedMutexGuard<()>`.
  - `pub async fn create_worktree(locks: &RepoLocks, repo: &Path, root: &Path, task: TaskId, title: &str, copy_files: &[String]) -> anyhow::Result<Worktree>`: under the repo's lock: `git fetch origin`, `git worktree add -b <branch> <root>/t<id>-<slug> origin/<default>`; then copy each listed file that exists in the repo to the same relative path in the worktree.
  - `pub async fn run_setup(path: &Path, cmd: &str, env: &[(String, String)]) -> (bool, String)`: `sh -c <cmd>` in the worktree with the env; returns success and the last 4,000 characters of stdout and stderr together.
  - `pub async fn remove_worktree(locks: &RepoLocks, repo: &Path, wt: &Worktree) -> anyhow::Result<()>`: `git worktree remove --force <path>` then `git branch -D <branch>`; never touches the remote.

- [ ] **Step 1: Write the failing tests** (`TempRepo::new()` makes a bare `origin` with one commit on `main`, a clone with `origin/HEAD` set, and an untracked `.env` in the clone)

```rust
#[test] fn slug_and_branch() { assert_eq!(branch_name(TaskId(7), "Add CSV export: v2!"), "regie/t7-add-csv-export-v2"); }
#[test] fn env_for_task() { /* REGIE_PORT_BASE=41020, COMPOSE_PROJECT_NAME=regie-t7, REGIE_TASK=T7 */ }
#[tokio::test] async fn creates_worktree_from_fresh_origin() { /* push a new commit to origin from elsewhere; worktree HEAD equals that commit; branch name; .env copied */ }
#[tokio::test] async fn default_branch_without_origin_head() { /* delete refs/remotes/origin/HEAD; still "main" */ }
#[tokio::test] async fn setup_gets_env_and_tail() { /* cmd `echo $REGIE_PORT_BASE; exit 3` → (false, "41020\n") */ }
#[tokio::test] async fn remove_cleans_worktree_and_branch() { /* dir gone; `git branch --list` has no regie branch; origin untouched */ }
#[tokio::test] async fn parallel_worktrees_same_repo() { /* create two worktrees concurrently with tokio::join! → both succeed */ }
#[tokio::test] async fn github_remote_parsing() { /* git@github.com:o/n.git and https://github.com/o/n → "o/n"; gitlab → None */ }
```

- [ ] **Step 2: Run them to see them fail**

Run: `cargo test -p regie git::`
Expected: FAIL.

- [ ] **Step 3: Implement** with `tokio::process::Command` calling `git`.

- [ ] **Step 4: Run the tests**

Run: `cargo test -p regie git::`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add crates/regie/src/git crates/regie/tests/support
git commit -m "feat: task worktrees, setup and cleanup"
```

---

### Task 9: GitHub through `gh`

**Files:**
- Create: `crates/regie/src/github.rs`, `crates/regie/tests/fixtures/gh/{pr_list_pending.json, pr_list_failed.json, pr_list_clean.json, pr_list_conflict.json, pr_list_merged.json, pr_view_activity.json}`

**Interfaces:**
- Consumes: core `PrState`, `PrComment`, `Checks`, `MergeState`, `ReviewDecision`.
- Produces:
  - `#[async_trait] trait GitHub: Send + Sync { async fn pr_for_branch(&self, repo: &Path, branch: &str) -> anyhow::Result<Option<PrState>>; async fn pr_activity(&self, repo: &Path, number: u64) -> anyhow::Result<Vec<PrComment>>; async fn close_pr(&self, repo: &Path, number: u64) -> anyhow::Result<()>; }`
  - `struct GhCli` implementing it with `gh pr list --head <branch> --state all --limit 1 --json number,url,state,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup,headRefOid`, `gh pr view <n> --json comments,reviews`, `gh pr close <n>`, each run in the repo folder.
  - Pure parsers: `pub fn parse_pr_list(json: &str) -> anyhow::Result<Option<PrState>>`, `pub fn parse_activity(json: &str) -> anyhow::Result<Vec<PrComment>>`, `pub fn aggregate_checks(rollup: &[serde_json::Value]) -> Checks`.
  - `aggregate_checks`: empty → `None`; any check-run `conclusion` in FAILURE, TIMED_OUT, CANCELLED, ACTION_REQUIRED, or any status-context `state` in FAILURE, ERROR → `Failure { failed: names }`; any check-run not COMPLETED, or status-context PENDING → `Pending`; else `Success`. `mergeable == "CONFLICTING"` → `conflicting`. Reviews with state CHANGES_REQUESTED → `changes_requested: true`.
  - `pub struct FakeGitHub` (in `crates/regie/tests/support/github.rs`): a `Mutex<BTreeMap<String /*branch*/, PrState>>` plus comments, with `set(branch, PrState)`, `merge(branch)`, and recorded `close_pr` calls.

- [ ] **Step 1: Capture fixtures.** Write the JSON fixtures by hand in the shape `gh` 2.101 returns (one object per array element with the fields listed); include one `CheckRun` and one `StatusContext` in the failed fixture.

- [ ] **Step 2: Write the failing tests**

```rust
#[test] fn pending_checks() { assert_eq!(parse_pr_list(PENDING).unwrap().unwrap().checks, Checks::Pending); }
#[test] fn failed_checks_named() { /* Failure { failed: ["build", "ci/lint"] } */ }
#[test] fn clean_pr() { /* Success, MergeState::Clean, !conflicting */ }
#[test] fn conflict_pr() { /* conflicting == true, MergeState::Dirty */ }
#[test] fn merged_pr() { /* PrLifecycle::Merged */ }
#[test] fn no_pr_is_none() { assert_eq!(parse_pr_list("[]").unwrap(), None); }
#[test] fn activity_comments_and_change_requests() { }
```

- [ ] **Step 3: Run them to see them fail**

Run: `cargo test -p regie github::`
Expected: FAIL.

- [ ] **Step 4: Implement the parsers and `GhCli`, and the fake.**

- [ ] **Step 5: Run the tests**

Run: `cargo test -p regie github::`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add crates/regie/src/github.rs crates/regie/tests
git commit -m "feat: PR and CI state through gh"
```

---

### Task 10: Claude agents: stream parsing, processes and prompts

**Files:**
- Create: `crates/regie/src/agents/{mod.rs, stream.rs, claude.rs, prompts.rs}`, `prompts/{builder_initial.md, builder_fix.md, builder_open_pr.md, builder_ci_fix.md, builder_conflict_fix.md, builder_resume.md, reviewer.md, verify.md}`, `crates/regie/tests/fixtures/claude/{basic.jsonl, error_no_result.jsonl, limit_rejected.jsonl}`, `crates/regie/tests/live_claude.rs`, `crates/regie/tests/support/claude.rs` (the fake)

**Interfaces:**
- Produces:
  - In `agents/mod.rs`: `struct RunSpec { run: RunId, role: Role, model: String, effort: Effort, cwd: PathBuf, prompt: String, resume: Option<String>, env: Vec<(String, String)> }`; `enum AgentEvent { Attached { native_session: String, pid: Option<u32> }, Output(String), Reading(Reading), Usage { input_tokens: u64, output_tokens: u64, cache_read_tokens: u64, duration_ms: u64 }, Finished { final_message: Option<String>, outcome: RunOutcome, limit_resets_at: Option<Millis> } }`; `enum AgentControl { Message(String), Interrupt, Stop }`; `struct RunHandle { events: mpsc::Receiver<AgentEvent>, control: mpsc::Sender<AgentControl> }`; `#[async_trait] trait AgentRunner: Send + Sync { async fn start(&self, spec: RunSpec) -> anyhow::Result<RunHandle>; }`.
  - In `stream.rs`: `pub fn parse_line(line: &str, now: Millis) -> Vec<StreamItem>` where `enum StreamItem { Init { session_id: String }, Text(String), ToolUse { name: String, summary: String }, Reading(Reading), Rejected { resets_at: Millis }, Result { text: Option<String>, is_error: bool, usage: (u64, u64, u64), duration_ms: u64 } }`. A `rate_limit_event` maps `rate_limit_info.unifiedWindows.five_hour` and `seven_day` (`utilization`, `resetsAt` in seconds → millis); `status == "rejected"` also yields `Rejected`.
  - In `claude.rs`: `struct ClaudeCli { bin: PathBuf }` implementing `AgentRunner`. Arguments: `-p --input-format stream-json --output-format stream-json --verbose --permission-mode auto --model <m> --effort <e>`, plus `--resume <id>` or `--session-id <new uuid>`. The prompt is written to stdin as `{"type":"user","message":{"role":"user","content":"<prompt>"}}` followed by a newline. On a `Result` item, stdin is closed so the process exits. `Message(text)` writes another user line; `Interrupt` writes `{"type":"control_request","request_id":"<uuid>","request":{"subtype":"interrupt"}}`; `Stop` sends SIGTERM, then SIGKILL after 5 seconds. Every stdout line is appended raw to the run file. `Finished.outcome`: `Rejected` seen → `LimitHit`; a `Result` → `Finished`; exit without a `Result` → `Crashed`; after `Interrupt` or `Stop` → `Paused` or `Stopped` as requested.
  - `pub async fn probe(bin: &Path) -> anyhow::Result<Reading>`: `claude -p ok --model claude-haiku-5-5 --effort low --output-format stream-json --verbose --strict-mcp-config --tools "" --max-turns 1`, returning the first `Reading`.
  - In `prompts.rs`: `enum Template { BuilderInitial, BuilderFix, BuilderOpenPr, BuilderCiFix, BuilderConflictFix, BuilderResume, Reviewer, Verify }`, `struct PromptVars(BTreeMap<&'static str, String>)`, `pub fn render(t: Template, vars: &PromptVars) -> anyhow::Result<String>` (replaces `{{name}}`; errors if a placeholder is left), `pub fn prompt_for(task: &Task, facts: &ProjectFacts, input: &PromptInput, model: &ModelChoice, port_base: u16) -> anyhow::Result<String>` (`PromptInput::Message` returns the text as is).
  - Fake (`tests/support/claude.rs`): `FakeClaude` implementing `AgentRunner`, scripted with a closure `Fn(&RunSpec) -> FakeOutcome { final_message: String, actions: Vec<FakeAction> }`, where actions are `WriteFile(path, content)`, `Commit(message)`, `Reading(Reading)`, `Crash`, `HangUntilControl`; records every `RunSpec` and control message it receives.
- Prompt content the tests pin:
  - `builder_initial.md` contains `{{instruction}}`, `{{tickets}}`, `{{checks}}`, `{{definition_of_done}}`, `{{port_base}}`, the lines "Run the project's checks before you finish.", "Commit your work on this branch with this trailer: `Regie-Agent: builder, claude, {{model}}, {{level}}`", "Do not push and do not open a PR yet: review comes first.", "Never merge.", and the contract "End your final message with one line: `Status: done` when the work is finished and the checks pass, or `Status: question` followed by nothing else if you need an answer; put the question just above it."
  - `builder_open_pr.md`: "Review is closed. Push this branch and open a pull request with `gh pr create`", the title `{{title}}`, a body listing `{{tickets}}`, and the same `Status:` contract.
  - `builder_fix.md`, `builder_ci_fix.md`, `builder_conflict_fix.md`: what to fix (`{{findings}}`, `{{failed_checks}}`, or "your branch conflicts with the repo's default branch: fetch, then merge or rebase it"), "run the checks", "commit"; the CI and conflict prompts add "push".
  - `reviewer.md`: "Do not edit files or commit.", the instruction and acceptance, and the exact JSON format from Task 3 with one example.
  - `verify.md`: the findings to check, and the results JSON format from Task 3.

- [ ] **Step 1: Record a real stream** (costs one tiny Haiku call)

Run: `cd $(mktemp -d) && claude -p "Reply with the word ok." --model claude-haiku-5-5 --effort low --output-format stream-json --verbose --max-turns 1 > /Users/frederic/Code/regie/crates/regie/tests/fixtures/claude/basic.jsonl`
Expected: a file with `system`, `rate_limit_event`, `assistant` and `result` lines. Then hand-write `error_no_result.jsonl` (init and one assistant line, no result) and `limit_rejected.jsonl` (a `rate_limit_event` with `"status": "rejected"`).

- [ ] **Step 2: Write the failing tests**

```rust
#[test] fn basic_stream_items() { /* Init, at least one Reading with five_hour and seven_day, a Result with text "ok" (case-insensitive) */ }
#[test] fn rejected_status_is_limit() { /* Rejected { resets_at } in millis */ }
#[test] fn tool_use_summarised() { /* ToolUse { name: "Bash", summary starts with the command } */ }
#[tokio::test] async fn exit_without_result_is_crash() { /* ClaudeCli with bin = a shell script that cats error_no_result.jsonl and exits 1 → Finished { outcome: Crashed } */ }
#[tokio::test] async fn result_closes_stdin_and_finishes() { /* script that cats basic.jsonl then waits on stdin → Finished { outcome: Finished } */ }
#[test] fn templates_render_without_leftovers() { /* every Template renders with full vars; no "{{" in output */ }
#[test] fn builder_prompt_has_contract_lines() { /* contains "Status: done", "Do not push", "Never merge", "Regie-Agent: builder, claude, claude-opus-5-5, standard" */ }
#[test] fn reviewer_prompt_forbids_edits() { }
```

- [ ] **Step 3: Run them to see them fail**

Run: `cargo test -p regie agents::`
Expected: FAIL.

- [ ] **Step 4: Implement `stream.rs`, `claude.rs`, `prompts.rs` and the prompt files, and the fake.** Tests for `ClaudeCli` use small shell scripts in a temp folder as `bin`.

- [ ] **Step 5: Run the tests**

Run: `cargo test -p regie agents::`
Expected: all pass.

- [ ] **Step 6: Write the live checks** in `tests/live_claude.rs`, each `#[ignore]`, on `claude-haiku-5-5` in a temp git repo:
  - `live_final_message_and_reading`: a run ends `Finished` with a final message and at least one `Reading`.
  - `live_resume_after_kill`: start a run told to remember a code word then run `sleep 30`; `Stop` it after the first tool use; resume with `resume: Some(native_session)` and ask for the word; the answer contains it.
  - `live_interrupt`: `Interrupt` during a `sleep 30` ends the run within 15 seconds.
  - `live_message_mid_turn`: send `Message("Also reply with the word KIWI.")` during a tool call; the final message contains KIWI. Print whether it arrived mid-turn or as a follow-up turn. (Design: "Checks still to run".)

Run: `cargo test -p regie --test live_claude -- --ignored --nocapture`
Expected: all pass; note the mid-turn behaviour in the commit message.

- [ ] **Step 7: Commit**

```bash
git add crates/regie/src/agents prompts crates/regie/tests
git commit -m "feat: Claude agent runner, stream parsing and prompts"
```

---

### Task 11: The engine: one task end to end with fakes

**Files:**
- Create: `crates/regie/src/engine/{mod.rs, input.rs, start.rs, poll.rs}`, `crates/regie/src/bus.rs`, `crates/regie/src/usage.rs`, `crates/regie/src/caffeinate.rs`, `crates/regie/tests/support/harness.rs`, `crates/regie/tests/engine_flow.rs`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `bus.rs`: `enum BusMsg { Event(Event), Output { task: TaskId, run: RunId, line: String } }`, `struct Bus` wrapping `tokio::sync::broadcast::Sender<BusMsg>` (capacity 1024) with `subscribe()` and `publish()`.
  - `engine/input.rs`: `enum Input { Command(Command, Actor, oneshot::Sender<Result<CommandResult, String>>), Agent(RunId, AgentEvent), Tick, Shutdown }`.
  - `engine/mod.rs`: `struct Deps { runner: Arc<dyn AgentRunner>, github: Arc<dyn GitHub>, probe: Arc<dyn Fn() -> BoxFuture<'static, anyhow::Result<Reading>> + Send + Sync>, clock: Arc<dyn Fn() -> Millis + Send + Sync>, caffeinate: bool }`; `struct EngineHandle { tx: mpsc::Sender<Input>, bus: Bus, snapshot: Arc<RwLock<Snapshot>> }` where `Snapshot { state: State, live: LiveView, settings: Settings }`; `pub async fn start(data: DataDir, settings: Settings, deps: Deps) -> anyhow::Result<EngineHandle>` (opens the store, replays, spawns the loop); `impl EngineHandle { pub async fn command(&self, cmd: Command, actor: Actor) -> Result<CommandResult, String>; pub fn board(&self, s: SessionId) -> Option<BoardView>; pub fn task(&self, t: TaskId) -> Option<TaskDetail>; }`.
  - Commands (actor from the caller): `validate`, then: `CreateSession` → `github_remote` for each repo (canonical path) → `SessionCreated`; `SetProjectFacts` → `ProjectFactsSet`; `CreateTask` → `TaskCreated` (returns the new id); `MessageAgent` → `LevelRaised` if a level is given, then `MessageQueued`, and if the task's builder is running, `AgentControl::Message(text)` and `MessageDelivered`. The other commands are Task 12's.
  - The loop: one task owns `Store`, `State`, `LiveView` and the run controls; every change is `append → state.apply → bus.publish → snapshot update → advance()`. A `Tick` every 5 seconds (tests can send it directly).
  - `advance()`:
    1. For each task not cleaned, compute `next_step`.
    2. `AwaitRuling` → append `FindingsRuled` with `auto_rule` (actor `Regie`; milestone 2 replaces this with a lead wake).
    3. `Cleanup` → remove the worktree (spawned), then `WorktreeCleaned`. `ClosePr` → `github.close_pr`, then `PrClosedByRegie`.
    4. `StartRun` → collect `Candidate`s (provider Claude in this milestone; `started_before` = the task has a worktree; `deps_merged` = every `blocked_by` task's PR is merged); `pick`; for each picked: if Claude's reading `needs_probe`, run the probe first and append `UsageReading`; recheck openness; then start.
  - `engine/start.rs`: starting a run: if the task has no worktree: allocate a port block, `create_worktree`, `WorktreeCreated`, then `run_setup` if facts have a setup command, `SetupFinished` (on failure, stop here). Build the prompt with `prompt_for`, choose `resume` = the builder's native session for builder runs other than `Initial`, `None` for reviewer and verify runs (each a fresh agent), append `RunStarted`, call `runner.start`, forward its events as `Input::Agent`.
  - Handling `AgentEvent`: `Attached` → `RunAttached` (and set `builder_native_session` for builder runs); `Output` → append to the run file and publish `BusMsg::Output`, update `last_output`; `Reading` → `UsageReading` (Claude); `Usage` → `RunUsage`; `Finished` → `RunFinished`, then for a reviewer run `ReviewReported { round, parse_review(..) }`, for a verify run `VerifyReported`; if `limit_resets_at` → `LimitHit`.
  - `engine/poll.rs`: every 30 seconds (on `Tick` when due), for each task with `open_pr_requested` or an open PR: `pr_for_branch`; append `PrObserved` when it differs from the stored one; `pr_activity` for new comments → `PrCommentObserved`. When a builder `OpenPr` run finishes, poll at once and then twice more 10 seconds apart; still no PR → `PrMissing`.
  - `usage.rs`: `pub fn openness_now(state: &State, now: Millis, s: &Settings) -> BTreeMap<Provider, Openness>` from stored readings and `blocked_until`.
  - `caffeinate.rs`: `struct Caffeinate` with `fn set_running(&mut self, n: usize)`: starts `caffeinate -i -w <own pid>` when n goes above 0, kills it at 0. Disabled when `Deps::caffeinate` is false (tests).
  - Test harness (`tests/support/harness.rs`): `Harness::new(script)` with a `TempRepo`, a temp `DataDir`, `FakeClaude`, `FakeGitHub`, a settable clock and probe; helpers `create_session_and_task(..) -> TaskId`, `tick()`, `wait_lane(task, Lane, secs)`, `lane(task)`.

- [ ] **Step 1: Write the failing integration tests** in `tests/engine_flow.rs`

```rust
#[tokio::test] async fn one_task_happy_path() {
    // builder Initial: writes src.txt, commits, "Status: done"
    // reviewer round 1: one blocking finding with command evidence
    // builder Fix: commits, "Status: done"; reviewer round 2: {"findings":[]}
    // builder OpenPr: FakeGitHub.set(branch, open, Pending)
    // assert lanes in order: Working → InReview → Working → InReview → Working → Ci
    // set checks Success + Clean → ReadyToMerge; merge → Merged; worktree dir gone; local branch gone
}
#[tokio::test] async fn message_to_running_builder_is_delivered_live() { /* builder hangs; MessageAgent → FakeClaude records AgentControl::Message; no new RunSpec */ }
#[tokio::test] async fn builder_question_then_message() { /* "…?\nStatus: question" → NeedsMe; MessageAgent → builder resumed with the same native session and the text as prompt */ }
#[tokio::test] async fn two_rounds_then_verify_then_needs_me() { /* rounds 1 and 2 blocking; verify says not_fixed → InReview + NeedsMe(VerifyFailed) */ }
#[tokio::test] async fn ci_failure_three_fixes_then_cap() { /* each CiFix run changes head_sha in the fake; 4th failure → NeedsMe(CapReached(Ci)) */ }
#[tokio::test] async fn dependent_task_waits_for_merge() { /* T2 blocked_by T1: Queued + Waiting(Dependency) until T1 merged, then starts */ }
#[tokio::test] async fn setup_failure_needs_me() { /* facts.setup = "exit 1" → Working + NeedsMe(SetupFailed); no builder RunSpec recorded */ }
#[tokio::test] async fn caps_hold_third_task_in_session() { /* 3 tasks, builders hang → 2 running, third Queued + Waiting(Slot) */ }
#[tokio::test] async fn threshold_holds_new_work() { /* probe returns five_hour 0.80 → task Queued + Waiting(Quota); no RunSpec */ }
#[tokio::test] async fn stale_reading_triggers_probe() { /* reading 11 minutes old → probe called once before start */ }
#[tokio::test] async fn pr_missing_after_open_pr_needs_me() { }
#[tokio::test] async fn crash_needs_me_then_retry() { /* FakeAction::Crash → NeedsMe(RunCrashed); RetryTask → builder resumed */ }
```

- [ ] **Step 2: Run them to see them fail**

Run: `cargo test -p regie --test engine_flow`
Expected: FAIL (no engine).

- [ ] **Step 3: Implement the bus, the engine loop, `advance`, starting runs, agent event handling, polling, usage and caffeinate.** Keep `advance` free of waiting: slow work (git, `gh`, setup, probe) runs in spawned tasks that report back through `Input`; add `Input` variants for their results (`WorktreeReady`, `SetupDone`, `Polled`, `Probed`, `CleanedUp`) as needed.

- [ ] **Step 4: Run the tests**

Run: `cargo test -p regie --test engine_flow`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add crates/regie/src crates/regie/tests
git commit -m "feat: engine takes one task from worktree to merge with fakes"
```

---

### Task 12: Pause, stop, cancel, retry and restart recovery

**Files:**
- Create: `crates/regie/src/engine/{control.rs, recover.rs}`, `crates/regie/tests/engine_control.rs`

**Interfaces:**
- Consumes: Task 11's engine and harness.
- Produces:
  - `PauseSession` → `SessionPaused`, then `Interrupt` to every active run in the session; when each run ends, `RunFinished { outcome: Paused }` (the runner reports it). `PauseAll` → `AllPaused` and the same for every session. The scheduler holds paused sessions (`QueuedReason::Paused`).
  - `ResumeSession`, `ResumeAll` → the events; `next_step` rule 5 resumes paused runs with the resume note; the builder's native session is reused.
  - `StopAgent` → `Stop` control; the run ends `Stopped`; the task then waits (`AwaitHuman(RunCrashed)` is not used: add `HumanReason::AgentStopped` to core and to `lane`'s "Working" group).
  - `CancelTask { close_pr }` → stop any run, `TaskCancelled`; `next_step` rule 1 then closes the PR if asked and cleans up.
  - `RetryTask` → `TaskRetried`; the next step resumes the last run's role.
  - `recover.rs`: `pub async fn recover(state: &State, now: Millis) -> Vec<NewEvent>`: for each run started and not finished: if its `pid` is alive (`kill(pid, 0)`), SIGTERM then SIGKILL after 5 seconds; then `RunFinished { outcome: Interrupted }`. Called once by `engine::start` after replay and before the loop. Also deletes run files older than `output_retention_days`.

- [ ] **Step 1: Write the failing tests**

```rust
#[tokio::test] async fn pause_interrupts_and_resume_continues() { /* builder hangs; Pause → run ends Paused, lane Working + Paused flag, no new RunSpec; Resume → new builder RunSpec with resume Some(native) and the resume note */ }
#[tokio::test] async fn pause_all_holds_every_session() { }
#[tokio::test] async fn stop_agent_waits_for_frederic() { /* NeedsMe(AgentStopped); Retry resumes */ }
#[tokio::test] async fn cancel_closes_pr_and_cleans_up() { /* FakeGitHub records close_pr; worktree gone; lane Cancelled */ }
#[tokio::test] async fn restart_resumes_interrupted_run() {
    // kill point: builder hangs; drop the EngineHandle (Shutdown) without finishing the run;
    // start a new engine on the same DataDir → recover appends RunFinished Interrupted
    // → a new builder RunSpec with resume Some(native) and the note "You were interrupted…"
}
#[tokio::test] async fn old_run_files_pruned() { }
```

- [ ] **Step 2: Run them to see them fail**

Run: `cargo test -p regie --test engine_control`
Expected: FAIL.

- [ ] **Step 3: Implement the controls and `recover`.**

- [ ] **Step 4: Run all tests**

Run: `cargo test`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add crates
git commit -m "feat: pause, stop, cancel, retry and restart recovery"
```

---

### Task 13: The HTTP server: auth, commands, views and the event stream

**Files:**
- Create: `crates/regie/src/server/{mod.rs, auth.rs, api.rs, sse.rs, web.rs}`, `crates/regie/tests/server.rs`

**Interfaces:**
- Consumes: `EngineHandle`, `Store::load_after` (through a read-only second connection opened by the server), `Bus`.
- Produces:
  - `pub struct ServerInfo { port: u16, token: String, pid: u32 }` written as JSON to `DataDir::server_json()` (file mode 0600).
  - `pub fn router(engine: EngineHandle, data: DataDir, info: ServerInfo) -> axum::Router`; `pub async fn serve(engine, data, port: u16) -> anyhow::Result<()>` binding `127.0.0.1:<port>` (default port 7317; `0` picks a free one), generating a 32-byte random hex token.
  - Auth (`auth.rs`), applied to every route:
    - `Host` must be `127.0.0.1:<port>` or `localhost:<port>`, else 403.
    - The token is accepted from `Authorization: Bearer <token>` or the cookie `regie_token`; missing or wrong → 401, except `GET /` with `?token=<t>`: when right, it sets `regie_token` (`HttpOnly; SameSite=Strict; Path=/`) and redirects 303 to `/`.
    - Requests other than GET and HEAD must have no `Origin` header (the CLI) or `Origin` equal to `http://127.0.0.1:<port>` or `http://localhost:<port>`, else 403.
  - Routes: `GET /api/health` → `{"ok":true,"version":..}`; `POST /api/commands` (body `Command`, actor `Frederic`) → `200 {"result": CommandResult}` or `400 {"error": ".."}`; `GET /api/sessions` → `Vec<Session>`; `GET /api/board?session=<id>` → `BoardView`; `GET /api/tasks/<id>` → `TaskDetail`; `GET /api/runs/<id>/output?lines=<n>` → the last n lines of the run file as text; `GET /api/events?after=<id>` (also reads `Last-Event-ID`) → SSE: first every stored event after the id as `event: event`, `id: <event id>`, `data: <Event JSON>`, then live `BusMsg`s (stored events the same way, skipping ids already sent; output as `event: output` with `data: {"task","run","line"}` and no id); keep-alive every 15 seconds.
  - `web.rs`: `#[derive(RustEmbed)] #[folder = "$CARGO_MANIFEST_DIR/../../web/dist"] #[allow_missing = true] struct Assets;` serving `index.html` at `/` and assets by path; when missing, `/` returns a short text page saying to run `make web`.

- [ ] **Step 1: Write the failing tests** (using `tower::ServiceExt::oneshot` on the router with a harness engine)

```rust
#[tokio::test] async fn rejects_missing_token() { /* GET /api/health → 401 */ }
#[tokio::test] async fn rejects_wrong_host() { /* Host: evil.com → 403 */ }
#[tokio::test] async fn rejects_foreign_origin_on_post() { /* Origin: https://evil.com → 403 */ }
#[tokio::test] async fn token_query_sets_cookie() { /* GET /?token=t → 303, Set-Cookie regie_token=t; HttpOnly; SameSite=Strict */ }
#[tokio::test] async fn create_session_and_task_by_command() { /* 200, CommandResult::Task(TaskId(1)); GET /api/board shows the card */ }
#[tokio::test] async fn invalid_command_is_400_with_message() { /* CreateTask without facts → 400 with "no project facts" */ }
#[tokio::test] async fn sse_replays_after_id() { /* 3 events; connect with after=1 → receives ids 2 and 3 first */ }
```

- [ ] **Step 2: Run them to see them fail**

Run: `cargo test -p regie --test server`
Expected: FAIL.

- [ ] **Step 3: Implement the server.**

- [ ] **Step 4: Run the tests**

Run: `cargo test -p regie --test server`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add crates/regie/src/server crates/regie/tests/server.rs
git commit -m "feat: local HTTP API with token, origin checks and event stream"
```

---

### Task 14: The CLI

**Files:**
- Create: `crates/regie/src/cli.rs`, `crates/regie/src/client.rs`
- Modify: `crates/regie/src/main.rs`
- Test: `crates/regie/tests/cli.rs`

**Interfaces:**
- Produces (clap derive), all accepting `--data-dir <path>`:
  - `regie` with no subcommand: read `server.json`; if `GET /api/health` answers, open `http://127.0.0.1:<port>/?token=<token>` in the browser (`open`); otherwise start `regie serve --no-open` as a detached child (new process group, output to `<data>/server.log`), wait up to 5 seconds for health, then open the browser.
  - `regie serve [--port <n>] [--no-open]`: starts the engine with real deps (`ClaudeCli` with `claude` from `PATH`, `GhCli`, the real probe, the system clock, caffeinate on) and the server.
  - `regie session new <name> <repo>...` → `CreateSession`, prints `session <id>`.
  - `regie project set <repo> --file <facts.json>` → `SetProjectFacts`.
  - `regie task new --session <id> --repo <path> --title <t> --instruction <text|@file> [--ticket <k>]... [--blocked-by <T…>]... [--level light|standard|hard]` → prints `T<id>`.
  - `regie task message <T…> <text>`, `regie task stop <T…>`, `regie task retry <T…>`, `regie task cancel <T…> [--close-pr]`.
  - `regie pause [<session>|--all]`, `regie resume [<session>|--all]`.
  - `regie board [--session <id>]`: prints one line per card: `T3  Working  [needs me: builder question]  Add CSV export`.
  - `client.rs`: `struct Client { base: String, token: String }` with `fn from_data_dir(&DataDir) -> anyhow::Result<Client>`, `async fn command(&self, cmd: &Command) -> anyhow::Result<CommandResult>`, `async fn board(&self, s: SessionId) -> anyhow::Result<BoardView>`, `async fn sessions(&self)`. Repo paths are canonicalised before sending. `T3` and `3` both parse as `TaskId(3)`.

- [ ] **Step 1: Write the failing tests**

```rust
#[test] fn parses_task_new() { /* args → Command::CreateTask with level Light, two tickets, blocked_by [TaskId(2)] */ }
#[test] fn task_ids_accept_t_prefix() { }
#[tokio::test] async fn client_round_trip_against_server() { /* in-process server on port 0 with a harness engine; Client::command(CreateSession) → Session(SessionId(1)) */ }
```

- [ ] **Step 2: Run them to see them fail**

Run: `cargo test -p regie --test cli`
Expected: FAIL.

- [ ] **Step 3: Implement the CLI and client.**

- [ ] **Step 4: Run the tests**

Run: `cargo test -p regie --test cli`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add crates/regie/src crates/regie/tests/cli.rs
git commit -m "feat: regie CLI for sessions, project facts and tasks"
```

---

### Task 15: The web page: board, inspector and top bar

**Files:**
- Create: `web/package.json`, `web/vite.config.ts`, `web/tsconfig.json`, `web/index.html`, `web/src/main.tsx`, `web/src/api/{client.ts, events.ts}`, `web/src/store.ts`, `web/src/components/{App.tsx, TopBar.tsx, Meter.tsx, Board.tsx, Monitor.tsx, Inspector.tsx}`, `web/src/theme.css`, `web/src/**/*.test.ts(x)`
- Reference: `docs/superpowers/specs/2026-11-19-lead-agent-orchestrator-mockups/main-screen.html` (Board view part)

**Interfaces:**
- Consumes: the generated types in `web/src/api/gen/` (`BoardView`, `TaskCard`, `TaskDetail`, `Lane`, `Flag`, `Command`, `Event`, `Openness`…), the API routes of Task 13.
- Produces:
  - `client.ts`: `getSessions()`, `getBoard(session)`, `getTask(id)`, `getOutput(run, lines)`, `send(cmd: Command)`; all `fetch` with `credentials: "same-origin"`; a 401 shows "Open Régie from the `regie` command" instead of the app.
  - `events.ts`: `connect(onEvent: (e: Event) => void, onOutput: (o: {task: number, run: number, line: string}) => void)`, using `EventSource("/api/events?after=<last id>")` and reconnecting with the last id after an error (2 seconds, then backing off to 30).
  - `store.ts` (`@preact/signals`): `sessions`, `currentSession`, `board`, `selectedTask`, `taskDetail`, `liveLines` (a map task → last 200 lines). Any stored event for the current session refetches the board, at most every 250 ms; events for the selected task refetch its detail.
  - `TopBar`: "RÉGIE", the studio (session) selector, the ON AIR sign with the running count ("ON AIR · 3", unlit text "OFF AIR" at 0), a `Meter` per Claude window (5h, 7d), Standby/Resume for the current session (`PauseSession` / `ResumeSession`), Fade to black / Fade in (`PauseAll` / `ResumeAll`). The Board / Lead toggle is milestone 2.
  - `Meter`: label, percentage text, a bar with the threshold zone from 75% (5h) and a pace tick at the week's elapsed share (7d); `title` attribute with the reading's age ("read 4 min ago") and the openness reason.
  - `Board`: columns in `Lane` order with counts; `Monitor` cards with a tally light plus its text label (`ON AIR` red for an active run, `READY` green for ReadyToMerge, `NEEDS ME` amber, `OFF` otherwise), the title, `T<id>`, level and model, tickets or `PR #n`, and the last live line.
  - `Inspector` (a drawer from the right): lane and flags; level, model; tickets, branch, PR link; runs with outcome and usage; a timeline of the task's events in plain words; the live output; a reply box when the card has `NeedsMe` (sends `MessageAgent`); buttons Stop agent, Retry, Cancel task (with a confirm that offers "also close the PR"), Raise level.
  - `theme.css`: colour tokens on `:root` for light, overridden under `@media (prefers-color-scheme: dark)`; tally and ON AIR glow animations disabled under `@media (prefers-reduced-motion: reduce)`.
  - `vite.config.ts`: dev proxy of `/api` to `http://127.0.0.1:7317`; build output `web/dist`.

- [ ] **Step 1: Scaffold and install**

Run: `cd web && pnpm init && pnpm add preact @preact/signals && pnpm add -D vite @preact/preset-vite typescript vitest @testing-library/preact jsdom && cd ..`
Add scripts: `"dev": "vite"`, `"build": "vite build"`, `"test": "vitest run"`, `"typecheck": "tsc --noEmit"`.
Expected: `pnpm -C web typecheck` runs (errors are fine before the code exists).

- [ ] **Step 2: Write the failing tests**

```ts
test("board groups cards into lane columns in lane order", ...)        // Board with cards in mixed lanes → column headers Queued…Merged with counts
test("monitor shows tally label text, not colour only", ...)          // NeedsMe card → text "NEEDS ME"; active run → "ON AIR"
test("meter marks the threshold zone and the week's pace", ...)        // 7d reading → element with data-pace at the elapsed share
test("events reconnect with the last id", ...)                         // fake EventSource error → new URL contains after=<last id>
test("reply box appears only for needs-me cards and sends MessageAgent", ...)
```

- [ ] **Step 3: Run them to see them fail**

Run: `pnpm -C web test`
Expected: FAIL.

- [ ] **Step 4: Implement the page**, following the mockup's Board view layout without the left rail's talkback (milestone 2): the studio list sits in the top bar's selector for now.

- [ ] **Step 5: Run the tests, the type check and the build**

Run: `pnpm -C web test && pnpm -C web typecheck && pnpm -C web build`
Expected: tests pass, no type errors, `web/dist/index.html` exists.

- [ ] **Step 6: Commit**

```bash
git add web
git commit -m "feat(web): board, inspector and top bar"
```

---

### Task 16: One binary with the page inside, and the build commands

**Files:**
- Modify: `Makefile`, `CLAUDE.md`, `crates/regie/src/server/web.rs` (if needed)
- Test: `crates/regie/tests/server.rs` (add one test)

**Interfaces:**
- Produces: `make web` (`pnpm -C web install --frozen-lockfile && pnpm -C web build`), `make build` (`make web && cargo build --release`), `make test` (`cargo test && pnpm -C web test && pnpm -C web typecheck`), `make dev` (prints the two commands: `cargo run -p regie -- serve --no-open` and `pnpm -C web dev`).

- [ ] **Step 1: Write the failing test**

```rust
#[tokio::test] async fn serves_embedded_index_when_built() { /* skip when web/dist is missing; otherwise GET / with the cookie → 200 text/html containing "RÉGIE" */ }
```

- [ ] **Step 2: Build the page and run it**

Run: `make web && cargo test -p regie --test server serves_embedded_index_when_built`
Expected: PASS.

- [ ] **Step 3: Build the release binary**

Run: `make build && ./target/release/regie --version`
Expected: the version line.

- [ ] **Step 4: Update `CLAUDE.md`** with the dev loop and the build commands as they now work.

- [ ] **Step 5: Commit**

```bash
git add Makefile CLAUDE.md crates/regie web/pnpm-lock.yaml
git commit -m "build: embed the web page and add make targets"
```

---

### Task 17: Live run on a real repo, and the demo

**Files:**
- Create: `docs/superpowers/plans/2026-11-19-milestone-1-live-run.md` (the notes from this run)

- [ ] **Step 1: Pick the repo with Frédéric.** Ask which GitHub repo to use (a small one he doesn't mind getting a PR in), and the ticket-free instruction to give. Recommend turning on branch protection for its default branch first.

- [ ] **Step 2: Install and start**

Run: `cargo install --path crates/regie && regie`
Expected: the browser opens on the empty board.

- [ ] **Step 3: Set up and start one task**

Run: `regie session new demo <repo>`; write `facts.json` with the repo's real setup, test and lint commands and files to copy; `regie project set <repo> --file facts.json`; `regie task new --session 1 --repo <repo> --title "<title>" --instruction "<instruction>" --level light`
Expected: the card goes Queued → Working, the ON AIR sign lights, live output streams in the inspector, the Claude meters show readings.

- [ ] **Step 4: Watch it through**

Expected: review runs (In review), fixes if any, the builder opens the PR (CI), checks finish, the card reaches Ready to merge. Note any step that needed a human answer and why.

- [ ] **Step 5: Exercise the controls**

Run: press Standby during a run, then Resume; `kill -9` the `regie serve` process during a run and run `regie` again.
Expected: the run stops and continues its session; after the kill, the run resumes with the "You were interrupted" note and no orphan `claude` process remains (`pgrep -fl "claude -p"`).

- [ ] **Step 6: Frédéric merges the PR on GitHub**

Expected: within a minute the card shows Merged and the worktree and local branch are gone.

- [ ] **Step 7: Write the live-run notes** (what worked, what needed a human, timings, tokens per run, anything to fix in milestone 2's plan), then commit them.

```bash
git add docs/superpowers/plans/2026-11-19-milestone-1-live-run.md
git commit -m "docs: milestone 1 live run notes"
```
