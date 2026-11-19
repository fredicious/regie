# Lead-agent orchestrator: design

**Date:** 2026-11-19
**Status:** approved by Frédéric on 2026-11-19. Next: the implementation plan for milestone 1.

**Inputs:**
- `docs/requirements/2026-11-19-lead-agent-orchestrator.md` (R1 to R24, revision 3). This design doesn't repeat the requirements; it says how to meet them.
- `docs/requirements/2026-11-19-lead-agent-orchestrator-fable-review.md`.
- `docs/spikes/2026-11-19-lead-agent-spike.md`, plus the checks run during the design (see "Checks run during the design").
- The agreed main-screen mockup: `2026-11-19-lead-agent-orchestrator-mockups/main-screen.html`. It's an HTML fragment that uses the brainstorming companion's colour variables, so open it in a browser and expect plain fallbacks.

**How to read it.** "Decided" marks what Frédéric chose in the conversation. "Call" marks what the designer chose and Frédéric can still correct; all calls are listed again in "Calls made". Everything else follows from the requirements.

## Intent

A local tool Frédéric uses every day instead of juggling agents by hand.

- He opens a web dashboard and talks to a **lead agent** for each session.
- The lead takes tickets from his own backlog (Jira, through his Claude connectors), splits them into tasks, and delegates them to sandboxed builders and cross-provider reviewers on his Claude and Codex subscriptions.
- It brings each PR to "ready to merge". He merges.
- Around that: usage-limit thresholds, pause, a single Needs me queue, memory and self-improvement, a log of decisions, and a light régie (TV control room) look.

**Why:** existing tools failed his bar of "open a folder, say start this work". Paperclip needed too much setup; Claude Code with the Codex plugin has no backlog, board or lead; Orchestrator.inc comes closest and is the reference design, but loses requests that need him across sessions and doesn't manage usage limits.

## Goals, non-goals, anti-goals

**Goals (first version):** the acceptance scenario in the requirements passes, and Frédéric uses it daily from milestone 2.

**Non-goals:** everything in the requirements' "Not in the first version": mid-run provider switch at 90%, a lead on Codex, a desktop app or TUI, letting the lead merge, hosts other than GitHub, multi-repo tasks, phone notifications, and memory beyond R19 to R22.

