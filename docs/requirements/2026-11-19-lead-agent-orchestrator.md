# Lead-agent orchestrator: requirements

**Date:** 2026-11-19 (revision 4: agent autonomy realigned during design)
**Source:** Frédéric's request in conversation, quoted where possible.
**Purpose:** the yardstick for building the next version of Régie.
**Status:**
- Decisions from both reviews applied (see `2026-11-19-lead-agent-orchestrator-fable-review.md`).
- Revision 4: R12 and R14 rewritten. The earlier "the tool enforces limits on agents" rule came from the review, not from me; agents now work like my own sessions (see R14).
- Spike done (see "Spike results").
- Next: design.

## Goal

> "On a daily basis what I want is: have a lead agent orchestrator I can tell to work on a backlog of tickets."

The model is an engineering org, inspired by Miguel Ríos Berríos, *I ran my agents like an engineering org* (LinkedIn): one agent leads and the others do the work. I talk to the lead in a web dashboard and watch the work move across a board.

## Terms

- **Session:** one conversation with one lead, covering one or more local repos. I can run several sessions.
- **Ticket:** an item in an outside backlog, such as Jira or GitHub Issues, reached through my Claude or Codex plugins. The tool doesn't own tickets.
- **Task:** one unit of work, started by an instruction from me or from the lead.
  - One task is one card on the board, one repo, one worktree, one branch and one PR, like an Orchestrator.inc worker session.
  - **Tasks and tickets aren't tied one-to-one.** A ticket can be split into several tasks, a task can cover several tickets, and a task can have no ticket. What defines a task is the instruction that started it. The tickets it covers are references on the card.
  - A task is either independent or **blocked by** another task. A blocked task waits in Queued until the task it depends on is merged.
- **Lead:** the agent that runs a session. It plans, delegates and escalates. It doesn't edit code itself.

## Requirements

### R1. Start from local folders, with no setup

- I point a session at one or more local folders and tell the lead to start. No GitHub App, no connectors to configure in the tool, no company or org setup, no per-agent sign-in.
- Agents run with **my own Claude Code and Codex setup**: the logins, plugins, MCP servers and connectors already on the machine. That is how they reach Jira and everything else. They work as autonomously as my own sessions (R14).
- **GitHub only in the first version**, through my existing `gh` login.
- **Which folders can get tasks:** a folder gets tasks only if it's a git repo with a GitHub remote. Any other folder in a session is read-only context.

> "I just wanna go to a local folder and say start this work"

### R2. Work from the project's backlog

- **Where tickets come from:** whatever my Claude or Codex plugins can reach. For me that's Jira; for another project it could be GitHub Issues. The tool has no ticket integration of its own.
- **Talking to the lead:** in chat, I tell it what to work on, or ask it to pick from the project's backlog.
- **Every instruction has an end point:** N tickets, named tickets, or "until I say stop".
- **First use of a project is a short onboarding.** The lead proposes:
  - the backlog filter and order (what "next" means);
  - which Jira statuses mean "in progress" and "in review";
  - the build, test and lint commands;
  - the untracked files to copy into worktrees, such as `.env`;
  - the definition of done.

  I confirm once, and it's stored in project memory (R15).
