# Lead-agent orchestrator: design

**Date:** 2026-11-19
**Status:** approved by Frédéric on 2026-11-19, then revised the same day after he asked why agents were so restricted: agents now work like his own sessions (see "Agent autonomy" and section 4). Next: the implementation plan for milestone 1.

**Inputs:**
- `docs/requirements/2026-11-19-lead-agent-orchestrator.md` (R1 to R24, revision 4). This design doesn't repeat the requirements; it says how to meet them.
- `docs/requirements/2026-11-19-lead-agent-orchestrator-fable-review.md`.
- `docs/spikes/2026-11-19-lead-agent-spike.md`, plus the checks run during the design (see "Checks run during the design").
- The agreed main-screen mockup: `2026-11-19-lead-agent-orchestrator-mockups/main-screen.html`. It's an HTML fragment that uses the brainstorming companion's colour variables, so open it in a browser and expect plain fallbacks.

**How to read it.** "Decided" marks what Frédéric chose in the conversation. "Call" marks what the designer chose and Frédéric can still correct; all calls are listed again in "Calls made". Everything else follows from the requirements.

## Intent

A local tool Frédéric uses every day instead of juggling agents by hand.

- He opens a web dashboard and talks to a **lead agent** for each session.
- The lead takes tickets from his own backlog (Jira, through his Claude connectors), splits them into tasks, and delegates them to builders and cross-provider reviewers on his Claude and Codex subscriptions.
- The agents work autonomously, like his own sessions, and bring each PR to "ready to merge". He merges.
- Around that: usage-limit thresholds, pause, a single Needs me queue, memory and self-improvement, a log of decisions, and a light régie (TV control room) look.

**Why:** existing tools failed his bar of "open a folder, say start this work". Paperclip needed too much setup; Claude Code with the Codex plugin has no backlog, board or lead; Orchestrator.inc comes closest and is the reference design, but loses requests that need him across sessions and doesn't manage usage limits.

**What Régie adds over a single session** is what no single session can do: one worktree per task, scheduling across sessions and providers within usage limits, state that survives restarts, the board, the Needs me queue, memory and the log.

## Goals, non-goals, anti-goals

**Goals (first version):** the acceptance scenario in the requirements passes, and Frédéric uses it daily from milestone 2.

**Non-goals:** everything in the requirements' "Not in the first version": mid-run provider switch at 90%, a lead on Codex, a desktop app or TUI, letting anyone but Frédéric merge, hosts other than GitHub, multi-repo tasks, phone notifications, and memory beyond R19 to R22.

**Anti-goals (a failure even if it works):**
- **Setup ceremony.** Anything to configure before the first session beyond pointing at folders.
- **Babysat agents.** Agents work like Frédéric's own sessions. Restrictions are added only when a real problem shows up.
- **Requests that need Frédéric getting lost.** Everything waiting on him is in one queue, always visible.
- **Runaway loops and runaway usage.** Every loop is capped; thresholds stop new work before a limit.
- **A theme that costs clarity.**

## Constraints

- macOS, local only, one user. The server listens on 127.0.0.1 and checks a per-launch token and the request origin (R11).
- Agents use Frédéric's own Claude Code and Codex installs: their logins, plugins, skills, hooks and connectors (R1). No API keys.
- Only Claude reaches Jira on this Mac. Codex must reuse the login in `~/.codex/auth.json`.
- **Only agents write and maintain this code.** Frédéric wants to learn Rust along the way.
- Versions seen on 2026-11-19: Claude Code 2.1.328, Codex CLI 0.170.0 (`gpt-6.1-sol`).

## Approaches considered

All decided by Frédéric.

**Server language: Rust.**
- **Python inside Régie** was first recommended: it reused about 1,500 lines (limit parsing, provider cooldowns, worktree setup, gates, knowledge, stats), the tests, and `regie-bench`'s harness.
- **TypeScript everywhere** was rejected.
- **Why Rust:** only agents maintain the code, so the compiler is a guardrail for a long-running process supervisor; the Codex repo provides Rust crates for its app-server protocol; it ships as one binary; Frédéric wants to learn it.
- **Cost:** the reusable Python is ported, and rebuilds are slower. `regie-bench` stays in Python.

**Web page: Preact + TypeScript (TSX), built with Vite.**
- **Server-rendered HTML with htmx** was rejected: if the core speaks data, every client gets data, the web page included.
- **React** was heavier than needed. **Rust compiled to WebAssembly** (Dioxus, Leptos, Yew) has a heavier toolchain, slower UI iteration and larger pages. **`htm` without a build step** was dropped when Frédéric asked for TypeScript from the start.
- Node is needed only to build. The built page is embedded in the binary. TypeScript types are generated from the Rust API types (`ts-rs`).

**Forking Orchestrator.inc: rejected.** About 60 MB of Go and TypeScript with desktop, mobile and cloud parts. We borrow its ideas, not its code.

