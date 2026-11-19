# Spike: headless Codex resume, concurrency, and Claude permissions

**Date:** 2026-11-19
**For:** `docs/requirements/2026-11-19-lead-agent-orchestrator.md` (R5, R13, R14, R16)
**Setup:**
- Claude Code 2.1.328 (Haiku 5.5 for the permission runs). Codex CLI 0.170.0 (`gpt-6.1-sol`).
- A throwaway git repo with no remote.
- Scripts in `2026-11-19-lead-agent-spike/`.

**Kept safe:**
- No Jira, Slack or Confluence write tool was ever called; write-tool availability was read from the tool list only.
- No push could reach anything, because the test repo has no remote.
- Nothing touched the Codex login's tokens.

## Results at a glance

| # | Question | Result |
|---|---|---|
| 1 | Does headless Codex resume work, including after a kill mid-turn? | **Yes** |
| 2 | Do concurrent Codex runs on one login break each other's sign-in? | **No failure seen. The refresh race couldn't be triggered safely, and one `codex app-server` avoids it entirely** |
| 3a | Can a headless Claude run allow only listed tools and deny the rest? | **Yes** (`--permission-mode dontAsk` plus `--allowedTools`) |
| 3b | Can write tools be removed from the agent's view? | **Yes** (`--disallowedTools`, by tool or whole server) |
| 3c | Can rules on shell commands stop an agent from pushing? | **No.** `bash -c '…'` and `git -C . push` get past them |
| 3d | Do the CLIs' sandboxes block network access? | **Yes, both, by default** |

## 1. Headless Codex resume

**1a. Basic resume.**
- `codex exec --json "Remember the code word PAPAYA…"` printed its thread id in the `thread.started` event.
- `codex exec resume <id> --json "What was the code word?"` answered **PAPAYA**.
- The resumed turn used 34,438 input tokens, of which 30,080 were cached. Resuming is cheap.

**1b. Resume after SIGKILL mid-turn.**
- A run was told to remember **MANGO** and then run `sleep 30`. It was killed with SIGKILL 9 seconds in, while the command was running.
- `codex exec resume <id>` answered "MANGO; I haven't confirmed that the sleep command finished." The context survived, and the agent correctly treated the interrupted command as unconfirmed.

**For R13:** resuming the same provider by session id works for Codex. On restart, the tool resumes each Codex agent's thread and tells it it was interrupted.

## 2. Concurrency on one Codex login

**2a. Four `codex exec` runs at once.**
- All four succeeded in about 5 seconds each.
- `~/.codex/auth.json` was untouched.
- The login's access token lasts **240 hours** and was last refreshed on 2026-11-17, so a refresh happens roughly every 10 days.
- The race the review worried about can only happen when two processes refresh at the same moment. That couldn't be provoked without risking the real login.

**2b. One `codex app-server`, two threads, two turns started at the same time.**
- Both turns started at 0.0 s and completed at 5.6 s and 6.4 s.
- Each thread got its own answer ("thread 1 done", "thread 2 done").

**For the design:**
- **Run all Codex agents as threads inside one long-lived `codex app-server`.** One process owns the login, so concurrent refreshes can't race.
- The same process reports rate limits (`account/rateLimits/read`) with no model call (R5).
- Its protocol also covers several requirements:
  - `turn/interrupt` gives the pause button (R16);
  - `thread/resume` gives restart recovery (R13);
  - `turn/steer` delivers a message into a running turn, the way Orchestrator.inc's lead feeds its workers.

## 3. Claude permissions

**3a. Allowlist mode works.**
- With `--permission-mode dontAsk --allowedTools <reads>`, a call to a tool not on the list (`atlassianUserInfo`, a read) was **denied**. It was recorded in the result's `permission_denials` and the agent replied "denied".
- With that tool added to the list, the call succeeded.
- However, tools that aren't allowed **stay visible** in the agent's tool list; they're only refused when called.

**3b. Removing tools from view works.**
- `--disallowedTools mcp__claude_ai_Slack` removed the whole Slack server's tools. Listing Atlassian write tools by name removed those too.
- Gone from the tool list: `createJiraIssue`, `editJiraIssue`, `executeWrite`, `executeDestructive`, `createConfluenceContent`, `updateConfluenceContent`, and all the Slack tools.
- This held even under `bypassPermissions`.

**For R14 and R2:** each agent profile combines both: `dontAsk` with an allowlist of what it may use, and `--disallowedTools` to hide every write tool it must not see.
- **Builder or reviewer:** connector reads only.
- **Jira writer:** Jira reads plus `addOrEditJiraIssueComment` and `transitionJiraIssue`.

**3c. Rules on shell commands are not a boundary.** With `Bash` allowed and `Bash(git push:*)` denied:

| Command | Outcome |
|---|---|
| `git push origin main` | Denied |
| `cd . && git push origin main` | Denied |
| `bash -c 'git push origin main'` | **Ran.** It failed only because there's no remote |
| `git -C . push origin main` | **Ran.** It failed only because there's no remote |

**3d. The sandboxes do block the network.**
- **Claude** with `--settings '{"sandbox":{"enabled":true}}'`: `curl https://example.com` and `git ls-remote https://github.com/...` were both blocked by the sandbox's network proxy ("blocked-by-allowlist").
- **Codex** with `-s workspace-write`: both failed with "Could not resolve host". There is no network at all.

**For R14:** the guarantee that agents can't push, or reach anything they shouldn't, comes from the **sandbox**, not from rules on shell commands:
- Agents run sandboxed, with no route to github.com and writes limited to their worktree.
- **The tool** commits and pushes outside the sandbox (R12).
- Dependencies are installed by the tool before an agent starts, as Régie's worktree setup already does. Claude agents could instead get a domain allowlist for package registries.

## Still to check during design

- **Claude's sandbox file rules:** that writes are limited to the worktree, and how to set a domain allowlist for package registries.
- **Connectors inside Claude's sandbox:** that the claude.ai connectors still work. They run in the Claude Code process, not in the shell, so they probably do. Run 3b was without the sandbox.
- **Claude's usage reading while idle:** `rate_limit_event` only arrives during a Claude call. Whether something cheaper can read it is still open (R5 already says to show each reading's age).
- **Resuming a killed Claude session:** `claude --resume` after a SIGKILL wasn't tested.
