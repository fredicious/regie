# Handover: start milestone 2 (the lead)

**Written:** 2026-11-19, end of the session that designed Régie's next version and built milestone 1.
**For:** a fresh session picking up tomorrow.

## Where things are

- **Requirements:** `docs/requirements/2026-11-19-lead-agent-orchestrator.md`, revision 4. Agents are autonomous, like Frédéric's own sessions (R14).
- **Design (approved):** `docs/superpowers/specs/2026-11-19-lead-agent-orchestrator-design.md`. Milestone 2 is section 9, item 2. Model choice was revisited after milestone 1: the lead picks levels; Frédéric owns the map from level to model (edited with `regie settings`) and can pin one task to a model (section 2).
- **Milestone 1:** done and live-tested. Plan: `2026-11-19-milestone-1-one-task-by-hand.md`. Live-run notes and every deferred review finding: `2026-11-19-milestone-1-live-run.md`.
- **Since the live run:** a fresh PR with no checks counts as pending for 3 minutes, not ready (`f693319`, `31b74c5`); `regie session delete` (`d4cb57b`, waits for cleanup); `regie stop` (`95978c6`).
- **Current code map:** `2026-11-19-milestone-2-interfaces.md`, a snapshot at `31b74c5` with exact names, signatures and the list of what's missing for a lead.
- **Installed:** `~/.cargo/bin/regie` (the old Python `regie` uv tool is uninstalled). Restart with `regie stop` then `regie`. Data in `~/.regie`; one session, "bench" (S2), with T1 merged.

## Next step

Invoke `superpowers:writing-plans` for **milestone 2** from the design (section 1 "The lead", the parts of 3, 6 and 7 it needs, section 2's revisit) and the interfaces map. Then run it with `superpowers:subagent-driven-development`, as for milestone 1 (Frédéric chose subagents; commit on `main`, never push).

Milestone 2 scope (design section 9, item 2):
- the lead conversation (`claude -p --resume` per wake), Régie's MCP endpoint and the lead's tools, the wake rules, replacement at 200k tokens or after 1 hour idle;
- the Lead view, the talkback (Needs me) queue, escalations, browser notifications;
- onboarding (`propose_onboarding` → confirmation → project facts), the preflight check, Jira updates by the lead itself;
- several sessions (they already work);
- `regie settings` for the level map, and the per-task model override.

Known gaps from the map: no MCP server, no run without a task or worktree (the lead needs one), no items or queue (Needs me is only a derived flag today), no chat messages, settings fixed at start, `Actor::Lead` never emitted. `auto_rule` at `engine/mod.rs` `advance` is where the lead's ruling plugs in.

## Worth doing first in milestone 2, or just before

- **On a real run, check:** Standby and Resume while an agent works, and `kill -9` of `regie serve` then `regie` (milestone 1 tested these with fakes only).
- **Parked from the final review:** the 60-second limit on every git call (a large or LFS repo can time out on fetch or `worktree add`, leaving a branch that breaks Retry).
- **Decide how the event schema evolves** before milestone 2 adds event kinds, so existing `~/.regie` logs keep replaying.

## How Frédéric works

Plain, concise messages; he decides quickly and accepts recommendations with clear trade-offs. Agents must be autonomous, not babysat. Ask him before a reviewer's safety concern becomes a requirement, and say where it came from.