**Anti-goals (a failure even if it works):**
- **Setup ceremony.** Anything to configure before the first session beyond pointing at folders.
- **Requests that need Frédéric getting lost.** Everything waiting on him is in one queue, always visible.
- **Agents with power outside the tool.** No agent can push, merge, write to GitHub or post to Slack, whatever its prompt says. (Code an agent writes still runs with Frédéric's rights when Régie runs the checks; see "How much the protection covers".)
- **Runaway loops and runaway usage.** Every loop is capped; thresholds stop new work before a limit.
- **A theme that costs clarity.**

## Constraints

- macOS, local only, one user. The server listens on 127.0.0.1 and checks a per-launch token and the request origin (R11).
- Agents use Frédéric's own Claude Code and Codex installs: their logins, plugins and connectors (R1). No API keys.
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

**The lead: one long conversation fed with events, replaceable from the tool's state.** The requirements' R3 had the tool rebuild the lead's picture on every call; Orchestrator.inc keeps one conversation. The middle way was chosen: one conversation, but every wake message carries a board snapshot, and all state lives in Régie so a lead can be replaced at any time. This settles R3's open point.

**Choosing workers' model and effort: levels.** Three options were weighed: fixed per role (simple, wastes quota on trivial tasks), the lead picks model and effort freely (ties prompts to model names, drifts to the biggest model), or the lead picks a level and settings turn it into a model and effort. Levels won (see section 2).

**Main screen: two views, never side by side, with lanes as columns.** A wall of monitors was the alternative for the board; showing the chat beside the board left it too narrow. Following Orchestrator.inc, the board and the lead chat are two views of the whole screen.

**Protection: against mistakes, not a hostile agent, for v1.** Sandboxing Régie's own setup and checks too was the alternative; it would have kept Docker-backed tests out of local checks (see section 4).

**Build order: the lead before Codex.** Daily use starts one milestone earlier; until Codex lands, all work runs on Claude. Frédéric has usage resets to spend if needed.

## Architecture

```
 web (Preact/TS) ─┐
 TUI (later)      ├─ localhost + token ─▶ regie (one Rust binary)
 native (later)  ─┤                         ├─ core: commands, queries, event stream (JSON API + SSE)
 lead tools (MCP)─┤                         ├─ store: SQLite ~/.regie/regie.db, append-only event log
 worker report   ─┤                         ├─ scheduler: slots, thresholds, dependencies
 CLI / bench     ─┘                         ├─ usage: Claude rate_limit_event + probe, Codex polling
                                            ├─ git: worktrees, setup, commits, push, PRs, CI via gh
                                            └─ supervisor: leads and Claude agents (`claude -p`, stream-json)
                                                           Codex agents (threads of one `codex app-server`)
```

**The core speaks data, never HTML.** Every action is a typed command, every view is a query, and every change is an event on one stream. The web page, the lead's tools, the workers' report tool, the CLI, the tests and the bench all use this one contract. A lead tool call is the same command a button would send, with the lead as the actor.

**The process.** `regie` starts the server if it isn't running and opens the browser. It runs until Frédéric quits it; no login item is installed. `regie serve --data-dir <dir>` runs an isolated instance (tests, bench).

**Side effects.** Régie does every commit, push, PR, CI poll, cleanup and Jira write. Agents only edit files in their worktree and report.

## Task flow (decided)

```
you: "work on the next 2 tickets of ABC"
  → lead reads Jira, creates tasks ── Queued
  → free slot + provider open + dependencies merged
  → Régie: fetch, worktree + branch, copy listed files, run setup
  → builder (sandboxed) ── Working ──▶ report: done / blocked + question / needs_setup
  → Régie runs build, test, lint ── fail → back to builder (max 3)
  → Régie commits locally
  → reviewer on the other provider ── In review
       → findings to the lead, who rules on scope
       → blocking in-scope finding → fix round (max 2, then a verify-only pass)
  → Régie pushes, opens the PR, moves the ticket to "in review" ── CI
  → CI red / changes requested / conflict → builder fixes (max 3 each)
  → review closed + CI green + no conflict + branch rules met ── Ready to merge → Needs me queue
  → Frédéric merges on GitHub → Merged; cleanup; dependent tasks start
```

### After the PR opens (call)

- **Failed CI or a merge conflict:** goes straight to the builder (Régie first merges the default branch into the task branch outside the sandbox, leaving conflict markers for the builder). The lead hears about it in its next wake.
- **Changes requested in a GitHub review, by a person or a bot:** wakes the lead, which rules on scope as it does for reviewer findings, then sends the in-scope part to the builder with `message_agent`. Nothing goes to the builder without that ruling, so the lead and the builder never both act on the same feedback.
- **Plain comments** (no "changes requested"), from people or bots: wake the lead, which decides whether anything goes to the builder. They don't count towards any cap.
- **Every post-PR fix** then goes through the local checks and a **verify-only pass** by a reviewer, at light level, limited to the fix's diff, before Régie pushes. So no agent-written change reaches the PR unreviewed, at the cost of one small review run per fix.
- **Counting:** these verify passes don't count towards the 2 review rounds. Each post-PR fix, including any rework its verify pass asks for, counts once towards the cap of 3 for its kind (CI, PR feedback, conflict). At the cap, the lead is woken and must escalate.

## 1. The lead

### How it runs (call)

- **One Claude conversation per session.** No lead process waits idle. Each wake is one `claude -p --resume <conversation id>` turn with `--output-format stream-json`. The first turn of a conversation uses `--session-id <uuid>` chosen by Régie.
- **Working folder:** a per-session folder under `~/.regie/sessions/<id>/`, with each of the session's repos added through `--add-dir` so the lead can read them.
- **Tools:** Régie's MCP endpoint is passed with `--mcp-config`, with a token that scopes it to this session. `--strict-mcp-config` is **not** used, so Frédéric's connectors (Jira) stay available.
- **Permissions:** `--permission-mode dontAsk`. Allowed: Régie's tools, `Read`, `Grep`, `Glob`, web search, and the Jira read tools. Hidden with `--disallowedTools`: `Bash`, file edits, web fetch, subagents (`Agent`/`Task`), and every other MCP tool (see section 4 for how the list is built).
- **What it inherits from Frédéric's setup:** the same rule as every Régie Claude run (section 4): his user settings with its logins and connectors, but no hooks, no skills or commands, and no settings files from inside the repos.
- **Text it writes** goes to the session chat. Text from turns woken only by events is shown in a compact, dimmer style. Its prompt asks it to stay brief after event batches.
- **One turn at a time per session.** A turn has a 10-minute wall-clock limit (call); past it, Régie interrupts it and logs that.
- **Pause** means no wakes; a running lead turn is interrupted.
- **Usage:** thresholds don't hold the lead back. A real Claude limit makes wakes wait until the window resets.

### What it sees when woken

**Standing context** (appended system prompt, kept unchanged for the life of one conversation so the cache holds):
- its role and rules: it plans, delegates, escalates and never edits code; the must-escalate list from R3; how to use its tools; the levels and when to use each;
- the session's repos and their project facts (R15);
- up to 20 lessons (R20): Frédéric's own ("me") lessons and the session's repo lessons, most recently confirmed first;
- its current plan.

Later changes to facts, lessons or the plan reach it as events.

**Each wake message** has three parts, in this order:
1. Frédéric's chat messages since the last turn, in full.
2. The events since the last turn, one line each, oldest first. For example: `14:02 T3 report: done — "Added retry with backoff" (3 files)`. Agent reports and review findings are included in full; other details stay behind the read tools.
3. A board snapshot: every task not merged or cancelled, one line each (id, title, lane, flags, level, agent), the open Needs me items, and one line of usage per provider.

The snapshot means the lead never has to rebuild the board from memory of past events.

### Tools

Every tool call is a command. Régie checks it and either applies it or returns an error with the reason, which the lead can correct.

| Tool | Input | What Régie does, and checks |
|---|---|---|
| `create_task` | repo, title, instruction, tickets[], blocked_by[], level (light, standard, hard; default standard), paths[] (expected areas, optional), provider (preference, optional) | Creates the task in Queued and returns its id. Checks: the repo is in the session, is a git repo with a GitHub remote, and has confirmed onboarding; `blocked_by` tasks exist in the session; no dependency cycle. |
| `message_agent` | task, text, level (optional, only higher) | If the task's builder is running, delivers the text into the run (redirect). Otherwise starts the task's next builder run, resuming the builder's session with this text. Checks: task not merged or cancelled; level only goes up. |
| `rule_findings` | task, review, rulings[] of {finding, decision: fix / out_of_scope / advisory, note} | `fix` findings go to the builder as one fix round. `out_of_scope` findings become proposed follow-ups. Checks: every blocking finding is ruled on; a blocking finding can't be marked advisory. |
| `escalate` | task (optional), question, suggested_answer, kind (scope, conflict, risky, tests, dispute, other) | Opens a Needs me item linked to the task and blocks only that task. |
| `propose_follow_up` | from_task, title, instruction, reason | Records a proposed task, shown in the chat and the log with a Start button. It isn't started without Frédéric, because it's outside his instruction (call). |
| `update_ticket` | ticket, comment (optional), status (optional) | Queues a Jira write. A status other than the mapped "in progress" or "in review" becomes a confirmation item in the Needs me queue instead (R2). |
| `remember` | scope (repo or me), repo, paths[] (optional), fact, action, evidence (an event id), replaces (optional), same_as (optional) | Stores a lesson (section 6). Active at once only if the evidence is Frédéric's own words. |
| `propose_onboarding` | repo, backlog filter and order, status mapping, setup/build/test/lint commands, files to copy, definition of done, how to use the task's ports | Opens a confirmation item. On accept, stores the project facts. |
| `update_plan` | text (at most about 300 words) | Stores the lead's plan: Frédéric's current instruction with its end point, and what the lead means to do next. Shown in the Lead view and given to a replacement lead. |
| `log_decision` | task (optional), decision, why | Adds a log entry (R23), for assumptions it made instead of asking, or how it split a ticket. |
| `get_task` | task | The card: instruction, level, agents, timeline, last report, findings, PR and CI state, lessons given. |
| `agent_output` | task, lines (optional) | The tail of the running agent's output. |
| `lessons` | repo (optional), status (optional) | Lessons and candidates, for `remember`'s `same_as`. |

Review is requested by Régie automatically once checks pass, so there's no "request review" tool (call). Mapped Jira transitions are automatic too (see "Jira writes").

### What wakes it

| Wakes the lead | Waits for the next wake |
|---|---|
| Frédéric's chat message | run started, checks passed or failed (the fix loop is automatic) |
| an agent's report: done, or blocked with a question | local commit, push, PR opened, CI running or green |
| review findings, or a verify pass result | lane changes, usage readings, slot changes |
| a loop cap reached (local checks, CI, PR feedback, review) | a dependency merged (Régie starts the dependent task itself) |
| an answered escalation or confirmation | automatic Jira transitions |
| a PR comment or review by a person, or changes requested | |
| a merge, or a PR closed outside Régie | |
| a Jira write that failed | |
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

### Jira writes (call)

- A one-off `claude -p` call that can see only `getJiraIssue`, `addOrEditJiraIssueComment` and `transitionJiraIssue`, running on Haiku 5.5 (a setting).
- **Automatic:** when a task's first builder run starts, Régie moves its tickets to the mapped "in progress" status; when its PR opens, to "in review", with a comment linking the PR. The lead adds other comments through `update_ticket`.
- While Claude is blocked by a limit, Jira writes wait in a queue and the rest of the work carries on (R2).

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
- **Agents' raw output:** one file per run under `~/.regie/runs/<run id>.jsonl`, streamed live, kept 14 days (call).
- **Usage readings:** their own table. Only threshold crossings, slot changes and limit hits become events.

### Event kinds

- **Session:** created, repo added, paused, resumed, lead conversation started or replaced, chat message (from Frédéric or the lead), plan updated.
- **Task:** created (with tickets, dependencies, level, paths, provider preference), level raised, follow-up proposed or started, cancelled, retried.
- **Run** (one agent working on one step of a task: builder, reviewer, verify pass): started (role, provider, model, effort, native session or thread id, process id), report, stopped (finished, paused, stopped by Frédéric, limit hit, crashed, timed out), usage (tokens, run time).
- **Setup and checks:** worktree created, setup run, check results (with a pointer to the output).
- **Review:** findings (with evidence), the lead's rulings, verify-pass results.
- **Git and GitHub:** local commit, pushed, PR opened, CI changed, comment or review received, changes requested, conflict detected, merged, closed, worktree cleaned up.
- **Jira:** write requested, done, failed.
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
  - open items: escalations, confirmations (onboarding, ticket changes beyond the allowance, closing a PR on cancel) and reflection proposals;
  - tasks in Ready to merge;
  - work held back only by a threshold, as one item per session and provider (see section 5).
- **Order:** items that block work first, then oldest first.
- A card in the chat and a row in the queue share the item id, so answering either clears both.

### The log (R23)

- A filtered view of `events`: decisions, lessons, improvements, with R23's filters including "in force now".
- **Versioned values:** lessons, settings, project facts and role instructions. Every change event stores the old and new value. Revert writes one more change that restores the old value, so nothing is lost.

## 4. Agent profiles and sandboxing

### Claude workers (call)

- `claude -p` in the task's worktree, with `--input-format stream-json` and `--output-format stream-json`, so Régie can send a redirect into the run or interrupt it.
- `--permission-mode dontAsk`, `--model` and `--effort` from the level.
- **Sandbox** through `--settings`: `sandbox.enabled: true`, `autoAllowBashIfSandboxed: true`, `network.allowedDomains: []`, `network.allowLocalBinding: true` (for test servers).
- **Allowed tools (builder):** `Read`, `Grep`, `Glob`, `Edit(./**)` (this one rule covers Write, Edit and the other file tools, scoped to the worktree), `Bash`, web search, the connector read tools, and Régie's `report` tool.
- **No web fetch (call):** it runs in the Claude process, outside the sandbox, so it could send what the agent has read (a `.env`, say) to any host.
- **Reviewer and verify pass:** the same without `Edit`.
- **Hidden tools:** Régie reads the agent's tool list once (from the `init` event) and passes every MCP tool that isn't on the allowlist to `--disallowedTools`. So connector write tools are never visible, whatever plugins are installed.
- Régie's MCP endpoint comes through `--mcp-config`, with a token scoped to the run.
- **Package-manager caches (call):** the sandbox may write to the cache folders of the package managers detected at onboarding (for example the pnpm store or `~/.cache/uv`), and agents run with those tools' offline settings, since they have no network.

### What every Régie Claude run inherits (call)

Applies to the lead, builders, reviewers, verify passes, the Jira writer, reflection and the usage probe.

- `--setting-sources user`: Frédéric's user settings, which carry his logins and the claude.ai connectors (Jira). Settings files inside repos (`.claude/settings.json`, `.claude/settings.local.json`) are never loaded, because an agent could edit them in its worktree to add permissions or hooks for its next run.
- `--settings` with `disableAllHooks: true`: no hooks from his plugins (superpowers, the Codex plugin's review gate, and the rest) run around agent turns, outside the sandbox.
- `--disable-slash-commands`: no skills or commands, so skills written for interactive use (such as brainstorming, which asks the user questions) never steer a headless agent. Régie's built-in prompts carry the working discipline instead.
- Every MCP tool not on the role's allowlist is hidden, as above.
- His user-level `CLAUDE.md` and the repo's `CLAUDE.md` still load.
- His organisation's managed plugins still load; Régie can't turn those off.

### Codex workers (call)

- Threads of one `codex app-server` that Régie starts and owns. It owns the login, so concurrent token refreshes can't race.
- Each thread: working folder = the worktree, sandbox `workspace-write` with the worktree as the only writable root, no network, never asks for approval. Model and reasoning effort from the level.
- **All Codex plugins are turned off** for this app-server, and its `notify` hook too. Frédéric's Codex config enables plugins including GitHub (which could write) and computer use (which runs outside the sandbox and could drive his Mac). Codex agents need none of them: only Claude reaches Jira.
- Régie's MCP endpoint is configured for the report tool.
- Redirect uses `turn/steer`, pause uses `turn/interrupt`, restart uses `thread/resume`, and usage comes from `account/rateLimits/read`.

### Reports

- **Every run ends by calling `report`.** If a run ends without one, Régie asks once in the same session; with no report after that, the run counts as blocked.
- **Builder:** status (done, blocked, needs_setup), summary, question (when blocked), contested findings (in a fix round: findings it believes are wrong, and why).
- **Reviewer:** findings, each with a title, kind (bug, security, missing test, unmet criterion, design, other), blocking or advisory, and evidence: a command that reproduces it, a failing test, or an unmet acceptance criterion with file and line. A finding marked blocking without evidence is stored as advisory (R6).
- **Verify pass:** for each earlier finding, fixed, not fixed, or disputed.

### How much the protection covers (decided)

- **First version: guard against mistakes, not against an agent that's been turned hostile.**
- **The agents themselves are sandboxed** as above, and can't push, write to GitHub, post through connectors, or change their own Claude settings.
- **Régie runs setup and checks outside the sandbox,** with Frédéric's rights and the commands he confirmed at onboarding, the way he would by hand. Docker is available to those commands, so tests backed by Docker Compose run locally.
- **The accepted risk:** those commands run code the agent wrote (tests) or added (dependencies). Text in a ticket, a PR comment or a web page could steer an agent into writing something that does harm when Régie runs it.
- **The alternative, not taken for v1:** sandbox setup and checks too (no network, writes only in the worktree). That rules out Docker in local checks, so Compose-backed tests would run only in CI.

### Régie's own work, outside the sandbox

- **New worktree:** fetch, create the worktree and branch `regie/<task id>-<slug>` from a fresh `origin/<default branch>`, copy the project's listed untracked files, run the setup command.
- **Commits:** after each builder run, Régie commits locally with Frédéric's git identity and a trailer naming the agent, for example `Regie-Agent: builder, claude, claude-opus-5-5, standard` (decided: his identity plus a trailer).
- **Before a review:** the builder's work is committed. **After it,** Régie restores the worktree to that commit (`git reset --hard` and `git clean -fd`, keeping ignored files such as installed dependencies), so anything the reviewer's commands wrote is thrown away.
- **Pushes and PRs** happen once review closes. The PR body has the tickets, the builder's summary and the review outcome.
- **A new dependency mid-run (call):** agents have no network, so no package registry (a registry is also where `npm publish` goes). The builder edits the manifest and reports `needs_setup`; Régie runs the setup command and resumes the builder. A per-project setting can open registry domains to Claude agents.
- **Ports and local services (call):** each task gets `REGIE_PORT_BASE` (the first of 10 ports reserved for it) and `COMPOSE_PROJECT_NAME=regie-<task id>`. Onboarding works them into the project's commands.
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

- **Overall:** at most 4 running agents across all sessions, builders and reviewers counted, leads and Jira writes not counted.
- **Per session:** at most 2 tasks with a running agent.
- **Per provider:** worked out from its headroom, so work slows as a window gets tight:

  | Provider's state | Slots |
  |---|---|
  | 5-hour under 50%, and on or behind the week's pace | 2 |
  | 5-hour at 50 to 75%, or up to 10 points ahead of the week's pace | 1 |
  | at a threshold, or blocked | 0 |

- All numbers are settings. The usage panel shows why a provider has its slots, for example "Claude: 1 slot, week 4.5 points ahead".

### What starts next

1. Work already started first (fix rounds, reviews, verify passes), then new tasks, oldest first.
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

- **Pause a session:** Claude workers get an interrupt, then their process ends; their session is kept. Codex turns get `turn/interrupt`. A running lead turn is interrupted. Nothing new starts: no agents, no lead wakes, no pushes, no Jira writes. GitHub polling continues, read-only.
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
- Agents never write memory.

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
- A separate, one-off, read-only Opus call (not the session's lead) gets the metrics, the lessons in force and recent findings, and returns up to 3 proposals, each with evidence and the one metric it should move.
- They arrive as one Needs me item, each with accept and reject. An accepted proposal is applied as a versioned change and logged with its metric. The next reflection reports that metric before and after, and says it's a comparison on few tasks, not a proof.
- It can't touch permissions, the tool's code or the built-in prompts; role instructions are added on top.

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
- **Inspector drawer:** lane and round, builder and reviewer with provider, model and level, tickets, branch, PR, usage (tokens, time), lessons given, the timeline of instructions, reports, findings, handovers and escalations, live output, and the controls: stop agent, retry, cancel task, raise level, run anyway.
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
│       ├── agents/     Claude processes, the Codex app-server client, profiles
│       ├── git/        worktrees, setup, commits, gh
│       ├── usage/      readings and thresholds
│       └── server/     JSON API, event stream, MCP endpoint, the embedded web page
├── prompts/            built-in prompts (lead, builder, reviewer, verify, Jira writer,
│                       reflection), Markdown, compiled into the binary
├── web/                Preact + TypeScript + Vite; API types generated from Rust
└── CLAUDE.md, AGENTS.md   how to build and test, and where things go
```

- `regie-core` has no dependency that does I/O, so the compiler keeps that boundary for the agents.
- **The Python code** stays untouched until the Rust version passes the acceptance scenario, then one commit deletes it (`src/regie`, `tests`, `profiles`, `pyproject.toml` and the rest). Ported: worktree setup and package-manager detection, the gates, `prime()`, the stats figures. Rewritten: quota parsing and the adapters. The new binary takes the `regie` name; the old one stays reachable as `uv run regie` meanwhile.

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

1. **One task, started by hand.** The workspace, store, server with token and origin check, event stream, and the Board view with the inspector. One task's whole path: worktree and setup, a sandboxed Claude builder, checks, a fresh Claude reviewer with fix rounds and the verify pass, local commits, push, PR, CI polling, Ready to merge, merge detection, cleanup. A simple usage guard: Claude readings, both thresholds, fixed caps. Pause and resume. Restart recovery. Tasks are started through the CLI or the API.
2. **The lead.** The lead conversation, its MCP tools, the wake rules and replacement. The Lead view, the talkback queue, escalations, browser notifications. The Jira writer, onboarding and preflight. Several sessions. **Daily use starts here, on Claude only.**
3. **Codex.** The app-server client, Codex builders, review by the other provider, Codex usage polling, adaptive slots, run anyway, the handover after a real limit.
4. **Memory and the log.** Lessons, how they reach prompts, the freshness job, reflection, the log page and the settings page.
5. **Done.** The full acceptance scenario with fakes plus one live run, the bench adapter, and the Python code deleted.

## Checks run during the design (2026-11-19)

In a throwaway repo, with Haiku 5.5 unless noted:

| Check | Result |
|---|---|
| Opus 5.5's context window in Claude Code on this subscription | 1M tokens; Claude Code writes 1-hour cache entries |
| Claude sandbox: shell write outside the worktree (in the home folder) | Blocked ("Operation not permitted"). Writes to the temp folder are allowed by default |
| Claude sandbox: network | A registry on `allowedDomains` reached; any other host blocked |
| Claude `Write` tool allowed without a path rule | Wrote a file in the home folder: **file tools are not covered by the sandbox** |
| Claude file tools allowed as `Edit(./**)` | Writes inside the worktree allowed; outside refused |
| A claude.ai connector inside the sandbox | A Jira read succeeded |
| `claude --resume` after SIGKILL mid-run | Resumed with its context |
| A minimal Haiku probe | Returns both Claude windows in about 1 second |
| Codex config on this Mac | Plugins on, including GitHub and computer use |
| Claude with `--setting-sources project,local` | No user plugins, skills or hooks, but also **no claude.ai connectors** |
| Claude with `--setting-sources user`, `--disable-slash-commands`, `disableAllHooks` | Jira connector tools present, no skills or commands, no hook events |
| Frédéric's user Claude settings | No permission rules and no hooks of their own; plugins enabled: superpowers, code-review, code-simplifier, slack, chrome-devtools-mcp, codex |
| Frédéric's active repos (`~/Code`) | Mostly Node with pnpm and Make, several with Docker Compose stacks; some Python with uv |

## Checks still to run during the build

These don't change the design; each has a fallback.

- **Claude redirect mid-turn:** whether a user message written to a running `-p` stream-json process is delivered mid-turn. Fallback: interrupt with a control request, then send the message.
- **Codex plugins:** the reliable way to turn every plugin off for the app-server Régie starts (`-c` overrides per plugin, or another config switch), without touching `~/.codex/config.toml` or the login.
- **Codex and Régie's MCP endpoint:** that a thread without network can call a localhost MCP server (the codex process makes the call, outside the sandbox). Fallback: the report as a structured final message (`outputSchema`).
- **Codex local ports:** whether `workspace-write` lets test servers bind local ports.
- **Claude `rate_limit_event` from the lead and workers:** that every Claude process reports both windows, as the probe does.
- **`CLAUDE.md` under `--setting-sources user`:** that the repo's `CLAUDE.md` still loads. Fallback: Régie includes it in the first message.
- **Package managers offline in the sandbox:** that `pnpm` and `uv` test runs work with their cache folders writable and no network. Fallback: Régie points their caches inside the worktree.

## Calls made (for Frédéric to check)

- The lead runs as one `claude -p --resume` turn per wake, not a process waiting idle.
- The lead's turn limit is 10 minutes.
- No "request review" tool: review starts automatically once checks pass.
- Mapped Jira transitions (in progress, in review, plus a comment with the PR link) are made by Régie automatically; the lead adds other comments.
- The Jira writer runs on Haiku 5.5.
- Proposed follow-ups aren't started without Frédéric, since they're outside his instruction.
- Local checks failing also loop back to the builder at most 3 times, like CI.
- Agents have no network by default; new dependencies go through `needs_setup`. A per-project setting can open registries to Claude agents.
- Codex agents run with all plugins off.
- The reviewer works in the task's worktree, which Régie restores after the review.
- Régie commits after every builder run and pushes when review closes.
- Ports: a block of 10 per task through `REGIE_PORT_BASE`, plus `COMPOSE_PROJECT_NAME`.
- On restart, Régie stops leftover agent processes and resumes their sessions rather than reattaching.
- Agent output files are kept 14 days.
- Log and settings pages are plain for v1.
- Two crates: a pure core and everything else.
- After the PR opens: CI and conflict fixes go straight to the builder; changes requested go through the lead's scope ruling first; every post-PR fix gets the checks and a light verify-only pass before it's pushed.
- Every Régie Claude run loads only Frédéric's user settings (logins and connectors), with hooks, skills and commands off, and never settings files from inside repos.
- No web fetch for agents or the lead; web search stays.
- Agents may write to the package managers' cache folders, and run offline.

## Left to the builder

- The Rust libraries (HTTP server, SQLite access, async runtime, MCP server), the exact SQL schema and migrations, and how the code is split into modules inside `crates/regie`.
- The wording of the built-in prompts, within the rules above.
- The exact JSON shapes of commands, queries and events, as long as they're generated into TypeScript from one Rust definition.
- The CLI's subcommands beyond `regie` and `regie serve`.
- The port range used for `REGIE_PORT_BASE`, the branch slug rules, and the PR body layout.
- Visual details of the web page within the agreed layout and R24's guard rails.