- **Preflight at session start:** check that the backlog can be reached. If not (for example the connector's sign-in has expired), escalate.
- **Ticket updates:**
  - The lead may comment on a ticket and move it to the mapped "in progress" or "in review" status without asking. Any other change to a ticket needs my OK.
  - The lead makes these updates itself, with my connectors. The allowance is part of its instructions.
- **When the backlog's provider is blocked:** only Claude reaches Jira on this Mac, and the lead runs on Claude. While Claude is blocked by its usage limit, the lead's decisions and ticket updates wait, and the rest of the work carries on.

> "tickets come from where ever is possible connected via native claude or codex plugins"

### R3. An autonomous lead that delegates

- **The tool calls the lead whenever something happens.** It doesn't keep one long conversation open. Triggers:
  - my chat message;
  - an agent finishing;
  - a CI or PR change;
  - a quota change;
  - an escalation being answered;
  - resuming after a pause.

  Each call rebuilds the lead's picture of the session from the tool's state (R12). Its context stays small however long the day runs. (Settled in the design: one lead conversation fed with events as messages, with a board snapshot in each, replaceable from the tool's state. See `docs/superpowers/specs/2026-11-19-lead-agent-orchestrator-design.md`.)
- **The lead orchestrates through commands the tool provides:**
  - create a task (with its tickets and dependencies);
  - assign it to a role and provider;
  - request a review;
  - escalate;
  - propose a follow-up.

  The tool checks each command and carries it out. Outside orchestration, the lead uses my setup like any session, for example to read and update Jira tickets.
- **It works autonomously**, in the spirit of the article.
- **It escalates whenever it's unsure or needs a human decision**, and each escalation comes with a suggested answer. It must escalate when:
  - The scope changes, or the acceptance criteria are unclear.
  - Tickets or instructions conflict.
  - A change is security-sensitive or can't be undone.
  - Tests can't be made to pass within the loop limits in R6.
  - Reviewer and builder still disagree after the verify pass in R6.
- **How escalations work:**
  - An escalation is a stored item linked to its card, with the suggested answer and an **accept suggestion** button.
  - It blocks only its own task.
  - It goes into the **Needs me queue** (R18) and also shows up in its session's chat. My reply, from either place, is tied to that escalation, not guessed from free text.
- **We tune how often it asks by using it.**

> "he should be autonomous … but always escalate with me when in doubt or something needs human input, suggest something then we test and iterate"

### R4. Claude and Codex on subscriptions

- Sub-agents and leads run on Claude Code or Codex, using their subscriptions. No API keys.
- **Model and effort per role** (lead, builder, reviewer) are settings, with defaults and an override per session. In the first version the lead runs on Claude, because only Claude reaches Jira (R5).

### R5. Stop starting work before a usage limit hits

**First-version policy:**

- **At a provider's threshold (R10), the tool starts no new work on that provider.** Running agents finish their current run. Stopping new work at the threshold is what keeps it short of the limit.
- **If an agent still hits a real limit mid-run,** the tool parks its task.
  - The task restarts from its handover package on the other provider if that provider is under its threshold.
  - Otherwise it waits in Queued until the window resets.
- **The handover package** is the task's branch with work in progress kept, the task's log of instructions, findings and decisions, and a short summary. It's used only across providers. On the same provider, the agent's native session is resumed instead (R13).
- **The lead stays on Claude.** The threshold leaves the rest of Claude's window for the lead and my own use. If Claude is fully blocked, Codex agents finish their runs, and the lead's next decisions wait for Claude's window to reset.
- **A "run anyway" override** per task lets me spend past a threshold when I choose to.

**Reading usage:**

- **Codex:** `codex app-server` is polled, with no model call.
- **Claude:** the `rate_limit_event` from any running Claude call. The reading goes stale when no Claude call is running, so the usage panel shows how old each reading is.

> "detect usage limits and reroute tasks or handover to another provider before hitting limits"

### R6. Build, test, review, PR

- **Order for each task:** build → local checks (the project's build, test and lint commands) → adversarial review → the tool opens the PR → CI → ready to merge.
- **Always reviewed by a different agent** from the one that wrote the work.
- **By the other provider** when that provider is under its "no new work" threshold (R10); otherwise by a fresh agent on the same provider.
- **The reviewer** works in the task's worktree. It can run commands and tests, and is told not to edit.
- **Review has to finish.** Unchecked, adversarial review keeps finding new edge cases every round (Régie bench, 2026-09-28). So:
  - **A round** is one review followed by one fix. At most **2 rounds**, then a **verify-only pass** limited to the earlier findings.
  - **A blocking finding needs evidence:** a command that reproduces it, a failing test, or an unmet acceptance criterion cited with file and line. Without evidence, a finding is advisory.
  - **The lead decides scope.** It decides whether each finding is in scope; out-of-scope findings become proposed follow-up tasks.
  - **Still disagreeing after the verify pass:** the lead escalates (R3).
- **Other fix loops are capped too:** fixing failed CI, or responding to change requests and review comments on the PR, at most **3 times each**, then escalate.

### R7. Web dashboard

- **A web page first:** the quickest to build, test and iterate on. A desktop app and a TUI may come later.
- **A board:** one card per task, in the lanes of R9.
- **Each card shows:**
  - its lane and flags;
  - the agent (role, provider, model) working on it;
  - the tickets it references and its PR link;
  - a timeline of the messages between agents: instructions, reports, review findings, handovers and escalations;
  - the agent's live output;
  - its usage in tokens and run time.
- **A chat with the lead** for each session. Escalations from that session show up there too (R3).
- **The Needs me queue** (R18), always visible whichever session I'm looking at.
- **The log of decisions and improvements** (R23).
- **A usage panel:** each provider's 5-hour and 7-day usage with the age of each reading, which provider is paused and when it resets.
- **Controls:** pause and resume (R16); cancel, retry and stop an agent (R17); run anyway (R5).

### R8. Several sessions, several repos

- **One session can cover several repos or folders.** Each task touches one repo (Terms).
- **I can run several sessions** and switch between them in the dashboard.

### R9. Task status, with merged as done

- **A task is done when its PR is merged. I merge.** The lead brings each task to Ready to merge and stops there.
- **The lane is never stored.** It's worked out each time from:
  - stored facts: task events, review rounds, open escalations, pause and cancel;
  - live facts: agent processes, the PR and CI state from `gh`, and quota.
- **Lanes, by precedence** (the first row that matches wins):

  | Lane | When |
  |---|---|
  | Merged | The PR is merged |
  | Cancelled | I cancelled it (archived off the board) |
  | Working | A builder is running, or a fix is due: failed local checks, a blocking finding, failed CI, changes requested, or a merge conflict |
  | In review | Adversarial review is running, or the PR waits for a GitHub approval the repo requires |
  | CI | The PR is open and checks are running |
  | Ready to merge | Review is closed, CI is green, there are no conflicts, and the repo's branch-protection rules are met |
  | Queued | Not started, or parked: waiting for a free slot, for quota, or for a task it depends on |

- **Flags on any card:** **Needs me** (an open escalation; these cards sort to the top), **Paused**, and the reason a card is queued.
- **GitHub reviews by people** are optional, unless the repo requires them.
- **Definition of done:** the project's definition is confirmed at onboarding (R2) and kept in project memory (R15).

### R10. Parallelism that fits subscription limits

Defaults below. All are settings, to be tuned as we use it.

- **Concurrency, counting only tasks with an agent running:**
  - At most **4 running agents across all sessions**, counting builders and reviewers. Leads aren't counted towards the cap; their usage still counts against the provider's quota.
  - At most **2 running tasks per session**. Tasks in CI, in Ready to merge, or waiting on me don't count.
- **Per provider:**
  - **5-hour window:** start no new work at **75% used**.
  - **7-day window:** keep usage on pace. Once a provider is above **20% used**, start no new work on it while its weekly percentage used is more than **10 points ahead** of the percentage of the week that has passed. My own interactive use counts against the same quota.
- **When both providers are blocked:** running agents finish, new work waits in Queued, and the usage panel shows when each window resets.

Why these numbers: each running agent uses quota roughly in proportion to its time, and one long implementation run can use a sizeable share of a 5-hour window. Four at once across two providers means about two per provider. That leaves room for the leads and for my own use. On 2026-11-19 this machine showed Claude's 7-day window at 63% used with 69% of the week gone. That's on pace, but with little slack.

### R11. Local only

- A web server on my Mac. No hosting, no accounts, no cloud sync.
- **It listens on 127.0.0.1 only and checks a per-launch token and the request origin,** because it can start agents with my credentials.

### R12. The tool holds the state

- **State:** sessions, tasks, ticket references, task logs, decisions, escalations, chat history and handover packages live in the tool's own storage, not in any agent's conversation.
- **The work itself is done by the agents,** like my own sessions: builders commit, push and open their PR; the lead updates tickets. The tool prepares worktrees, schedules, watches PRs and CI, and cleans up. It never merges.

### R13. Resume after a restart

- After the tool restarts, crashes, or the Mac sleeps, sessions and tasks pick up where they were.
- **An agent resumes its own session first** (`claude --resume`, `codex exec resume`). The handover package is used only across providers or when native resume fails.
- **On start, the tool checks the agent processes it recorded.** It takes over the ones still running or stops them. No orphan keeps editing a worktree.
- **The Mac stays awake while agents are running.**

### R14. Autonomy and isolation

> "the whole point of the agents is to be autonomous, not baby sitted"

- **Agents work like my own Claude Code and Codex sessions:** my plugins, skills, hooks and connectors, network access, and no prompts for permission. Claude runs in its auto mode; Codex in its autonomous mode.
- **Start simple, restrict when a problem shows up.** No sandbox profiles or tool allowlists in the first version. Every agent's output is kept, so problems are visible when they happen.
- **What stays, because it's the job rather than babysitting:**
  - **Worktrees:** each task works in its own worktree and branch, created from a freshly fetched `origin/<default branch>`. My working copy (its files, index and checked-out branch) is never changed. The tool copies the project's listed untracked files, such as `.env`, into new worktrees (R2).
  - **I merge.** Neither the tool nor the agents merge; agents are told so.
  - **Parallel test runs must not collide**, for example on ports or local databases. How is a design question.
  - **Cleanup:** after a merge or a cancel, the tool removes the task's worktree and local branch. It never deletes remote branches.
- **Accepted risk:** an agent steered by a ticket, a PR comment or a web page could push elsewhere, post through a connector, or touch files outside its worktree. Only its instructions and, for Claude, auto mode's own judgement stand in the way.
- **Recommended:** branch protection on each repo's default branch as a backstop.

### R15. The lead remembers each project

For each repo or project, the tool stores what onboarding settled (R2) and what the lead learns afterwards:

- the backlog filter and order;
- the Jira status mapping;
- the build, test and lint commands;
- the files to copy into worktrees;
- conventions;
- the definition of done.

It doesn't ask me again in the next session.

### R16. Pause button

> "I wanna be able to press a PAUSE button to stop everything running in a session (like if I need to go home or save usage limit for a hotfix)"

- **Pause on a session:**
  - Every running agent in that session stops immediately.
  - Its worktree stays as it is, and its session is kept so it can be resumed.
  - Nothing new starts: no new agents, no lead calls, no pushes, no ticket updates. Queued actions stay queued.
  - The board keeps showing PR and CI changes, read-only, and the session shows as **Paused**.
- **Resume:**
  - Agents continue their own sessions (R13) and are told they were paused.
  - The lead is called with what happened in the meantime, such as CI results and PR comments.
- **Pause all:** one button pauses every session, to free quota at once, for example for a hotfix.
- Pausing stops usage straight away. Nothing is lost or rolled back.

### R17. Task controls

- **Cancel a task:** stop its agent, close its PR if one is open (after I confirm), and clean up (R14).
- **Retry a task:** start a fresh run from its branch and log.
- **Stop one agent** without cancelling its task. The task goes back to the lead to decide what's next.

### R18. One queue for everything that needs me

> "its hard to see when something is requested from me, its lost in the different sessions / lead, no real highlight or notification. I would prefer this to be centralized in a queue or in the lead chat."

This is what Orchestrator.inc gets wrong in daily use.

- **What goes in the queue:** everything waiting on me, across **all sessions**, in one place:
  - escalations (R3);
  - PRs ready to merge (R9);
  - confirmations: project onboarding (R2), ticket changes beyond the allowance (R2), closing a PR on cancel (R17);
  - work held back by a threshold that I could release with "run anyway" (R5).
- **Each item shows:**
  - its session, task card and type;
  - what is asked, with the suggested answer;
  - the actions to take it right there: accept the suggestion, reply, open the PR, or confirm.
- **It's one list, shown twice.** The same items also appear in their session's lead chat. Answering in either place clears them in both.
- **Order:** items that block work come first, then oldest first.
- **Visibility:**
  - The queue is pinned and always visible, whichever session is open.
  - Each session in the sidebar shows its own count.
  - The browser tab title shows the total.
  - A new item gets a **browser notification** (opt-in), so I see it even when the dashboard isn't in front.

### R19. Memory: lessons the tool keeps

> "can we include a memory and self improvement baked in the tool? Like metaswarm is doing"

R15 covers the facts onboarding settles. This adds **lessons**: short things learned from doing the work. (Proposed by Fable 5.2 after reading MetaSwarm and Régie's existing knowledge code.)

- **A lesson is** one sentence of fact, one sentence of what to do about it, and its evidence: the task, PR, escalation or review finding it came from. A lesson without evidence isn't stored.
- **Two scopes in the first version:**
  - **Repo:** conventions, gotchas and decisions for one repo, optionally tied to paths.
  - **Me:** how I want things done in every project, such as how I like PRs split or what I always reject.
- **Where they live:** in the tool's storage (R12), not in the repo. Nothing to set up, nothing committed. I can export a repo's lessons to a file if I want them in git.
- **Who writes them:** only the tool, in two ways.
  - **From my own words, active at once:** my answer to an escalation, a correction I give the lead in chat, a change I request on a PR, or "remember this" in chat. The lead proposes the lesson with a command (R3), and the tool stores it only if it points to a message of mine.
  - **From the work, as candidates:** a blocking review finding that was confirmed and fixed, a CI failure that needed a fix loop, a human review comment on a PR. A candidate isn't shown to agents.
- **A candidate becomes active without me** when the same lesson turns up in a second task. It's dropped after 30 days if it doesn't.
- **Agents never write memory directly.** Text from tickets, PR comments by other people, and agent output can become a candidate, never an active lesson on its own.
- **I'm not asked to approve lessons.** I see them in the log (R23), with their evidence, and can edit or delete one in place. Chat works too: "forget that", "that's wrong".

### R20. Memory reaches the lead and the agents, within a budget

- **The tool injects lessons into prompts.** Agents don't query memory, and it works the same on Claude and Codex.
- **The lead** gets the "me" lessons and the repo lessons for the session's repos on each call (R3).
- **A builder or reviewer** gets the lessons that match its task: by the paths it's likely to touch, then by words in the instruction.
- **Budget:** at most **10 lessons and about 600 tokens per agent prompt**, and **20 lessons for the lead**. When more match, the most recently confirmed win. The numbers are settings.
- **Each task records which lessons it was given**, on its card. That's what makes R21 and R22 measurable.

### R21. Memory stays fresh on its own

- **Every lesson has a "last confirmed" date.** It's set when the lesson is created and renewed whenever the lesson is given to a task that then merges with no blocking finding and no correction from me on that subject.
- **Expiry:** a lesson not confirmed for **90 days** is retired. Retired lessons stop being injected and are kept for 30 days in case I want one back.
- **Lessons tied to paths** are retired when those paths no longer exist.
- **Contradictions:**
  - A newer statement from me replaces an older lesson on the same subject, and the card says so.
  - If two lessons conflict and neither is from me, both stop being injected and one item goes to the Needs me queue (R18) with a suggested answer.
- **A lesson that agents were given and still broke twice** is flagged in the next reflection (R22) as not working, to be reworded or removed.
- **Cap:** at most **200 active lessons per repo**. Past that, the least recently confirmed are retired.

### R22. Self-improvement: the tool proposes changes and shows whether they helped

- **What triggers a reflection:** every **10 merged tasks** in a project, or me asking for one. It's one lead call, read-only, over data the tool already holds (R12).
- **What it looks at, per project, compared with the previous period:**
  - escalations per task, and the share I answered with **accept suggestion**;
  - review rounds per task, and how many tasks reached the verify pass;
  - blocking findings by kind, to spot the ones that repeat;
  - CI fix loops and PR change-request loops per task;
  - tokens and run time per merged task;
  - tasks cancelled or retried;
  - limit hits mid-run.
- **What it may propose, at most 3 per reflection, each with its evidence:**
  - a new or reworded lesson (R19);
  - a change to a setting: the caps and thresholds in R6 and R10, or the model and effort per role in R4;
  - a change to the project's stored facts (R15), such as a test command or the definition of done;
  - an addition to a role's standing instructions for this project, such as what the reviewer must check or when the lead should escalate.
- **What it may not touch:** R14's rules (agent autonomy, worktrees, I merge), the tool's own code, or the built-in prompts. Project instructions are added on top of the built-in ones, not instead of them.
- **Who approves:** I do. The proposals from one reflection arrive as **one item** in the Needs me queue (R18), each with accept and reject. Lessons from R19 don't wait for this.
- **Every accepted change is recorded** in the log (R23): what changed, why, when, and the old value, with a one-click **revert**.
- **Did it help:** each proposal names the one metric it expects to move. After the next 10 merged tasks, the reflection reports that metric before and after, and suggests keeping or reverting. It's a before-and-after comparison on few tasks, not a proof, and the report says so.

### R23. A log of what was decided and improved

> "would be nice to have a log of this to see what was decided and improved, and edit or revert in place"

One page in the dashboard: a dated history of every decision and change that shapes how the tool works.

- **What it records:**
  - **My decisions:** escalation answers, confirmations (onboarding, ticket changes, closing PRs), and reflection proposals I accepted or rejected.
  - **The lead's decisions that change scope or rules:**
    - findings it ruled out of scope, and the follow-ups it proposed;
    - assumptions it made instead of asking;
    - how it split a ticket into tasks.
  - **Memory:** every lesson created (from my words, or a candidate reaching its second occurrence), edited, replaced, retired or deleted (R19, R21).
  - **Improvements:** changes to settings, project facts (R15) and role instructions (R22), each with its metric before and after once measured.
- **Each entry shows** what was decided or changed, who decided it (me, the lead, or the tool), why, its evidence (a link to the task, PR, escalation or chat message), and for changes the old and new value.
- **Edit or revert in place:**
  - **Edit:** lessons, settings, project facts and role instructions can be edited from their entry.
  - **Revert:** a change can be undone in one click, restoring the old value.
  - **Logged too:** every edit and revert is itself an entry, so nothing is lost.
  - **Decisions that already happened** (an answered escalation, a merged task) can't be undone. Their entry links to the work they shaped, and I can start a follow-up task from there.
- **Filters:** by project or session, by kind (decisions, lessons, improvements), by who decided it, and **"in force now"**: the lessons, settings and instructions currently shaping the agents.
- **Improvements view:** the changes in force, each with its metric before and after, and the reverted ones (R22). It's a filter of this log, not a separate store.

### R24. A régie look (nice to have)

> "given the tool is called "Regie" it would be nice to have a playfull UI in the thematic of a regie, like with nobs and stuff"

The dashboard is styled as a TV control room (*régie*). I'm the director, and the work is the show.

| Tool concept | In the régie |
|---|---|
| Me | The director |
| A session | A studio, picked from a studio selector |
| The board | The multiviewer: a wall of monitors, one per task |
| A task's lane | The monitor's tally light: red when an agent is on air (working), green in preview (ready to merge), amber when it needs me, off when queued |
| The lead and the agents | The assistant director and the operators (Cam 1, Cam 2…), each labelled with its provider |
| Any agent running | The ON AIR sign is lit |
| Usage per provider (R10) | Audio-desk VU meters, one per provider and window, with the threshold marked as the red zone |
| Settings such as concurrency and thresholds | Knobs and faders |
| Pause all (R16) | The "fade to black" button; pausing one session is "standby" |
| The Needs me queue (R18) | The talkback panel, with a call light that blinks |
| The log (R23) | The logbook |

Guard rails:

- **The theme never costs clarity.** Every control and light also has a plain label, and colour is never the only signal.
- **It respects the system's light and dark modes and its reduced-motion setting.** Blinking lights stop blinking when reduced motion is on.
- **The first version gets a light touch:** tally lights, VU meters, the ON AIR sign and fade to black. The rest can grow later.

## Not in the first version

- **Switching provider mid-run at 90% of a window**, and **moving the lead to Codex.** Both were wanted; they're deferred by the review because of their cost and risk, and because a lead on Codex can't reach Jira.
- **A desktop app and a TUI;** answering escalations from the terminal (R7).
- **Letting the lead merge,** as a setting (R9).
- **Git hosts other than GitHub** (R1).
- **Tasks that span several repos** (Terms).
- **Phone and desktop-app notifications.** Browser notifications are in the first version (R18).
- **Memory and self-improvement beyond R19 to R22:**
  - a third "project" scope spanning several repos;
  - search by meaning (embeddings), which would need an API key or a local model;
  - a scheduled curator agent;
  - lessons committed to the repo;
  - changes that apply themselves without my approval;
  - A/B runs of prompt variants;
  - a "this task only" answer to an escalation, to stop the lead turning a one-off answer into a lesson. Added only if over-generalising shows up in use.

## Acceptance scenario

The tool needs two things for testing: a **usage source that can be overridden**, so thresholds and limit hits can be simulated, and a **defined way to kill the tool** for the restart test.

**Main flow:**

1. I open the dashboard and start a session on two local repos. The lead runs onboarding for a new project, I confirm, and it passes the preflight check.
2. I say: *"Work on the next 2 tickets of Jira project X."* The lead creates three tasks: one ticket is split into two, and the second of those is blocked by the first. It assigns them to builders on Claude and Codex.
3. Local checks run, then cross-provider review. One blocking finding (with evidence) loops a task back to **Working** for a fix.
4. The lead hits an unclear acceptance criterion. It escalates with a suggested answer:
   - the card shows **Needs me**;
   - the item appears in the **Needs me queue** and in the session's chat;
   - a browser notification fires.

   I click **accept suggestion** in the queue, and the item clears in the chat too.
5. The simulated Claude 5-hour window crosses 75%. New work goes to Codex, and the usage panel shows why.
6. I press **Pause**. Every agent in the session stops, and no further usage occurs. I press **Resume**, and the agents continue their sessions.
7. The builders open the PRs, CI runs, and the cards reach **Ready to merge**. The lead has commented on both Jira tickets and moved them to "in review".
8. I kill the tool mid-run and start it again. Everything resumes, with no orphan agents.
9. I merge the first PR. The blocked task starts. Merged cards move to **Merged**, and their worktrees are removed.

**Edge cases the first version must also pass:**

- A real limit hit mid-run parks the task and restarts it on the other provider.
- With both providers blocked, work waits, and the panel shows when each resets.
- While Claude is blocked, the lead's decisions and Jira updates wait, and Codex work carries on.
- The 2-round review cap leads to the verify pass, then an escalation.
- A CI failure loops back to Working; after 3 failures, it escalates.
- A merge conflict loops back to Working.
- A task can be cancelled and retried.
- A second session runs alongside the first, and I can switch between them. Items from both sessions appear in the one Needs me queue.
- **Pause all** stops every session.
- I answer an escalation; the next task in that repo gets the lesson in its prompt, and the card lists it.
- A lesson past 90 days unconfirmed is no longer injected.
- After 10 merged tasks, one reflection item appears in the Needs me queue. I accept one proposal, and the log shows it with its metric and a revert button. I revert it from the log, the old value is back, and the revert is logged too.
- I edit a lesson in the log, and the next task's prompt uses the edited text.

## Spike results (2026-11-19)

Details in `docs/spikes/2026-11-19-lead-agent-spike.md`.

1. **Headless Codex resume works**, including after the process is killed mid-turn. The context survives, and the prompt cache is reused.
2. **Concurrent Codex runs on one login work.** A refresh race couldn't be provoked safely: the token lasts 240 hours. **Design choice:** run all Codex agents as threads of one `codex app-server`, which owns the login. It ran two turns at once, and its protocol also gives interrupt (R16), resume (R13), steering, and rate limits (R5).
3. **Claude permissions:** `--permission-mode dontAsk` with an allowlist denies everything else, and `--disallowedTools` removes write tools (or whole connectors) from view. **Rules on shell commands are not a boundary:** `bash -c 'git push …'` got past a `git push` deny. The CLIs' sandboxes are: Claude's blocks network through an allowlist proxy, and Codex's workspace-write has no network. This led to sandboxed agents in revision 3; revision 4 dropped that in favour of autonomous agents (R14).

## Constraints found on this machine

- **R5 is feasible: both CLIs report remaining usage.**
  - Claude Code with `--output-format stream-json` emits a `rate_limit_event`. It carries `unifiedWindows.five_hour` and `seven_day` utilization, read at 11% and 63% on 2026-11-19.
  - `codex app-server` answers `account/rateLimits/read` with the percentage used per window, without making a model call.
- **Codex must reuse the existing login.** The ChatGPT Team workspace blocks device-code sign-in, so the tool can't sign in to Codex itself; it has to use the login already in `~/.codex/auth.json`.
- **Jira is reachable from Claude only.**
  - A headless `claude -p` run gets the claude.ai Atlassian connector.
  - Codex's plugins on this Mac include GitHub but nothing for Atlassian.
- **Tools that isolate the agent's config break R1 and R2.** Paperclip, for example, gives each agent its own Claude config directory, so the user's plugins and connectors never reach the agents.

## What carries over from Régie

- **Reused:**
  - the Claude and Codex adapters;
  - quota parsing and provider cooldowns;
  - git and worktree handling, including dependency setup;
  - gates (build, test, lint);
  - the run ledger;
  - the review profiles;
  - `knowledge.py`'s store and `prime()` matching, for R19 and R20;
  - `stats.py`'s per-stage outcome and usage figures, for R22's metrics.
- **Not reused as-is:** the existing knowledge flow. It turns every decision and finding into a candidate, then waits for a manual `regie knowledge-approve` that was never run: the store holds three empty project folders and no run has a candidates file. R19's "my own words, or a second occurrence" rule replaces that approval step.
- **Replaced:**
  - the deterministic pipeline and state machine, by the event-driven lead and the tool's command handling;
  - the plan stage and Product Owner intake, by the lead;
  - the TUI control room, by the web dashboard.
- **Built as a new core in the Régie repo,** importing the reused parts, rather than bending the existing pipeline into a lead-driven model.

## Candidates checked against the requirements

| Candidate | Main gaps | Verdict |
|---|---|---|
| Paperclip | R1 (company, connectors, GitHub App and per-agent sign-in to set up); R2 (the agents' separate Claude config hides my connectors); R5 (no cross-provider fallback upstream) | Rejected |
| Claude Code + Codex plugin | R2 and R3 (no backlog; Codex runs only when asked); R5 (handover to Codex is manual, via `/codex:transfer`); R7 and R9 (no board, no status from PRs) | Not enough |
| Régie today | R2 and R3 (one brief per run; a deterministic state machine with no lead agent); R5 (usage limits handled only after they hit); R7 (a TUI with no board or chat); R8 (one repo per run); R13 (resume is a manual `regie resume`); R16 (no pause) | The source of reusable parts (see above) |
| Orchestrator.inc | R7 (a desktop app, not web); R5 (not mentioned); R2 (issue intake is GitHub or GitLab only, read-only) | Being tried by Frédéric since 2026-11-19, who rates it highly, including how its lead works. **The main reference design** (see below) |

## Reference design: Orchestrator.inc

We should borrow freely from it.

**From using it (Frédéric, 2026-11-19):**
- **Liked:** the ease of use. One instruction, and the lead spread the work across workers. The board view is really nice.
- **Disliked:** it's hard to see when something needs me. Requests get lost across sessions and the lead, with no real highlight or notification. That's what R18 is for.

**From its docs and architecture notes:**

- **The lead:** a Chat session on the project that plans the work and starts one worker session per piece, each from an initial instruction. The worker's title and branch name come from that instruction.
- **Workers:** each has its own worktree, branch (`ao/<task-slug>/root`) and PR, and its own choice of agent and model.
- **Feedback goes back to the worker that owns the branch:** failed CI, review comments and merge conflicts. Waiting messages (agent messages, review comments, conflict notices) are batched into the agent's next turn, while each message I type gets a turn of its own.
- **Status is never stored.** It's derived at read time from durable facts in a fixed order: merged or terminated, then waiting for input, then the PR states, then working or idle.
- **The dashboard:**
  - a sidebar of projects and sessions;
  - a centre pane with either a structured chat or the agent's own terminal UI, switchable for Claude Code and Codex while keeping the same session;
  - an inspector with the PR summary, reviews, changed files and a browser preview.
- **Notifications** when a session needs input, a PR is ready to merge, or a PR is merged or closed.

**One design point to settle against R3.** Orchestrator.inc's lead looks like one long chat conversation that receives events as queued messages. R3, following the review, says the tool calls a lead whose picture is rebuilt from the tool's state on each call. A middle way: keep the lead's conversation and deliver events into it as messages, which Orchestrator.inc does and Frédéric likes. Meanwhile R12 still keeps all state in the tool, so a lead can be replaced from that state when its context grows too large or it has to change provider.
