# Review of the lead-agent orchestrator requirements (Fable 5.2)

**Reviewed:** `2026-11-19-lead-agent-orchestrator.md`, the version revised after the first review.
**Reviewer:** Claude Fable 5.2, reading on its own without the earlier review.

## Verification notes

Added afterwards by Opus 5.5, checked on this Mac:

- **F4, Claude usage payload:** confirmed. `rate_limit_event` carries `unifiedWindows.five_hour.utilization` and `seven_day.utilization` (0.11 and 0.63 on 2026-11-19). The review is right that it only arrives while a Claude call is running. No separate weekly limit for the top model appeared in that payload, but one may show up only when it applies.
- **F7, headless Codex resume:** confirmed. `codex exec resume [SESSION_ID] [PROMPT]` exists in Codex 0.170.0.
- **F13, Régie quotes:** partly confirmed. `docs/ARCHITECTURE.md` line 8 says the UI "never becomes a second orchestration authority". The phrase "cannot dispatch agents" wasn't found in the README, ARCHITECTURE.md or `src/`.

## 1. Findings, most important first

**F1. The lead's runtime model is missing (R3, R5, R12, R13).**
- Problem: the doc never says what the lead is. Two readings are possible: a long-lived conversation, or a stateless agent the tool invokes per event with state rebuilt from storage. It also doesn't say how the lead acts (create a task, assign, escalate, comment on Jira) or what wakes it when I'm not typing.
- Why it matters: R12 and the lead handover in R5 only work with the second reading, and "autonomous" only works if events wake the lead. Two engineers would build two different products.
- Fix: add a requirement along these lines.
  - The lead is invoked per event: my chat message, an agent finishing, a CI or PR change, a quota change, an answered escalation.
  - It acts only through a small command set the tool exposes: create task, assign, request review, escalate, propose follow-up, queue a ticket update.
  - The tool validates and executes those commands; the lead has no other side effects.

**F2. R14 can't be enforced by the CLIs as written (R14, R1, R2).**
- Problem: headless agents need a non-interactive permission mode, and they run with my `gh` login and every plugin on the machine. "Never force-push, never merge, never touch other repos, no other ticket change" is then only a prompt.
- Known:
  - Claude Code has permission modes and allow/deny tool lists, including for MCP tools.
  - Codex's workspace-write sandbox blocks network by default, so dependency installs and pushes fail.
  - A worktree's git metadata lives in the main repo's `.git/worktrees`, outside the worktree. A sandboxed agent often can't commit there, and writing there contradicts "my own checkouts are never touched".
- Gap: the same inherited setup includes Slack, Confluence and other write-capable connectors. R14 says nothing about them, so a sub-agent could post to Slack on day one.
- Fix:
  - The tool owns commit, push, PR creation and ticket writes, as Régie already does ("orchestrator-owned git").
  - Sub-agents get no push and no `gh` write.
  - Connector write tools are denied by default, with an allowlist for Jira comment and transition.
  - Say that "never" means enforced by the tool, and recommend branch protection on the default branch as a backstop.

**F3. "Status is never stored" contradicts the lanes (R9, R6, R3).**
- Problem: several lanes and flags can't be derived from agent activity, PR state and quota:
  - Queued, where there is no PR and no agent;
  - "review done" and the 2-round count;
  - Needs me;
  - Working versus In review before a PR exists.
- Further ambiguity:
  - Is adversarial review before or after the PR opens? Scenario steps 3 and 6 suggest before; "PR waiting for reviews" in the In review lane suggests after.
  - Does Ready to merge require a GitHub approval when branch protection demands one?
  - Which lane wins when checks are running and review is pending at once?
- Fix:
  - Reword to "the lane is never stored; it is computed from stored facts (task events, review rounds, open escalations) plus live facts (process, PR, quota)".
  - Add a precedence table.
  - State the order, which I'd make: build, local tests, adversarial review, PR, CI, ready.
  - State that human GitHub reviews are optional unless the repo requires them.

