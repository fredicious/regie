//! Which board lane a task sits in, and the flags on its card.

use serde::{Deserialize, Serialize};
use ts_rs::TS;

use crate::event::Role;
use crate::flow::{HumanReason, NextStep};
use crate::github::{Checks, MergeState, PrLifecycle};
use crate::scheduler::QueuedReason;
use crate::state::Task;

/// The board's columns, in order.
#[derive(Serialize, Deserialize, TS, Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
#[ts(export)]
pub enum Lane {
    Queued,
    Working,
    InReview,
    Ci,
    ReadyToMerge,
    Merged,
    Cancelled,
}

#[derive(Serialize, Deserialize, TS, Clone, Copy, Debug, PartialEq, Eq)]
#[ts(export)]
pub enum Flag {
    NeedsMe(HumanReason),
    Paused,
    Waiting(QueuedReason),
}

/// The lane and flags of one task. The rules are ordered; the first that matches wins.
pub fn lane(
    task: &Task,
    active_role: Option<Role>,
    next: &NextStep,
    session_paused: bool,
    waiting: Option<QueuedReason>,
) -> (Lane, Vec<Flag>) {
    let mut flags = Vec::new();
    let waiting_flag = waiting.map(Flag::Waiting);
    let lane = place(task, active_role, next, waiting_flag, &mut flags);
    if session_paused {
        flags.push(Flag::Paused);
    }
    (lane, flags)
}