**Agent autonomy: like Frédéric's own sessions.**
- **Rejected: sandboxed, restricted agents.** An earlier version of this design ran every agent in its CLI's sandbox with no network, tool allowlists, hidden connector tools, hooks and skills off, and Régie doing every commit, push, PR and Jira write. That came from the requirements' old R12 and R14, which a reviewer had added ("the tool enforces limits; they don't depend on agents following instructions"), not from anything Frédéric asked for.
- **Chosen:** agents run with his full setup, network and no permission prompts. Builders commit, push and open their PR; the lead updates Jira. Régie prepares, schedules, watches and cleans up, and never merges. Restrictions are added when a problem shows up.
- **Why:** "the whole point of the agents is to be autonomous, not baby sitted." Agents need network for research and dependencies, and his skills and hooks are part of how he works. The strict version also cost a lot of design and build work: sandbox profiles, a report tool, Régie's git and PR plumbing, a separate Jira writer, and dependency round trips.
- **Accepted risk:** see section 4.

**The lead: one long conversation fed with events, replaceable from the tool's state.** The requirements' R3 had the tool rebuild the lead's picture on every call; Orchestrator.inc keeps one conversation. The middle way was chosen: one conversation, but every wake message carries a board snapshot, and all state lives in Régie so a lead can be replaced at any time.

**Choosing workers' model and effort: levels.** Three options were weighed: fixed per role (simple, wastes quota on trivial tasks), the lead picks model and effort freely (ties prompts to model names, drifts to the biggest model), or the lead picks a level and settings turn it into a model and effort. Levels won (see section 2).

**Main screen: two views, never side by side, with lanes as columns.** A wall of monitors was the alternative for the board; showing the chat beside the board left it too narrow. Following Orchestrator.inc, the board and the lead chat are two views of the whole screen.

**Build order: the lead before Codex.** Daily use starts one milestone earlier; until Codex lands, all work runs on Claude. Frédéric has usage resets to spend if needed.

## Architecture

```
 web (Preact/TS) ─┐
 TUI (later)      ├─ localhost + token ─▶ regie (one Rust binary)
 native (later)  ─┤                         ├─ core: commands, queries, event stream (JSON API + SSE)
 lead tools (MCP)─┤                         ├─ store: SQLite ~/.regie/regie.db, append-only event log
 CLI / bench     ─┘                         ├─ scheduler: slots, thresholds, dependencies
                                            ├─ usage: Claude rate_limit_event + probe, Codex polling
                                            ├─ git: worktrees, setup, cleanup; PR and CI state via gh
                                            └─ supervisor: leads and Claude agents (`claude -p`, stream-json)
                                                           Codex agents (threads of one `codex app-server`)
```

**The core speaks data, never HTML.** Every action is a typed command, every view is a query, and every change is an event on one stream. The web page, the lead's tools, the CLI, the tests and the bench all use this one contract. A lead tool call is the same command a button would send, with the lead as the actor.

**The process.** `regie` starts the server if it isn't running and opens the browser. It runs until Frédéric quits it; no login item is installed. `regie serve --data-dir <dir>` runs an isolated instance (tests, bench).

**Who does what.** Agents do the work end to end, like a session: builders edit, run the project's checks, commit, push and open the PR; reviewers review; the lead plans, rules and updates Jira. Régie prepares worktrees, schedules runs, tells agents what to do next, watches PRs and CI, keeps the state, and cleans up. Nobody but Frédéric merges.

## Task flow

```
you: "work on the next 2 tickets of ABC"
  → lead reads Jira, creates tasks, moves tickets to "in progress" ── Queued
  → free slot + provider open + dependencies merged
  → Régie: fetch, worktree + branch, copy listed files, run setup
  → builder ── Working: builds, runs the project's checks, commits
       ──▶ final message: done, or a question for the lead
  → reviewer on the other provider reviews the branch ── In review
       → findings to the lead, who rules on scope
       → blocking in-scope finding → fix round (max 2, then a verify-only pass)
  → review closed → Régie tells the builder to push and open the PR ── CI
  → lead moves the tickets to "in review", with a comment linking the PR
  → CI red / changes requested / conflict → builder fixes and pushes (max 3 each)
  → review closed + CI green + no conflict + branch rules met ── Ready to merge → Needs me queue
  → Frédéric merges on GitHub → Merged; cleanup; dependent tasks start
```

The order (build, local checks, review, PR, CI) is R6's, decided in the requirements. The builder runs the local checks itself, with the commands confirmed at onboarding.

### After the PR opens (call)

- **Failed CI or a merge conflict:** Régie resumes the builder with the failure (the CI summary, or "your branch conflicts with `main`"). It fixes, runs the checks and pushes. The lead hears about it in its next wake.
- **Changes requested in a GitHub review, by a person or a bot:** wakes the lead, which rules on scope as it does for reviewer findings, then sends the in-scope part to the builder with `message_agent`. Nothing goes to the builder without that ruling, so the lead and the builder never both act on the same feedback.
- **Plain comments** (no "changes requested"), from people or bots: wake the lead, which decides whether anything goes to the builder. They don't count towards any cap.
- **No extra agent review for post-PR fixes:** CI and Frédéric's own look before merging are the check.
- **Caps:** each post-PR fix counts once towards the cap of 3 for its kind (CI, PR feedback, conflict). At the cap, the lead is woken and must escalate.

## 1. The lead

### How it runs (call)