**F4. Proactive handover is the riskiest and least specified part (R5, R10).**
- Known: Codex exposes rate limits through its app-server without a model call.
- Assumed: the doc says `rate_limit_event` carries percentages for both windows on Claude. I can't confirm the payload. It arrives only as a by-product of a running Claude call, so the reading is stale when nothing runs, and my interactive use moves it unseen.
- Known as of my information: Claude plans also had a separate weekly limit for the top model. The pacing rule doesn't say which weekly number it uses.
- Unspecified:
  - How a running agent is interrupted at 90%, and who commits its work in progress.
  - What happens when the other provider is itself above 75%.
  - What happens at 100%.
  - How "hand over at 90%" squares with "in-flight work finishes" when both are blocked.
- Lead handover defeats R2: Jira is reachable from Claude only, so a lead on Codex can't read the backlog.
- Cost: a mid-run handover makes the new agent re-read everything, which can burn more quota than letting the run finish.
- Fix for v1:
  - At the threshold, start no new work on that provider.
  - Let running agents finish.
  - On a real limit error, park the task and restart it from branch plus log on the other provider, or after the reset.
  - Keep the lead on Claude.
  - Defer 90% mid-run handover and lead handover.
  - Add a manual "run anyway" override.

**F5. The task model breaks for multi-repo and dependent tasks (Terms, R8, R9).**
- Problem: "one task is one worktree" and "one PR per repo it touches" can't both hold; a multi-repo task needs one worktree per repo.
- Problem: the scenario splits one ticket into two tasks. Split tasks usually depend on each other, but only I merge, so task B can't build on task A. Stacked branches, waiting, and merge conflicts between parallel tasks aren't mentioned.
- Unspecified: the base branch, whether the tool fetches first, and whether "folders" must be git repos with a GitHub remote.
- Fix:
  - v1: a task touches one repo; a session can still cover several.
  - Tasks are independent, or explicitly "blocked by task X", waiting in Queued until X merges.
  - Base is a freshly fetched `origin/<default>`.
  - Non-GitHub folders are read-only context.

**F6. The review rules are ambiguous and partly untestable (R6, R9).**
- "Round" is undefined. If round 2 raises a blocking finding and the builder fixes it, nobody verifies the fix.
- "Disagree" is undefined.
- "Whenever its remaining usage allows" names no threshold.
- "Must come with a reproducing command" makes missing tests, unmet acceptance criteria, and security or design defects advisory. That is probably not intended.
- The reviewer needs to run commands in the worktree; the doc doesn't say where or with what rights.
- Nothing bounds the other loops: CI-fix attempts, or change requests from humans or bots on the PR. "Tests can't be made to pass" therefore has no trigger.
- Fix:
  - A round is one review plus one fix.
  - After round 2, the reviewer does a verify-only pass limited to the earlier findings.
  - A blocking finding needs a command, or a failing test, or a cited acceptance criterion with file and line.
  - Cap CI-fix and PR-feedback loops (for example 3), then escalate.
  - Cross-provider review applies when the other provider is under its "no new work" threshold.

**F7. Resume ignores native session resume and laptop sleep (R13, R5).**
- Known: `claude --resume <session-id>` exists. Assumed: `codex exec resume` works equivalently headless.
- Problem: restarting from a handover package on the same provider throws away context and quota for no reason.
- Unspecified: what happens to child agents when the tool crashes (orphans still editing worktrees), and what "picks up" means after sleep kills a stream mid-turn.
- Fix:
  - Resume on the same provider by session id first; use the package only across providers or when resume fails.
  - On start, reconcile recorded process ids and kill or adopt orphans.
  - Hold a `caffeinate` assertion while agents run.

**F8. Concurrency terms are undefined (R10).**
- Problem: "tasks in progress" could include tasks sitting in CI, in Ready to merge, or waiting on me. If it does, a session stalls after two PRs until I merge.
- Problem: with one session, the cap of 4 agents is unreachable, because 2 tasks have one agent each at a time.
- Fix: count only tasks with a running agent. State whether the per-session cap of 2 is intended to bind in v1.

**F9. Escalation mechanics are loose (R3, R7).**
- "Whenever it's unsure" is untestable; only the must-escalate list is.
- Unspecified: whether an escalation blocks the task, the session, or nothing; and how my free-text chat answer is matched when three are pending.
- Fix: an escalation is a stored item linked to a card, with a suggested answer and an "accept suggestion" control. It blocks only its task. The chat shows it; a reply is bound to it explicitly.