fn place(
    task: &Task,
    active_role: Option<Role>,
    next: &NextStep,
    waiting: Option<Flag>,
    flags: &mut Vec<Flag>,
) -> Lane {
    let pr = task.pr.as_ref();
    // 1. Finished.
    if pr.is_some_and(|pr| pr.lifecycle == PrLifecycle::Merged) {
        return Lane::Merged;
    }
    if task.cancelled.is_some() {
        return Lane::Cancelled;
    }
    // 2. A run is active.
    match active_role {
        Some(Role::Builder) => return Lane::Working,
        Some(Role::Reviewer | Role::Verify) => return Lane::InReview,
        None => {}
    }
    // 3. No builder run was ever started.
    if task.worktree.is_none() {
        flags.extend(waiting);
        return Lane::Queued;
    }
    // 4. The next run is about to start, or a ruling is due.
    match next {
        NextStep::StartRun { role: Role::Builder, .. } => {
            flags.extend(waiting);
            return Lane::Working;
        }
        NextStep::StartRun { .. } | NextStep::AwaitRuling { .. } => {
            flags.extend(waiting);
            return Lane::InReview;
        }
        // 5. Waiting for Frédéric.
        NextStep::AwaitHuman(reason) => {
            flags.push(Flag::NeedsMe(*reason));
            return match reason {
                HumanReason::ReviewUnparsed | HumanReason::VerifyFailed | HumanReason::ChangesRequested => {
                    Lane::InReview
                }
                _ => Lane::Working,
            };
        }
        _ => {}
    }
    // 6. An open PR.
    if let Some(pr) = pr.filter(|pr| pr.lifecycle == PrLifecycle::Open) {
        let green = pr.checks == Checks::Success;
        return if green && !pr.conflicting && pr.merge_state == MergeState::Clean {
            Lane::ReadyToMerge
        } else if green && pr.merge_state == MergeState::Blocked {
            // Waiting for a required GitHub approval.
            Lane::InReview
        } else {
            Lane::Ci
        };
    }
    // 7. Otherwise.
    Lane::Queued
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::event::{Actor, Event, EventKind, Purpose, RepoInfo};
    use crate::github::PrState;
    use crate::ids::{EventId, Millis, SessionId, TaskId};
    use crate::settings::Level;
    use crate::state::{State, Worktree};

    pub(crate) fn event(id: i64, task: Option<TaskId>, kind: EventKind) -> Event {
        Event { id: EventId(id), at: Millis(id), session: Some(SessionId(1)), task, actor: Actor::Regie, kind }
    }

    /// A fresh task (created, no worktree) from a replayed `TaskCreated`.
    fn task() -> Task {
        let events = [
            event(1, None, EventKind::SessionCreated { name: "s".into(), repos: vec![RepoInfo { path: "/r".into(), github: Some("o/r".into()) }] }),
            event(
                2,
                Some(TaskId(1)),
                EventKind::TaskCreated {
                    repo: "/r".into(),
                    title: "t".into(),
                    instruction: "i".into(),
                    tickets: vec![],
                    blocked_by: vec![],
                    level: Level::Standard,
                    paths: vec![],
                    provider: None,
                },
            ),
        ];
        State::replay(&events).tasks.remove(&TaskId(1)).unwrap()
    }

    fn with_worktree(mut t: Task) -> Task {
        t.worktree = Some(Worktree { path: "/w".into(), branch: "b".into(), port_base: 41000 });
        t
    }

    fn pr(checks: Checks, merge_state: MergeState, conflicting: bool, lifecycle: PrLifecycle) -> PrState {
        PrState {
            number: 7,
            url: "u".into(),
            lifecycle,
            checks,
            conflicting,
            merge_state,
            review_decision: None,
            head_sha: "abc".into(),
        }
    }

    fn with_pr(mut t: Task, p: PrState) -> Task {
        t.pr = Some(p);
        t
    }

    fn start(role: Role) -> NextStep {
        let purpose = if role == Role::Builder { Purpose::Fix } else { Purpose::Review };
        NextStep::StartRun { role, purpose, input: crate::flow::PromptInput::Initial }
    }

    #[test]
    fn lane_precedence_table() {
        use QueuedReason::Slot;
        let open = |c, m, x| pr(c, m, x, PrLifecycle::Open);
        let mut cancelled = with_worktree(task());
        cancelled.cancelled = Some(false);
        let merged_and_cancelled = {
            let mut t = with_pr(with_worktree(task()), pr(Checks::Success, MergeState::Clean, false, PrLifecycle::Merged));
            t.cancelled = Some(false);
            t
        };
        let nothing = NextStep::Nothing;
        #[allow(clippy::type_complexity)]
        let rows: Vec<(&str, Task, Option<Role>, NextStep, bool, Option<QueuedReason>, Lane, Vec<Flag>)> = vec![
            // 1. merged wins over everything, even cancelled and an active run
            ("merged", merged_and_cancelled, Some(Role::Builder), nothing.clone(), false, None, Lane::Merged, vec![]),
            ("cancelled", cancelled, Some(Role::Builder), nothing.clone(), false, None, Lane::Cancelled, vec![]),
            // 2. active role
            ("active builder", with_worktree(task()), Some(Role::Builder), nothing.clone(), false, None, Lane::Working, vec![]),
            ("active reviewer", with_worktree(task()), Some(Role::Reviewer), nothing.clone(), false, None, Lane::InReview, vec![]),
            ("active verify", with_worktree(task()), Some(Role::Verify), nothing.clone(), false, None, Lane::InReview, vec![]),
            // 3. no worktree
            ("no worktree", task(), None, start(Role::Builder), false, None, Lane::Queued, vec![]),
            ("no worktree waiting", task(), None, start(Role::Builder), false, Some(Slot), Lane::Queued, vec![Flag::Waiting(Slot)]),
            // 4. next step
            ("start builder", with_worktree(task()), None, start(Role::Builder), false, None, Lane::Working, vec![]),
            ("start builder waiting", with_worktree(task()), None, start(Role::Builder), false, Some(Slot), Lane::Working, vec![Flag::Waiting(Slot)]),
            ("start reviewer", with_worktree(task()), None, start(Role::Reviewer), false, None, Lane::InReview, vec![]),
            ("start verify", with_worktree(task()), None, start(Role::Verify), false, Some(Slot), Lane::InReview, vec![Flag::Waiting(Slot)]),
            ("await ruling", with_worktree(task()), None, NextStep::AwaitRuling { round: 1 }, false, None, Lane::InReview, vec![]),
            // 5. await human
            ("human review unparsed", with_worktree(task()), None, NextStep::AwaitHuman(HumanReason::ReviewUnparsed), false, None, Lane::InReview, vec![Flag::NeedsMe(HumanReason::ReviewUnparsed)]),
            ("human verify failed", with_worktree(task()), None, NextStep::AwaitHuman(HumanReason::VerifyFailed), false, None, Lane::InReview, vec![Flag::NeedsMe(HumanReason::VerifyFailed)]),
            ("human changes requested", with_worktree(task()), None, NextStep::AwaitHuman(HumanReason::ChangesRequested), false, None, Lane::InReview, vec![Flag::NeedsMe(HumanReason::ChangesRequested)]),
            ("human question", with_worktree(task()), None, NextStep::AwaitHuman(HumanReason::BuilderQuestion), false, None, Lane::Working, vec![Flag::NeedsMe(HumanReason::BuilderQuestion)]),
            ("human crashed", with_worktree(task()), None, NextStep::AwaitHuman(HumanReason::RunCrashed), false, None, Lane::Working, vec![Flag::NeedsMe(HumanReason::RunCrashed)]),
            // 6. open PR
            ("pr ready", with_pr(with_worktree(task()), open(Checks::Success, MergeState::Clean, false)), None, nothing.clone(), false, None, Lane::ReadyToMerge, vec![]),
            ("pr green, blocked", with_pr(with_worktree(task()), open(Checks::Success, MergeState::Blocked, false)), None, nothing.clone(), false, None, Lane::InReview, vec![]),
            ("pr conflicting", with_pr(with_worktree(task()), open(Checks::Success, MergeState::Clean, true)), None, nothing.clone(), false, None, Lane::Ci, vec![]),
            ("pr pending", with_pr(with_worktree(task()), open(Checks::Pending, MergeState::Blocked, false)), None, nothing.clone(), false, None, Lane::Ci, vec![]),
            ("pr failing", with_pr(with_worktree(task()), open(Checks::Failure { failed: vec!["x".into()] }, MergeState::Unstable, false)), None, nothing.clone(), false, None, Lane::Ci, vec![]),
            // 7. otherwise
            ("nothing, no pr", with_worktree(task()), None, nothing.clone(), false, None, Lane::Queued, vec![]),
            ("closed pr", with_pr(with_worktree(task()), pr(Checks::Success, MergeState::Clean, false, PrLifecycle::Closed)), None, nothing, false, None, Lane::Queued, vec![]),
        ];
        for (name, t, role, next, paused, waiting, want_lane, want_flags) in rows {
            assert_eq!(lane(&t, role, &next, paused, waiting), (want_lane, want_flags), "{name}");
        }
    }

    #[test]
    fn needs_me_flag_on_await_human() {
        let t = with_worktree(task());
        let (l, flags) = lane(&t, None, &NextStep::AwaitHuman(HumanReason::PrNotFound), false, None);
        assert_eq!((l, flags), (Lane::Working, vec![Flag::NeedsMe(HumanReason::PrNotFound)]));
    }

    #[test]
    fn ready_to_merge_needs_clean_merge_state() {
        let t = |m| with_pr(with_worktree(task()), pr(Checks::Success, m, false, PrLifecycle::Open));
        assert_eq!(lane(&t(MergeState::Clean), None, &NextStep::Nothing, false, None).0, Lane::ReadyToMerge);
        assert_eq!(lane(&t(MergeState::Blocked), None, &NextStep::Nothing, false, None).0, Lane::InReview);
    }

    #[test]
    fn paused_flag_added() {
        let (l, flags) = lane(&task(), None, &start(Role::Builder), true, Some(QueuedReason::Paused));
        assert_eq!(l, Lane::Queued);
        assert_eq!(flags, vec![Flag::Waiting(QueuedReason::Paused), Flag::Paused]);
        let (_, flags) = lane(&with_worktree(task()), Some(Role::Builder), &NextStep::Nothing, true, None);
        assert_eq!(flags, vec![Flag::Paused]);
    }
}