- **One Claude conversation per session.** No lead process waits idle. Each wake is one `claude -p --resume <conversation id>` turn with `--output-format stream-json`. The first turn of a conversation uses `--session-id <uuid>` chosen by Régie.
- **Working folder:** a per-session folder under `~/.regie/sessions/<id>/`, with each of the session's repos added through `--add-dir` so the lead can read them.
- **Setup and permissions:** Frédéric's full setup (plugins, skills, hooks, connectors) and `--permission-mode auto`, like his own sessions. Régie's MCP endpoint is added with `--mcp-config` and a token that scopes it to this session.
- **Its role**, set by its prompt: it plans, delegates, rules on findings, escalates and updates tickets. It doesn't edit code itself.
- **Text it writes** goes to the session chat. Text from turns woken only by events is shown in a compact, dimmer style. Its prompt asks it to stay brief after event batches.
- **One turn at a time per session.** A turn has a 10-minute wall-clock limit (call); past it, Régie interrupts it and logs that.
- **Pause** means no wakes; a running lead turn is interrupted.
- **Usage:** thresholds don't hold the lead back. A real Claude limit makes wakes wait until the window resets.

### What it sees when woken

**Standing context** (appended system prompt, kept unchanged for the life of one conversation so the cache holds):
- its role and rules: the must-escalate list from R3, R2's ticket allowance, how to use its tools, the levels and when to use each;
- the session's repos and their project facts (R15);
- up to 20 lessons (R20): Frédéric's own ("me") lessons and the session's repo lessons, most recently confirmed first;
- its current plan.

Later changes to facts, lessons or the plan reach it as events.

**Each wake message** has three parts, in this order:
1. Frédéric's chat messages since the last turn, in full.
2. The events since the last turn, one line each, oldest first. For example: `14:02 T3 builder finished — "Added retry with backoff; checks pass"`. Agents' final messages and review findings are included in full; other details stay behind the read tools.
3. A board snapshot: every task not merged or cancelled, one line each (id, title, lane, flags, level, agent), the open Needs me items, and one line of usage per provider.

The snapshot means the lead never has to rebuild the board from memory of past events.

### Tools

Every tool call is a command. Régie checks it and either applies it or returns an error with the reason, which the lead can correct.

| Tool | Input | What Régie does, and checks |
|---|---|---|
| `create_task` | repo, title, instruction, tickets[], blocked_by[], level (light, standard, hard; default standard), paths[] (expected areas, optional), provider (preference, optional) | Creates the task in Queued and returns its id. Checks: the repo is in the session, is a git repo with a GitHub remote, and has confirmed onboarding; `blocked_by` tasks exist in the session; no dependency cycle. |
| `message_agent` | task, text, level (optional, only higher) | If the task's builder is running, delivers the text into the run (redirect). Otherwise starts the task's next builder run, resuming the builder's session with this text. Checks: task not merged or cancelled; level only goes up. |
| `rule_findings` | task, review, rulings[] of {finding, decision: fix / out_of_scope / advisory, note} | `fix` findings go to the builder as one fix round. `out_of_scope` findings become proposed follow-ups. Checks: every blocking finding is ruled on; a blocking finding can't be marked advisory. |
| `escalate` | task (optional), question, suggested_answer, kind (scope, conflict, risky, tests, dispute, ticket_change, other) | Opens a Needs me item linked to the task and blocks only that task. `ticket_change` is for ticket changes beyond R2's allowance; on accept, the answer goes back to the lead, which makes the change. |
| `propose_follow_up` | from_task, title, instruction, reason | Records a proposed task, shown in the chat and the log with a Start button. It isn't started without Frédéric, because it's outside his instruction (call). |
| `remember` | scope (repo or me), repo, paths[] (optional), fact, action, evidence (an event id), replaces (optional), same_as (optional) | Stores a lesson (section 6). Active at once only if the evidence is Frédéric's own words. |
| `propose_onboarding` | repo, backlog filter and order, status mapping, setup/build/test/lint commands, files to copy, definition of done, how to use the task's ports | Opens a confirmation item. On accept, stores the project facts. |
| `update_plan` | text (at most about 300 words) | Stores the lead's plan: Frédéric's current instruction with its end point, and what the lead means to do next. Shown in the Lead view and given to a replacement lead. |
| `log_decision` | task (optional), decision, why | Adds a log entry (R23), for assumptions it made instead of asking, or how it split a ticket. |
| `get_task` | task | The card: instruction, level, agents, timeline, final messages, findings, PR and CI state, lessons given. |
| `agent_output` | task, lines (optional) | The tail of the running agent's output. |
| `lessons` | repo (optional), status (optional) | Lessons and candidates, for `remember`'s `same_as`. |

- **No "request review" tool (call):** Régie starts the review when the builder reports done.
- **No ticket tool:** the lead updates Jira with its own connectors (R2). It moves tickets to "in progress" when it creates their tasks, and to "in review" with a comment linking the PR when it's woken by the PR opening. Régie records those updates from the lead's output for the log.

### What wakes it

