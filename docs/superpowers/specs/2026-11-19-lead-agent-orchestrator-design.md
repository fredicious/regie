# Lead-agent orchestrator: design

**Status: DRAFT, design conversation in progress. Not approved.**
Handed over to a fresh session on 2026-11-19 because the context window was full.

**Inputs:**
- `docs/requirements/2026-11-19-lead-agent-orchestrator.md` (R1 to R24, revision 3 with the spike results);
- `docs/requirements/2026-11-19-lead-agent-orchestrator-fable-review.md`;
- `docs/spikes/2026-11-19-lead-agent-spike.md`.

## How to continue (for the next session)

The conversation follows the `superpowers:brainstorming` skill.

**Done so far:**
- echo and findings;
- approaches laid out, and Frédéric picked;
- two playback chunks agreed: the architecture and the task flow.

**Next:**
1. **Play back the remaining parts** one at a time (see "Still to cover"), each about 200 to 300 words, asking what's wrong or missing.
2. **Turn this draft into the full written design.**
3. **Run the builder check** (`builder-check-prompt.md` in the brainstorming skill's directory).
4. **Get Frédéric's approval.**
5. **Invoke `superpowers:writing-plans`.**

**How Frédéric works:**
- Plain, concise messages.
- He decides quickly and accepts recommendations that come with clear trade-offs.
- **Only agents will write and maintain this code.** He'd like to learn Rust along the way.
- Commit on `main` and never push (see memory).

## Intent

A local tool Frédéric uses every day instead of juggling agents by hand:

- He opens a web dashboard and talks to a **lead agent** for each session.
- The lead takes tickets from his own backlog (Jira, through his Claude connectors), splits them into tasks, and delegates them to sandboxed builders and cross-provider reviewers on his Claude and Codex subscriptions.
- It brings each PR to "ready to merge". He merges.
- Around that: usage-limit thresholds, pause, a single Needs me queue, memory and self-improvement, a decision log, and a light régie (TV control room) look.

All requirements are in the requirements doc. This design doesn't repeat them.

## Approaches considered (decided)

**Server language: Rust.**
- **Python inside Régie** was the first recommendation. It reused about 1,500 lines (limit parsing, provider cooldowns, worktree setup, gates, knowledge, stats), the tests, and `regie-bench`'s Python harness.
- **TypeScript everywhere** was considered and rejected.
- **Why Rust won:**
  - Only agents maintain the code, so the compiler is a guardrail for a long-running process supervisor.
  - The Codex repo provides Rust crates `codex-rs/app-server-protocol` and `app-server-client` for its server.
  - It ships as a single binary.
  - Frédéric would like to learn Rust.
- **Cost:** the reusable Python is ported (about a day of agent work), and rebuilds are slower. `regie-bench` stays in Python and drives the CLI.

**Web page: Preact + TypeScript (TSX), built with Vite.**
- **Server-rendered HTML with htmx** was rejected: Frédéric's point was that if the core speaks data, every client should get data, the web page included.
- **React** was rejected as heavier than needed.
- **Rust compiled to WebAssembly** (Dioxus, Leptos, Yew) was considered. It has a heavier toolchain, slower UI iteration, larger pages, and agents write it less fluently.
- **`htm` without a build step** was dropped when Frédéric asked for **TypeScript from the start**.
- **How it ships:**
  - Node is needed only to build.
  - The built page is embedded in the Rust binary (for example with `rust-embed`).
  - TypeScript types are generated from the Rust API definitions (`ts-rs`).

**Forking Orchestrator.inc: rejected.** It's Apache-2.0 but about 60 MB of Go and TypeScript with desktop, mobile and cloud parts. We borrow its ideas, not its code (see "Reference design" in the requirements).

## Decided so far

### Architecture

```
 web (Preact/TS) ─┐
 TUI (later)      ├─ localhost + token ─▶ regie (one Rust binary)
 native (later)  ─┤                         ├─ core: commands, queries, event stream (JSON API + SSE)
 lead tools (MCP)─┤                         ├─ store: SQLite ~/.regie/regie.db, append-only fact log
 CLI / bench     ─┘                         ├─ scheduler: caps, thresholds, dependencies
                                            ├─ usage watcher: Claude rate_limit_event, Codex polling
                                            ├─ git: worktrees, setup, commit/push/PR, CI via gh
                                            └─ supervisor: leads + Claude agents (`claude -p` stream-json, resume by id)
                                                           Codex agents (threads in one `codex app-server`)
```

**The core speaks data, never HTML.**
- Every action is a typed command, every view is a query, and every change is an event on one stream.
- The web page, a future TUI (`ratatui`, same binary), a future native app (Tauri around the web page, or SwiftUI on the API), the lead's tools, the CLI, the tests and the bench all use this one contract.

**The process:**
- `regie` starts the server if it isn't running and opens the browser.
- It runs until Frédéric quits it. No login item is installed.
- It listens on 127.0.0.1 and checks a per-launch token (R11).

**The store:**
- Facts are appended to a log: task created, agent started, finding, escalation, answer, and so on.
- Lanes (R9), the Needs me queue (R18) and the decision log (R23) are derived from it.

**The lead:**
- One Claude conversation per session, long-lived like Orchestrator.inc's. Events reach it as batched messages (resume by session id).
- It acts only through Régie's tools, exposed over MCP: create task, assign, request review, escalate, propose follow-up, update ticket, remember.
- Régie checks each action, then carries it out.
- All state stays in Régie, so a lead can be restarted from a state summary when its context grows too large (R12).

**Workers:**
- Each runs sandboxed in its own worktree and ends with a structured report: done or blocked, what changed, any question.
- The lead can redirect a worker mid-run: Codex through `turn/steer`; Claude by stopping and resuming, or through streaming input (to settle).

**Side effects:**
- Régie does all commits, pushes, PRs, CI polling and cleanup.
- Jira updates go through a Claude call allowed to use only the comment and status tools (R2, R14).

### Task flow (agreed: "sounds good for a start")

```
you: "work on the next 2 tickets of ABC"
  → lead reads Jira, creates tasks ── Queued
  → free slot + provider under threshold + dependencies merged
  → Régie: fetch, worktree + branch, copy .env, install dependencies
  → builder (sandboxed) ── Working ──▶ report: done / blocked + question
  → Régie runs build/test/lint ── fail → back to builder
  → reviewer on the other provider ── In review ── blocking finding → fix round (max 2, then verify pass)
  → Régie commits, pushes, opens PR; lead updates Jira ── CI
  → CI red / changes requested / conflict → builder fixes (max 3 each)
  → review closed + CI green + no conflict ── Ready to merge → Needs me queue + notification
  → Frédéric merges on GitHub → Merged; cleanup; dependent tasks start
```

### Calls made (Frédéric can correct them)

- **Authorship:** commits and PRs use Frédéric's git and `gh` identity. A trailer on each commit names the agent and provider that wrote it.
- **Dependencies:** a dependent task waits until the task it depends on is merged. No stacked branches in the first version (as decided in the requirements).
- **Lead model:** a long conversation fed with events, and replaceable from the tool's state. This settles the open point in R3 and the requirements' "Reference design".

## Still to cover in the conversation

1. **The lead in detail:**
   - what it sees when it's woken (how its context is assembled);
   - the exact tool set and schemas;
   - batching and wake-up rules;
   - when it's replaced, and from what summary;
   - its model and effort.
2. **The data model:**
   - event types and tables;
   - derived views, with the lane order of precedence (R9);
   - escalations and queue items (R18);
   - lessons (R19 to R21);
   - settings, and log entries with edit and revert (R23).
3. **Agent profiles and sandboxing:**
   - **Claude:** `--permission-mode dontAsk`, an allowlist, `--disallowedTools`, sandbox settings.
   - **Codex:** `workspace-write`, no network.
   - **Around the agents:** dependency setup outside the sandbox, copying untracked files, ports that don't collide (R14).
   - **The open spike items:** Claude's sandbox write limits and registry allowlist, connectors inside the sandbox, resuming a killed Claude session.
4. **Usage and scheduling:**
   - the thresholds (R10), and how stale Claude's reading is when idle;
   - pause (R16): Claude by stopping and resuming, Codex with `turn/interrupt`;
   - "run anyway".
5. **Memory, self-improvement and the log (R19 to R23):** storage, how lessons reach prompts, reflection.
6. **The web page:**
   - **Pages:** the board ("multiviewer"), the task inspector, the lead chat, the Needs me queue (talkback), usage meters, the log, settings.
   - **The look:** the light régie touch (R24).
7. **The repo layout:**
   - a Rust workspace inside the Régie repo;
   - what happens to the Python code (retired once ported);
   - how `regie-bench` connects.
8. **The first-version build plan:** milestones, handed to `superpowers:writing-plans`.