**F10. Backlog rules are under-specified (R2, R15).**
- "Next" tickets has no meaning until a filter and order exist. First use on a project needs a short onboarding (filter, build and test commands, definition of done) that the scenario skips.
- Status names like "in review" vary per Jira workflow.
- An open-ended "work the backlog" has no stop condition.
- The Atlassian connector's sign-in can expire in headless runs (assumed), and there is no stated behaviour for that.
- Fix:
  - Add first-run onboarding to the scenario, and store the status mapping in project memory.
  - Require every instruction to carry a bound (N tickets, or until told to stop).
  - Run a preflight "can I reach the backlog" check that escalates when it fails.

**F11. Missing requirements a daily user hits in week one.**
- Stop, cancel or retry a task; pause a session; kill an agent.
- Cleanup of worktrees and local branches after merge or cancel. R14's "never delete branches" forbids it today.
- Untracked files worktrees don't get: `.env`, local config.
- Port and database collisions between parallel test runs.
- Local test, lint and build gates before review. Régie has these; this doc drops them unless a definition of done adds them.
- Who chooses model and effort; R7 displays them but nothing sets them.
- The lead's context growing over a day.
- Localhost hardening: bind to 127.0.0.1 and check origin or a token, since the server can spawn agents with my credentials.
- A minimal signal that something needs me. External notifications are deferred, but a count in the tab title costs nothing.

**F12. The acceptance scenario has gaps.**
- Steps 5 and 7 can't be run on demand. Fix: require an injectable usage source so a threshold can be simulated, and a defined kill point.
- Not exercised: a real limit hit, both providers blocked, Jira queueing while Claude is blocked, the 2-round cap, a CI failure looping back, a merge conflict, cancel, a second session, a multi-repo task, and R14's "never" list.
- Step 2 assumes a split with no dependency (see F5).

**F13. The "Régie today" row overstates the fit.**
- Régie's architecture is a deterministic state machine. Its docs say an agent "cannot dispatch agents" and that the UI "never becomes a second orchestration authority". A delegating lead inverts that.
- "Already has R13" means a manual `regie resume`, not automatic pick-up.
- Fix: list what is reused (provider adapters, quota tracking, git and worktree handling, gates, the run ledger) and what is replaced (the pipeline and state machine, the TUI).

**F14. Smaller points.**
- R7 "usage per task": define it as tokens or run time. A percentage of a window isn't attributable per task.
- R4 "no API keys" on headless subscription use: it works today (known). Whether it stays permitted under provider terms is a risk I can't assess.
- Concurrent headless runs on one login: common for Claude (known). A token-refresh race on `~/.codex/auth.json` is plausible (assumed) and worth a spike.

## 2. Decisions to make before design

1. **What is the lead, and who performs side effects?** Recommend an event-driven lead that emits commands, with the tool owning git, `gh` and ticket writes. This settles F1 and F2 together and keeps Régie's strongest property.
2. **Usage policy for v1.** Recommend: stop new work at the threshold, let runs finish, restart parked tasks after a real limit hit, keep the lead on Claude. Defer 90% handover and lead handover.
3. **Review placement and the meaning of Ready to merge.** Recommend local gates and adversarial review before the PR opens. Ready means review closed, CI green, no conflict, plus whatever branch protection requires.
4. **First-version scope.** Recommend one session, single-repo tasks, independent or blocked-by tasks, no per-task usage, no multi-session switching. Add cancel and cleanup instead.
5. **Extend Régie or start a new core beside it.** Recommend a new small core that imports Régie's adapters, quota, git and gates. Bending the existing pipeline into a lead-driven model will cost more than it saves.

## 3. Verdict

Not ready to design from yet, but close. The goal, the terms and the constraints section are concrete. The blockers are the unspecified lead (F1), permissions that the CLIs can't enforce (F2), a status rule that contradicts its own lanes (F3), a handover scheme that is the costliest part of the doc and resting on one unverified usage signal (F4), and a task model that fails on the scenario's own "split one ticket" step (F5). Making the five decisions above and cutting proactive handover, lead handover, multi-repo tasks and multi-session would leave a first version that is small and testable. Before committing to R5 and R13, a half-day spike should confirm three things: the Claude usage event payload, headless Codex resume, and concurrent runs on one Codex login.