| Wakes the lead | Waits for the next wake |
|---|---|
| Frédéric's chat message | run started |
| a builder run ending (its final message: done, or a question) | lane changes, usage readings, slot changes |
| review findings, or a verify-pass result | pushes, CI running or green |
| a PR opened (so it can update the tickets) | a CI or conflict fix started (Régie resumes the builder itself) |
| a loop cap reached (CI, PR feedback, conflict, review) | a dependency merged (Régie starts the dependent task itself) |
| an answered escalation or confirmation | |
| a PR comment or review by a person or bot, or changes requested | |
| a merge, or a PR closed outside Régie | |
| session start (preflight) and resume after pause | |

**Timing:** Frédéric's message wakes an idle lead at once. Other wake events wait 5 seconds to gather events that arrive together. Events that arrive during a turn trigger the next turn as soon as it ends.

**Session start:** the first wake asks the lead to run the preflight (can it read the backlog?) and onboarding for any repo without confirmed project facts.

### When it's replaced (decided)

**Replaced** means Régie stops resuming that conversation and starts a new one for the same session, primed with a brief it builds from its own state. The chat and its history don't change; a marker says "new lead from here". The old conversation stays on disk.

A new lead starts when:
- the conversation passes **200k tokens** (Opus 5.5 has a 1M window here, so this is a cost and continuity setting, not a limit; Régie logs tokens per wake so it can be tuned);
- it's woken after **more than 1 hour idle** (Claude Code's cache lasts 1 hour, so a fresh start from a brief costs less than re-reading the conversation);
- resuming it fails;
- Frédéric presses **Fresh lead**.

**The brief:** project facts and lessons, Frédéric's current instruction and the lead's plan, the board snapshot with each open task's last decision, open Needs me items and proposed follow-ups, the session's recent log entries, and the last 20 or so chat exchanges word for word. It doesn't carry the old lead's private reasoning or what it read; the new lead reads again what it needs.

### Model (decided)

Opus 5.5 at high effort, as a setting with a per-session override (R4).

## 2. Worker levels: model and effort (decided)

- `create_task` takes a level. Settings turn each level into a model and effort for each provider:

  | Level | Claude | Codex |
  |---|---|---|
  | light | Sonnet 5.5, high effort | Codex's main model (`gpt-6.1-sol` today), low |
  | standard (default) | Opus 5.5, medium | Codex's main model, medium |
  | hard | Opus 5.5, high | Codex's main model, high |

- **Why:** the lead has read the ticket and the code, so it judges how hard the task is; model choices stay in settings where reflection (R22) can propose changes. Whether Sonnet at high effort beats Opus at low effort on light tasks is unknown; Régie records tokens, time and fix loops per level and model so reflections can show it.
- The lead can **raise** a task's level mid-task, for example after a failed fix round. The next run uses the new setting.
- **Reviewers** work at the task's level.
- **Provider is Régie's call:** quota and the "review by the other provider" rule decide. The lead may state a preference, honoured when that provider is open.
- Each card shows its level, model and effort.

## 3. Data model

### One log, plus tables kept from it

- **`events`** (append-only): id (increasing integer), time, session, task (optional), actor (Frédéric, the lead, an agent run, or Régie), kind, payload (JSON).
- In the same write transaction, Régie updates tables for quick reads: `sessions`, `session_repos`, `tasks`, `runs`, `reviews` and `findings`, `items`, `lessons`, `lesson_uses`, `settings`, `project_facts`, `lead_conversations`.
- Those tables can be rebuilt from `events` alone, and a test checks that a rebuild equals the live tables.
- Event ids are also the ids on the event stream, so a client that reconnects with its last id gets everything it missed.

**Kept out of the log:**
- **Agents' raw output:** one file per run under `~/.regie/runs/<run id>.jsonl`, streamed live, kept 14 days (call). These files are also how problems with autonomous agents get noticed.
- **Usage readings:** their own table. Only threshold crossings, slot changes and limit hits become events.

### Event kinds

- **Session:** created, repo added, paused, resumed, lead conversation started or replaced, chat message (from Frédéric or the lead), plan updated.
- **Task:** created (with tickets, dependencies, level, paths, provider preference), level raised, follow-up proposed or started, cancelled, retried.
- **Run** (one agent working on one step of a task: builder, reviewer, verify pass): started (role, provider, model, effort, native session or thread id, process id), final message, stopped (finished, paused, stopped by Frédéric, limit hit, crashed, timed out), usage (tokens, run time).
- **Setup:** worktree created, setup run.
- **Review:** findings (with evidence), the lead's rulings, verify-pass results.
- **GitHub** (from polling the task's branch with `gh`): pushed, PR opened, CI changed, comment or review received, changes requested, conflict detected, merged, closed; and worktree cleaned up.
- **Jira:** ticket updates the lead made (recorded from its output).
- **Items:** opened (escalation, confirmation, reflection), answered (by whom, from the queue or the chat, whether the suggestion was accepted), withdrawn.
- **Memory:** lesson created, candidate seen again, edited, replaced, confirmed, retired, deleted; lessons given to a run.
- **Settings and project facts:** changed (old value, new value, who, why), reverted.
- **Usage:** threshold crossed, slots changed, limit hit, window reset.

### The lane

- One pure function in `regie-core`: it takes the task's stored facts and the live facts (is an agent running, PR and CI state, provider state, pause) and returns the lane, the flags and the reason a task is queued, using R9's order of precedence (Merged, Cancelled, Working, In review, CI, Ready to merge, Queued).
- PR and CI state come from `gh` polling. Changes are recorded as events, so the function only reads stored facts plus the process table.
- Covered by many small table-driven tests.

### The Needs me queue

- A query, not a table of its own. It returns:
  - open items: escalations (including ticket changes beyond the allowance), confirmations (onboarding, closing a PR on cancel) and reflection proposals;
  - tasks in Ready to merge;
  - work held back only by a threshold, as one item per session and provider (see section 5).
- **Order:** items that block work first, then oldest first.
- A card in the chat and a row in the queue share the item id, so answering either clears both.

### The log (R23)

- A filtered view of `events`: decisions, lessons, improvements, with R23's filters including "in force now".
- **Versioned values:** lessons, settings, project facts and role instructions. Every change event stores the old and new value. Revert writes one more change that restores the old value, so nothing is lost.

## 4. Agents: autonomy and what each run is told

### Agent autonomy (decided)

- **Agents work like Frédéric's own sessions:** his plugins, skills, hooks and connectors, network access, and no permission prompts. No sandbox profiles or tool allowlists.
- **Start simple; restrict when a problem shows up.** Every run's output is kept (section 3), so problems are visible.
- **What stays, because it's the job:** one worktree per task; nobody but Frédéric merges (agents are told so, and Régie never does); ports that don't collide; cleanup.
- **Accepted risk:** an agent steered by a ticket, a PR comment or a web page could push elsewhere, post through a connector, or touch files outside its worktree. Only its instructions and, for Claude, auto mode's own judgement stand in the way. Branch protection on each default branch is the recommended backstop.

### Claude agents

- `claude -p` in the task's worktree, with `--permission-mode auto`, `--model` and `--effort` from the level. His full setup loads as in his own sessions.
- `--input-format stream-json` and `--output-format stream-json`, so Régie can send a redirect into the run, interrupt it, and read its `rate_limit_event`.
- Checked during the design: in a headless run, auto mode did routine work without asking.

### Codex agents (call)

- Threads of one `codex app-server` that Régie starts and owns. It owns the login, so concurrent token refreshes can't race.
- Each thread: working folder = the worktree; sandbox `workspace-write` **with network on**, and the repo's `.git` folder as an extra writable root so the agent can commit and push from its worktree; never asks for approval. Model and reasoning effort from the level. His Codex plugins stay as configured.
- If that sandbox gets in the way of normal work, the fallback is full access. Solve it when it shows up.
- Redirect uses `turn/steer`, pause uses `turn/interrupt`, restart uses `thread/resume`, and usage comes from `account/rateLimits/read`.

### What each run is told

Built-in prompts, plus the project's role instructions (R22) and lessons (R20).

- **Builder:** the task's instruction, tickets and acceptance; the project's setup, build, test and lint commands; `REGIE_PORT_BASE` and `COMPOSE_PROJECT_NAME`; the definition of done. Build, run the checks, commit on the task's branch with a trailer naming the agent (for example `Regie-Agent: builder, claude, claude-opus-5-5, standard`); don't push until told, because review comes first; never merge. End with a short final message: done, with what changed and the check results, or a question for the lead.
- **Builder, after review closes:** push and open the PR with `gh pr create`: a title from the task, a body with the tickets, a summary and the review outcome.
- **Builder, after a CI failure, conflict or ruled feedback:** fix it, run the checks, commit and push.
- **Reviewer:** review the branch against the instruction and acceptance criteria. Don't edit or commit. End with the findings as a list: title, kind (bug, security, missing test, unmet criterion, design, other), blocking or advisory, and evidence (a command that reproduces it, a failing test, or an unmet acceptance criterion with file and line). A blocking finding without evidence is treated as advisory (R6).
- **Verify pass:** for each earlier finding: fixed, not fixed, or disputed.

**The final message is the report.** Régie passes it to the lead as the run's event; the lead reads it and decides what's next. Findings are parsed from the reviewer's list, which its prompt asks it to give in a fixed format; if Régie can't parse it, the lead gets the text and rules on it as written.

### Régie's own work

- **New worktree:** fetch, create the worktree and branch `regie/<task id>-<slug>` from a fresh `origin/<default branch>`, copy the project's listed untracked files, run the setup command. Doing this before the agent starts saves agent time; agents can still install anything else they need.
- **Ports and local services (call):** each task gets `REGIE_PORT_BASE` (the first of 10 ports reserved for it) and `COMPOSE_PROJECT_NAME=regie-<task id>`. Onboarding works them into the project's commands.
- **Watching:** `gh` polling of each task's branch for its PR, CI checks, reviews, comments, conflicts and merge.
- **A real limit mid-run:** the agent can't continue, so Régie commits the work in progress on the task's branch for the handover (section 5).
- **Cleanup:** after a merge or a cancel, remove the worktree and the local branch. Never delete remote branches.

## 5. Usage and scheduling

### Readings

- **Codex:** `account/rateLimits/read` on the app-server every minute while Codex work runs, every 10 minutes otherwise. No model call.
- **Claude:** the `rate_limit_event` from every running Claude process (`unifiedWindows.five_hour` and `seven_day`, with utilization and reset times). If the latest reading is more than 10 minutes old when Régie is about to start Claude work, it first sends a minimal probe: `claude -p` on Haiku 5.5 at low effort with no tools and no MCP (about 1 second and 5k mostly cached tokens; checked during the design). No probes while idle.
- **Tests and the bench** use a fake usage source that can be set through the API.

### When a provider is open

- **5-hour window:** under 75% used.
- **7-day window:** at or under 20% used, or no more than 10 points ahead of the week's pace. The week's pace is the share of the window elapsed, from its reset time.
- **Blocked:** after a real limit hit, until the window resets.

### Slots (decided: adaptive)

- **Overall:** at most 4 running agents across all sessions, builders and reviewers counted, leads not counted.
- **Per session:** at most 2 tasks with a running agent.
- **Per provider:** worked out from its headroom, so work slows as a window gets tight:

  | Provider's state | Slots |
  |---|---|
  | 5-hour under 50%, and on or behind the week's pace | 2 |
  | 5-hour at 50 to 75%, or up to 10 points ahead of the week's pace | 1 |
  | at a threshold, or blocked | 0 |

- All numbers are settings. The usage panel shows why a provider has its slots, for example "Claude: 1 slot, week 4.5 points ahead".

### What starts next

1. Work already started first (fix rounds, reviews, verify passes, PR fixes), then new tasks, oldest first.
2. A task needs: a free overall slot, a free session slot, a free slot on the chosen provider, the session not paused, and its dependencies merged.
3. **A new task** goes to the lead's preferred provider if it's open, otherwise to the open provider with more headroom.
4. **A started task waits for its own provider** rather than switching, because a switch makes the new agent re-read everything.
5. **A reviewer** goes to the other provider if it's open, otherwise to a fresh agent on the same provider (R6).

### A real limit mid-run

Régie marks the provider blocked until its reset and commits the work in progress on the task's branch. If the other provider is open, the task restarts there from its handover package (the branch, the task's log, and a summary Régie builds from the log and the diff). Otherwise it waits in Queued.

### Run anyway

- One Needs me item per session and provider, for example "2 tasks held: Claude 5h at 78%, resets 19:00", sorted after items that block work, with a button per task. The card has the button too.
- Run anyway skips the thresholds, never the slot caps on overall and per-session agents.

### Pause, resume, restart

- **Pause a session:** Claude agents get an interrupt, then their process ends; their session is kept. Codex turns get `turn/interrupt`. A running lead turn is interrupted. Nothing new starts: no agents and no lead wakes. GitHub polling continues, read-only.
- **Resume:** each agent continues its own session and is told it was paused. The lead is woken with what happened meanwhile.
- **Pause all** ("fade to black") pauses every session.
- **Restart after a crash or sleep (call):** on start, Régie stops any recorded agent process still alive (it can't reattach to its pipes), then resumes each run's native session with a note that it was interrupted. Checked during the design: a killed Claude session resumes with its context; the spike showed the same for Codex.
- **Mac awake:** Régie holds a `caffeinate -i` assertion while any agent runs.

## 6. Memory, self-improvement and the log

### Lessons

- Stored in `lessons`, versioned like settings. Each has: scope (a repo, or "me"), optional paths, fact, action, evidence (event ids), status (candidate, active, retired, deleted), last confirmed date, and the tasks it was seen in.
- **Written only through the lead's `remember`:**
  - **Evidence from Frédéric's words** (a chat message, an escalation answer, a PR comment by his GitHub user): active at once. Régie checks the evidence event's actor.
  - **Evidence from the work** (a blocking finding confirmed and fixed, a CI fix loop, someone else's PR comment): a candidate, never shown to agents.
  - **A second occurrence:** the lead finds open candidates with the `lessons` tool and passes `same_as`. That makes the candidate active. Whether two lessons are the same is judged by a model, not a keyword rule.
  - **`replaces`:** a newer statement from Frédéric replaces an older lesson, and the card says so.
- Agents don't write Régie's memory. (Their own tools, such as Claude's auto-memory, are their business.)

### How lessons reach prompts (R20)

- **The lead:** in its standing context, as in section 1.
- **Builders and reviewers:** Régie picks up to 10 lessons and about 600 tokens: first lessons tied to paths that overlap the task's `paths`, then lessons whose words overlap the instruction (ported from Régie's `knowledge.prime()`), then the most recently confirmed. The task records which lessons it was given, and the card lists them.

### Freshness (R21)

- **Confirmed** when a task the lesson was given to merges with no blocking finding and no correction from Frédéric.
- **A daily job:** retires lessons not confirmed for 90 days; drops candidates after 30 days; retires lessons whose paths no longer exist on the default branch; keeps each repo at 200 active lessons.
- Conflicting lessons where neither is from Frédéric, and lessons given to tasks that still broke twice, are flagged by reflection.

### Reflection (R22)

- Every 10 merged tasks in a project, or on request.
- Régie computes R22's metrics with SQL over the events, for the current and previous period.
- A separate, one-off Opus call (not the session's lead) gets the metrics, the lessons in force and recent findings, and returns up to 3 proposals, each with evidence and the one metric it should move.
- They arrive as one Needs me item, each with accept and reject. An accepted proposal is applied as a versioned change and logged with its metric. The next reflection reports that metric before and after, and says it's a comparison on few tasks, not a proof.
- It can't change R14's rules, the tool's code or the built-in prompts; role instructions are added on top.

### Export

A repo's lessons can be exported to a Markdown file on request.

## 7. The web page

Agreed from mockups (see the mockup file). Built with Preact and TypeScript; it uses only the JSON API and the event stream.

### Layout (decided)

```
┌ RÉGIE  Studio: ABC backend ▾  [Board|Lead•]  ON AIR·3   VU meters   ⏸ Standby  ■ Fade to black ┐
├ Studios          ┬───────────────────────────────────────────────────────────────────────────────┤
│ ABC backend  (2) │  Board view: lanes as columns, full width                                   │
│ Player web   (1) │  QUEUED │ WORKING │ IN REVIEW │ CI │ READY │ MERGED                            │
│ + New studio     │  monitors with tally lights; a click opens the inspector drawer on the right │
│ Talkback · 4     │                                                                              │
│  escalation …    │  — or —                                                                      │
│  confirm …       │  Lead view: the chat, full width, with a narrow column for the instruction, │
│  ready to merge …│  the lead's plan, a mini board of tally chips, and the lead's model,         │
│  held …          │  context size and Fresh lead button                                          │
└──────────────────┴───────────────────────────────────────────────────────────────────────────────┘
```

- **Top bar:** studio (session) selector; the Board / Lead toggle (keys 1 and 2; a dot on Lead when it said something new); the ON AIR sign, lit with the number of running agents; VU meters per provider and window, with the threshold as a red zone and the week's pace as a tick, and each reading's age on hover; Standby (pause this session) and Fade to black (pause all).
- **Left rail, in both views:** studios with their Needs me counts, and the talkback (Needs me) queue pinned underneath, with its call light. Each row has its actions: accept the suggestion, reply, open the PR, confirm, run anyway.
- **Board view:** lanes as columns. A monitor shows its tally light (red on air, green ready, amber needs me, off otherwise), title, agent (provider and level), tickets or PR, and the last line of live output. Needs me monitors sort to the top of their lane.
- **Inspector drawer:** lane and round, builder and reviewer with provider, model and level, tickets, branch, PR, usage (tokens, time), lessons given, the timeline of instructions, final messages, findings, handovers and escalations, live output, and the controls: stop agent, retry, cancel task, raise level, run anyway.
- **Lead view:** the chat full width. Escalations appear as cards with "Accept suggestion"; events as dim lines; proposed follow-ups with a Start button.

### Other pages (call)

- **Log:** a filtered list of entries, each with edit and revert where it applies.
- **Settings:** a plain form for v1. Knobs and faders can come later.
- **Usage panel:** the meters in the top bar, expanding to readings, reading age, slots and why, and reset times.

### Signals that something needs Frédéric

The queue count in the browser tab title, the per-studio counts, and opt-in browser notifications for new items (R18).

### The régie look (R24)

The light touch only: tally lights, VU meters, the ON AIR sign and fade to black. Every light also has a text label; colour is never the only signal. Light and dark modes follow the system; blinking stops when reduced motion is on.

## 8. Repo layout, tests and the bench

### Layout (call)

```
regie/
├── Cargo.toml          workspace
├── crates/
│   ├── regie-core/     pure logic, no I/O: events, the tables built from them, the lane
│   │                   function, scheduling and slots, checks on the lead's commands,
│   │                   lesson picking
│   └── regie/          everything that touches the outside world, plus the `regie` binary:
│       ├── store/      SQLite and migrations
│       ├── agents/     Claude processes, the Codex app-server client, prompts
│       ├── git/        worktrees, setup, cleanup, gh polling
│       ├── usage/      readings and thresholds
│       └── server/     JSON API, event stream, MCP endpoint, the embedded web page
├── prompts/            built-in prompts (lead, builder, reviewer, verify, reflection),
│                       Markdown, compiled into the binary
├── web/                Preact + TypeScript + Vite; API types generated from Rust
└── CLAUDE.md, AGENTS.md   how to build and test, and where things go
```

- `regie-core` has no dependency that does I/O, so the compiler keeps that boundary for the agents.
- **The Python code** stays untouched until the Rust version passes the acceptance scenario, then one commit deletes it (`src/regie`, `tests`, `profiles`, `pyproject.toml` and the rest). Ported: worktree setup and package-manager detection, `prime()`, the stats figures. Rewritten: quota parsing and the adapters. The new binary takes the `regie` name; the old one stays reachable as `uv run regie` meanwhile.

### Tests

- **The core:** many small unit tests (lanes, scheduling, slots, threshold maths, lesson picking, command checks).
- **Everything else:** Claude, Codex and GitHub sit behind interfaces with in-process fakes, so the whole acceptance scenario runs in `cargo test` with no quota and no network. The fake usage source sets thresholds; a defined kill point covers the restart test.
- **Live checks:** a few opt-in tests against the real CLIs on Haiku 5.5, like the probes run during the design.
- **The web page:** TypeScript type checks and a few component tests.

### regie-bench

It starts `regie serve --data-dir <tmp>` with its own database, a fake GitHub and a fake usage source, sends the instruction through the API, waits for Ready to merge, and reads tokens, time and fix loops from the API. Its cases stay in Python.

### Dev loop

Vite's dev server proxies API calls to the running binary. A release build runs the Vite build, then `cargo build`, which embeds the page.

## 9. Build plan

Five milestones (decided: the lead before Codex). Each ends with green tests, a live run on a real repo and a short demo. Each gets its own implementation plan when it starts.

1. **One task, started by hand.** The workspace, store, server with token and origin check, event stream, and the Board view with the inspector. One task's whole path: worktree and setup, a Claude builder that builds, checks and commits, a fresh Claude reviewer with fix rounds and the verify pass, the builder pushing and opening the PR, `gh` polling for CI, Ready to merge, merge detection, cleanup. A simple usage guard: Claude readings, both thresholds, fixed caps. Pause and resume. Restart recovery. Tasks are started through the CLI or the API, and the builder's questions are answered there too.
2. **The lead.** The lead conversation, its MCP tools, the wake rules and replacement. The Lead view, the talkback queue, escalations, browser notifications. Onboarding, preflight and Jira updates. Several sessions. **Daily use starts here, on Claude only.**
3. **Codex.** The app-server client, Codex builders, review by the other provider, Codex usage polling, adaptive slots, run anyway, the handover after a real limit.
4. **Memory and the log.** Lessons, how they reach prompts, the freshness job, reflection, the log page and the settings page.
5. **Done.** The full acceptance scenario with fakes plus one live run, the bench adapter, and the Python code deleted.

## Checks run during the design (2026-11-19)

In a throwaway repo, with Haiku 5.5 unless noted:

| Check | Result |
|---|---|
| Opus 5.5's context window in Claude Code on this subscription | 1M tokens; Claude Code writes 1-hour cache entries |
| `claude --resume` after SIGKILL mid-run | Resumed with its context |
| A minimal Haiku probe | Returns both Claude windows in about 1 second |
| Claude auto mode in a headless run (Sonnet 5.5) | Did the routine work asked of it without prompting, including a write outside the worktree when asked |
| A claude.ai connector in a headless run | A Jira read succeeded |
| Frédéric's active repos (`~/Code`) | Mostly Node with pnpm and Make, several with Docker Compose stacks; some Python with uv |

Also checked, and relevant only if restrictions are added later: Claude's sandbox blocks shell writes outside the worktree and network except allowed domains, but not its own file tools unless they're scoped as `Edit(./**)`; `--setting-sources user` with `--disable-slash-commands` and `disableAllHooks` keeps connectors but drops skills and hooks; Codex on this Mac has plugins on, including GitHub and computer use.

## Checks still to run during the build

These don't change the design; each has a fallback.

- **Claude redirect mid-turn:** whether a user message written to a running `-p` stream-json process is delivered mid-turn. Fallback: interrupt with a control request, then send the message.
- **Codex commits and pushes from a thread:** with network on and the repo's `.git` writable, `git commit`, `git push` and `gh pr create` work from a worktree. Fallback: full access.
- **Claude `rate_limit_event` from the lead and workers:** that every Claude process reports both windows, as the probe does.
- **Auto mode over a long build:** that its judgement doesn't stop routine work (installs, test servers, Docker). Fallback: `bypassPermissions` for builders.
- **Frédéric's skills and hooks in headless runs:** what his plugins do in a `-p` run (for example a skill that asks the user questions before building). Expected outcome: such a question ends the run as a question for the lead, which answers it. Fallback, only if it gets in the way: turn that plugin off for Régie's runs.

## Calls made (for Frédéric to check)

- The lead runs as one `claude -p --resume` turn per wake, not a process waiting idle, with a 10-minute limit per turn.
- No "request review" tool: review starts when the builder reports done.
- The lead moves tickets itself: "in progress" when it creates the task, "in review" with a PR link when the PR opens.
- Proposed follow-ups aren't started without Frédéric, since they're outside his instruction.
- After the PR opens: CI and conflict fixes go straight to the builder; changes requested go through the lead's scope ruling first; no extra agent review for post-PR fixes.
- Codex agents: `workspace-write` with network on and the repo's `.git` writable, falling back to full access if it gets in the way.
- Agents' final messages are their reports; there's no report tool.
- Ports: a block of 10 per task through `REGIE_PORT_BASE`, plus `COMPOSE_PROJECT_NAME`.
- On restart, Régie stops leftover agent processes and resumes their sessions rather than reattaching.
- Agent output files are kept 14 days.
- Log and settings pages are plain for v1.
- Two crates: a pure core and everything else.

## Left to the builder

- The Rust libraries (HTTP server, SQLite access, async runtime, MCP server), the exact SQL schema and migrations, and how the code is split into modules inside `crates/regie`.
- The wording of the built-in prompts, within the rules above, including the reviewer's findings format.
- The exact JSON shapes of commands, queries and events, as long as they're generated into TypeScript from one Rust definition.
- The CLI's subcommands beyond `regie` and `regie serve`.
- The port range used for `REGIE_PORT_BASE`, the branch slug rules, and the PR body layout.
- Visual details of the web page within the agreed layout and R24's guard rails.
