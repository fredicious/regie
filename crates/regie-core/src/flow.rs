//! The task flow: what a task needs next, decided from its state alone.

use serde::{Deserialize, Serialize};
use ts_rs::TS;

use crate::event::{LoopKind, Purpose, Role, RunOutcome};
use crate::findings::{BuilderStatus, Decision, Finding, ReviewOutcome, VerifyOutcome, VerifyStatus};
use crate::github::{Checks, PrLifecycle, PrState, ReviewDecision};
use crate::settings::Settings;
use crate::state::{ReviewRecord, State, Task};

/// Why a task waits for Frédéric.
#[derive(Serialize, Deserialize, TS, Clone, Copy, Debug, PartialEq, Eq)]
#[ts(export)]
pub enum HumanReason {
    BuilderQuestion,
    SetupFailed,
    ReviewUnparsed,
    VerifyFailed,
    CapReached(LoopKind),
    ChangesRequested,
    PrNotFound,
    PrClosed,
    FixNotPushed(LoopKind),
    RunCrashed,
    AgentStopped,
}

/// What the next run's prompt is built from.
#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub enum PromptInput {
    Initial,
    Fix { findings: Vec<Finding> },
    OpenPr,
    CiFix { failed: Vec<String> },
    ConflictFix,
    Message { text: String },
    Resume { note: String },
    Review { round: u32 },
    Verify { findings: Vec<Finding> },
}

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub enum NextStep {
    Nothing,
    StartRun { role: Role, purpose: Purpose, input: PromptInput },
    AwaitRuling { round: u32 },
    AwaitHuman(HumanReason),
    ClosePr,
    Cleanup,
}

/// The note a resumed run gets.
pub const RESUME_NOTE: &str =
    "You were interrupted. Continue where you left off; check the state of the worktree first.";

/// What the task needs next. The rules are ordered; the first that matches wins.
pub fn next_step(task: &Task, state: &State, settings: &Settings) -> NextStep {
    let lifecycle = task.pr.as_ref().map(|pr| pr.lifecycle);
    let cleanup = || if task.cleaned { NextStep::Nothing } else { NextStep::Cleanup };

    // 1. Cancelled: close the PR if asked, then clean up.
    if let Some(close_pr) = task.cancelled {
        if close_pr && lifecycle == Some(PrLifecycle::Open) && !task.pr_closed_by_regie {
            return NextStep::ClosePr;
        }
        return cleanup();
    }
    // 2. Merged.
    if lifecycle == Some(PrLifecycle::Merged) {
        return cleanup();
    }
    // 3. A run is active.
    if state.active_run(task.id).is_some() {
        return NextStep::Nothing;
    }
    // 4. Setup failed.
    if task.setup_ok == Some(false) {
        return NextStep::AwaitHuman(HumanReason::SetupFailed);
    }
    // 5. The last run did not finish.
    match task.last_run_outcome {
        Some(RunOutcome::Crashed) => return NextStep::AwaitHuman(HumanReason::RunCrashed),
        Some(RunOutcome::Stopped) => return NextStep::AwaitHuman(HumanReason::AgentStopped),
        Some(RunOutcome::Interrupted | RunOutcome::Paused | RunOutcome::LimitHit) => {
            // R11: reviewer and verify runs are always fresh agents, so they restart.
            return match state.last_run(task.id).map_or(Role::Builder, |r| r.role) {
                Role::Builder => start(Role::Builder, Purpose::Resume, PromptInput::Resume { note: RESUME_NOTE.into() }),
                Role::Reviewer => {
                    let round = task.reviews.last().map_or(1, |r| r.round + 1);
                    start(Role::Reviewer, Purpose::Review, PromptInput::Review { round })
                }
                Role::Verify => {
                    let findings = task.reviews.last().map(to_fix).unwrap_or_default();
                    start(Role::Verify, Purpose::Verify, PromptInput::Verify { findings })
                }
            };
        }
        _ => {}
    }
    // 6. A pending message (after the first builder run, which gets the instruction).
    if let Some(text) = task.pending_messages.first().filter(|_| state.builder_started(task.id)) {
        return start(Role::Builder, Purpose::Message, PromptInput::Message { text: text.clone() });
    }
    // 7. The builder asked a question.
    if task.last_builder_status == Some(BuilderStatus::Question) {
        return NextStep::AwaitHuman(HumanReason::BuilderQuestion);
    }
    // 8. No builder run yet.
    if !state.builder_started(task.id) {
        return start(Role::Builder, Purpose::Initial, PromptInput::Initial);
    }
    // 9. Review, until closed.
    if let Some(step) = review_step(task, settings.loops.review_rounds) {
        return step;
    }
    // 10. Open the PR.
    if !task.open_pr_requested {
        return start(Role::Builder, Purpose::OpenPr, PromptInput::OpenPr);
    }
    // 11. The PR is missing or closed without a merge.
    if task.pr_missing {
        return NextStep::AwaitHuman(HumanReason::PrNotFound);
    }
    let Some(pr) = task.pr.as_ref().filter(|_| lifecycle == Some(PrLifecycle::Open)) else {
        return match lifecycle {
            Some(PrLifecycle::Closed) => NextStep::AwaitHuman(HumanReason::PrClosed),
            _ => NextStep::Nothing,
        };
    };
    // 12. The open PR needs a fix or a human.
    if pr.conflicting {
        let input = PromptInput::ConflictFix;
        return fix_loop(task, pr, LoopKind::Conflict, settings.loops.conflict, Purpose::ConflictFix, input);
    }
    if let Checks::Failure { failed } = &pr.checks {
        let input = PromptInput::CiFix { failed: failed.clone() };
        return fix_loop(task, pr, LoopKind::Ci, settings.loops.ci, Purpose::CiFix, input);
    }
    if pr.review_decision == Some(ReviewDecision::ChangesRequested) {
        return NextStep::AwaitHuman(HumanReason::ChangesRequested);
    }
    // 13.
    NextStep::Nothing
}

fn start(role: Role, purpose: Purpose, input: PromptInput) -> NextStep {
    NextStep::StartRun { role, purpose, input }
}

/// A post-PR fix: refuse to repeat a fix that pushed nothing, and stop at the cap.
fn fix_loop(task: &Task, pr: &PrState, kind: LoopKind, cap: u32, purpose: Purpose, input: PromptInput) -> NextStep {
    if task.fixed_shas.get(&kind) == Some(&pr.head_sha) {
        NextStep::AwaitHuman(HumanReason::FixNotPushed(kind))
    } else if task.loops.get(kind) < cap {
        start(Role::Builder, purpose, input)
    } else {
        NextStep::AwaitHuman(HumanReason::CapReached(kind))
    }
}

/// The review phase's step, or `None` once review is closed.
fn review_step(task: &Task, rounds: u32) -> Option<NextStep> {
    let review = |round| start(Role::Reviewer, Purpose::Review, PromptInput::Review { round });
    let Some(latest) = task.reviews.last() else { return Some(review(1)) };
    if latest.fix_done && latest.round < rounds {
        return Some(review(latest.round + 1));
    }
    let findings = match &latest.outcome {
        ReviewOutcome::Unparsed { .. } => return Some(NextStep::AwaitHuman(HumanReason::ReviewUnparsed)),
        ReviewOutcome::Findings(f) => f,
    };
    let Some(rulings) = &latest.rulings else {
        // Unruled: blocking findings wait for a ruling; anything else is clean or advisory.
        return findings.iter().any(|f| f.blocking).then_some(NextStep::AwaitRuling { round: latest.round });
    };
    // No `Fix` ruling (all advisory or out of scope) closes review.
    if !rulings.iter().any(|r| r.decision == Decision::Fix) {
        return None;
    }
    let to_fix = to_fix(latest);
    if !latest.fix_done {
        return Some(start(Role::Builder, Purpose::Fix, PromptInput::Fix { findings: to_fix }));
    }
    match &task.verify {
        None => Some(start(Role::Verify, Purpose::Verify, PromptInput::Verify { findings: to_fix })),
        Some(v) if verified(v, &to_fix) => None,
        Some(_) => Some(NextStep::AwaitHuman(HumanReason::VerifyFailed)),
    }
}

/// The round's findings ruled `Fix`.
pub(crate) fn to_fix(review: &ReviewRecord) -> Vec<Finding> {
    let (ReviewOutcome::Findings(findings), Some(rulings)) = (&review.outcome, &review.rulings) else {
        return Vec::new();
    };
    findings
        .iter()
        .filter(|f| rulings.iter().any(|r| r.finding == f.id && r.decision == Decision::Fix))
        .cloned()
        .collect()
}

/// R8: every finding sent to verify has a result, and every result is `Fixed`.
pub(crate) fn verified(outcome: &VerifyOutcome, sent: &[Finding]) -> bool {
    let VerifyOutcome::Results(results) = outcome else { return false };
    results.iter().all(|r| r.status == VerifyStatus::Fixed)
        && sent.iter().all(|f| results.iter().any(|r| r.finding == f.id))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::event::RunOutcome;
    use crate::findings::{
        BuilderStatus, Decision, FindingKind, ReviewOutcome, Ruling, VerifyOutcome, VerifyResult, VerifyStatus,
    };
    use crate::github::{Checks, MergeState, PrLifecycle, PrState, ReviewDecision};
    use crate::ids::{Millis, RunId, SessionId, TaskId};
    use crate::settings::{Effort, Level, Provider};
    use crate::state::{LoopCounts, ReviewRecord, Run};
    use std::collections::BTreeMap;

    /// Builds a task (and the state holding its runs) for one `next_step` call.
    struct TaskB {
        task: Task,
        state: State,
    }

    impl TaskB {
        fn new() -> Self {
            let task = Task {
                id: TaskId(1),
                session: SessionId(1),
                repo: "/r".into(),
                title: "t".into(),
                instruction: "do it".into(),
                tickets: vec![],
                blocked_by: vec![],
                level: Level::Standard,
                paths: vec![],
                created_at: Millis(0),
                worktree: None,
                setup_ok: None,
                setup_output: String::new(),
                builder_native_session: None,
                last_builder_status: None,
                pending_messages: vec![],
                reviews: vec![],
                verify: None,
                open_pr_requested: false,
                pr: None,
                pr_missing: false,
                comments: vec![],
                loops: LoopCounts::default(),
                fixed_shas: BTreeMap::new(),
                cancelled: None,
                pr_closed_by_regie: false,
                cleaned: false,
                last_run_outcome: None,
            };
            TaskB { task, state: State::default() }
        }

        /// Adds a run; `outcome` None leaves it active.
        fn run(mut self, role: Role, purpose: Purpose, outcome: Option<RunOutcome>) -> Self {
            let id = RunId(self.state.runs.len() as i64 + 1);
            self.state.runs.insert(
                id,
                Run {
                    id,
                    task: self.task.id,
                    role,
                    purpose,
                    provider: Provider::Claude,
                    model: "m".into(),
                    effort: Effort::Medium,
                    native_session: None,
                    pid: None,
                    started_at: Millis(0),
                    outcome,
                    final_message: None,
                },
            );
            if outcome.is_some() {
                self.task.last_run_outcome = outcome;
            }
            self
        }

        /// The builder finished its initial run with `Status: done`.
        fn built(self) -> Self {
            self.run(Role::Builder, Purpose::Initial, Some(RunOutcome::Finished)).status(BuilderStatus::Done)
        }

        fn status(mut self, s: BuilderStatus) -> Self {
            self.task.last_builder_status = Some(s);
            self
        }

        fn message(mut self, text: &str) -> Self {
            self.task.pending_messages.push(text.into());
            self
        }

        fn review(mut self, round: u32, findings: Vec<Finding>) -> Self {
            self.task.reviews.push(ReviewRecord {
                round,
                outcome: ReviewOutcome::Findings(findings),
                rulings: None,
                fix_done: false,
            });
            self
        }

        fn unparsed_review(mut self, round: u32) -> Self {
            self.task.reviews.push(ReviewRecord {
                round,
                outcome: ReviewOutcome::Unparsed { raw: "lgtm".into() },
                rulings: None,
                fix_done: false,
            });
            self
        }

        /// Rules the latest review: `(finding id, decision)`.
        fn ruled(mut self, decisions: &[(u32, Decision)]) -> Self {
            let rulings =
                decisions.iter().map(|&(finding, decision)| Ruling { finding, decision, note: String::new() });
            self.task.reviews.last_mut().unwrap().rulings = Some(rulings.collect());
            self
        }

        fn fix_done(mut self) -> Self {
            self.task.reviews.last_mut().unwrap().fix_done = true;
            self
        }

        /// Two review rounds, each with one blocking finding ruled Fix and fixed.
        fn two_rounds_fixed(self) -> Self {
            self.built()
                .review(1, vec![finding(1, true)])
                .ruled(&[(1, Decision::Fix)])
                .fix_done()
                .review(2, vec![finding(1, true), finding(2, false)])
                .ruled(&[(1, Decision::Fix), (2, Decision::Advisory)])
                .fix_done()
        }

        fn verify(mut self, results: &[(u32, VerifyStatus)]) -> Self {
            let r = results.iter().map(|&(finding, status)| VerifyResult { finding, status, note: String::new() });
            self.task.verify = Some(VerifyOutcome::Results(r.collect()));
            self
        }

        /// A clean review closed, the PR asked for and observed open with pending checks.
        fn with_pr(self, f: impl FnOnce(&mut PrState)) -> Self {
            let mut b = self.built().review(1, vec![]).with(|t| t.open_pr_requested = true);
            let mut pr = PrState {
                number: 7,
                url: "https://github.com/o/r/pull/7".into(),
                lifecycle: PrLifecycle::Open,
                checks: Checks::Pending,
                conflicting: false,
                merge_state: MergeState::Clean,
                review_decision: None,
                head_sha: "sha1".into(),
            };
            f(&mut pr);
            b.task.pr = Some(pr);
            b
        }

        fn with(mut self, f: impl FnOnce(&mut Task)) -> Self {
            f(&mut self.task);
            self
        }
    }

    fn step(b: TaskB) -> NextStep {
        next_step(&b.task, &b.state, &Settings::default())
    }

    fn start(role: Role, purpose: Purpose) -> NextStep {
        let input = match purpose {
            Purpose::Initial => PromptInput::Initial,
            Purpose::OpenPr => PromptInput::OpenPr,
            Purpose::ConflictFix => PromptInput::ConflictFix,
            other => panic!("start() has no default input for {other:?}"),
        };
        NextStep::StartRun { role, purpose, input }
    }

    fn human(r: HumanReason) -> NextStep {
        NextStep::AwaitHuman(r)
    }

    fn finding(id: u32, blocking: bool) -> Finding {
        Finding { id, title: format!("f{id}"), kind: FindingKind::Bug, blocking, evidence: None }
    }

    fn review_round(round: u32) -> NextStep {
        NextStep::StartRun { role: Role::Reviewer, purpose: Purpose::Review, input: PromptInput::Review { round } }
    }

    #[test]
    fn new_task_starts_builder() {
        assert_eq!(step(TaskB::new()), start(Role::Builder, Purpose::Initial));
    }

    #[test]
    fn active_run_waits() {
        assert_eq!(step(TaskB::new().run(Role::Builder, Purpose::Initial, None)), NextStep::Nothing);
        assert_eq!(step(TaskB::new().built().run(Role::Reviewer, Purpose::Review, None)), NextStep::Nothing);
        // even with a pending message: it is delivered live, not as a new run
        assert_eq!(
            step(TaskB::new().run(Role::Builder, Purpose::Initial, None).message("hi")),
            NextStep::Nothing
        );
    }

    #[test]
    fn builder_question_needs_human() {
        let b = TaskB::new().built().status(BuilderStatus::Question);
        assert_eq!(step(b), human(HumanReason::BuilderQuestion));
    }

    #[test]
    fn message_resumes_builder_even_after_question() {
        let b = TaskB::new().built().status(BuilderStatus::Question).message("use sqlite").message("and tests");
        assert_eq!(
            step(b),
            NextStep::StartRun {
                role: Role::Builder,
                purpose: Purpose::Message,
                input: PromptInput::Message { text: "use sqlite".into() },
            }
        );
    }

    #[test]
    fn message_before_the_first_run_waits_for_it() {
        // The first run gets the task's instruction; the message follows it.
        assert_eq!(step(TaskB::new().message("use sqlite")), start(Role::Builder, Purpose::Initial));
    }

    #[test]
    fn done_builder_goes_to_review_round_1() {
        assert_eq!(step(TaskB::new().built()), review_round(1));
    }

    #[test]
    fn clean_review_opens_pr() {
        assert_eq!(step(TaskB::new().built().review(1, vec![])), start(Role::Builder, Purpose::OpenPr));
        // advisory-only findings with no rulings, and all-advisory / out-of-scope rulings, close review too
        assert_eq!(
            step(TaskB::new().built().review(1, vec![finding(1, false)])),
            start(Role::Builder, Purpose::OpenPr)
        );
        let b = TaskB::new()
            .built()
            .review(1, vec![finding(1, true), finding(2, false)])
            .ruled(&[(1, Decision::OutOfScope), (2, Decision::Advisory)]);
        assert_eq!(step(b), start(Role::Builder, Purpose::OpenPr));
        // once asked for, the PR is not asked for again
        let b = TaskB::new().built().review(1, vec![]).with(|t| t.open_pr_requested = true);
        assert_eq!(step(b), NextStep::Nothing);
    }

    #[test]
    fn blocking_findings_await_ruling() {
        let b = TaskB::new().built().review(1, vec![finding(1, false), finding(2, true)]);
        assert_eq!(step(b), NextStep::AwaitRuling { round: 1 });
    }

    #[test]
    fn ruled_fix_goes_to_builder_then_round_2() {
        let ruled = || {
            TaskB::new()
                .built()
                .review(1, vec![finding(1, true), finding(2, false), finding(3, true)])
                .ruled(&[(1, Decision::Fix), (2, Decision::Advisory), (3, Decision::Fix)])
        };
        assert_eq!(
            step(ruled()),
            NextStep::StartRun {
                role: Role::Builder,
                purpose: Purpose::Fix,
                input: PromptInput::Fix { findings: vec![finding(1, true), finding(3, true)] },
            }
        );
        assert_eq!(step(ruled().fix_done()), review_round(2));
    }

    #[test]
    fn round_2_fix_goes_to_verify() {
        assert_eq!(
            step(TaskB::new().two_rounds_fixed()),
            NextStep::StartRun {
                role: Role::Verify,
                purpose: Purpose::Verify,
                input: PromptInput::Verify { findings: vec![finding(1, true)] },
            }
        );
    }

    #[test]
    fn verify_all_fixed_closes_review() {
        let b = TaskB::new().two_rounds_fixed().verify(&[(1, VerifyStatus::Fixed)]);
        assert_eq!(step(b), start(Role::Builder, Purpose::OpenPr));
    }

    #[test]
    fn verify_not_fixed_needs_human() {
        for results in [
            &[(1, VerifyStatus::NotFixed)][..],
            &[(1, VerifyStatus::Disputed)][..],
            // R8: a finding sent to verify without a result counts as not fixed
            &[][..],
            &[(2, VerifyStatus::Fixed)][..],
            &[(1, VerifyStatus::Fixed), (2, VerifyStatus::NotFixed)][..],
        ] {
            let b = TaskB::new().two_rounds_fixed().verify(results);
            assert_eq!(step(b), human(HumanReason::VerifyFailed), "{results:?}");
        }
        let b = TaskB::new().two_rounds_fixed().with(|t| t.verify = Some(VerifyOutcome::Unparsed { raw: "?".into() }));
        assert_eq!(step(b), human(HumanReason::VerifyFailed));
    }

    #[test]
    fn unparsed_review_needs_human() {
        assert_eq!(step(TaskB::new().built().unparsed_review(1)), human(HumanReason::ReviewUnparsed));
    }

    #[test]
    fn review_rounds_come_from_settings() {
        let b = TaskB::new().two_rounds_fixed();
        let mut s = Settings::default();
        s.loops.review_rounds = 3;
        assert_eq!(next_step(&b.task, &b.state, &s), review_round(3));
    }

    #[test]
    fn ci_failure_fixes_then_caps() {
        let failing = |pr: &mut PrState| pr.checks = Checks::Failure { failed: vec!["test".into()] };
        assert_eq!(
            step(TaskB::new().with_pr(failing)),
            NextStep::StartRun {
                role: Role::Builder,
                purpose: Purpose::CiFix,
                input: PromptInput::CiFix { failed: vec!["test".into()] },
            }
        );
        let capped = TaskB::new().with_pr(failing).with(|t| {
            t.loops.ci = 3;
            t.fixed_shas.insert(LoopKind::Ci, "older".into());
        });
        assert_eq!(step(capped), human(HumanReason::CapReached(LoopKind::Ci)));
    }

    #[test]
    fn ci_failure_same_sha_after_fix_needs_human() {
        let b = TaskB::new()
            .with_pr(|pr| pr.checks = Checks::Failure { failed: vec![] })
            .with(|t| {
                t.loops.ci = 1;
                t.fixed_shas.insert(LoopKind::Ci, "sha1".into());
            });
        assert_eq!(step(b), human(HumanReason::FixNotPushed(LoopKind::Ci)));
    }

    #[test]
    fn conflict_fix() {
        assert_eq!(
            step(TaskB::new().with_pr(|pr| pr.conflicting = true)),
            start(Role::Builder, Purpose::ConflictFix)
        );
        let same_sha = TaskB::new()
            .with_pr(|pr| pr.conflicting = true)
            .with(|t| _ = t.fixed_shas.insert(LoopKind::Conflict, "sha1".into()));
        assert_eq!(step(same_sha), human(HumanReason::FixNotPushed(LoopKind::Conflict)));
        let capped = TaskB::new().with_pr(|pr| pr.conflicting = true).with(|t| t.loops.conflict = 3);
        assert_eq!(step(capped), human(HumanReason::CapReached(LoopKind::Conflict)));
        // a conflict comes before a CI failure
        let both = TaskB::new().with_pr(|pr| {
            pr.conflicting = true;
            pr.checks = Checks::Failure { failed: vec![] };
        });
        assert_eq!(step(both), start(Role::Builder, Purpose::ConflictFix));
    }

    #[test]
    fn changes_requested_needs_human() {
        let b = TaskB::new().with_pr(|pr| pr.review_decision = Some(ReviewDecision::ChangesRequested));
        assert_eq!(step(b), human(HumanReason::ChangesRequested));
    }

    #[test]
    fn pr_green_waits() {
        let b = TaskB::new().with_pr(|pr| {
            pr.checks = Checks::Success;
            pr.merge_state = MergeState::Clean;
            pr.review_decision = Some(ReviewDecision::Approved);
        });
        assert_eq!(step(b), NextStep::Nothing);
    }

    #[test]
    fn pr_missing_needs_human() {
        let b = TaskB::new().built().review(1, vec![]).with(|t| {
            t.open_pr_requested = true;
            t.pr_missing = true;
        });
        assert_eq!(step(b), human(HumanReason::PrNotFound));
    }

    #[test]
    fn pr_closed_outside_regie_needs_human() {
        let b = TaskB::new().with_pr(|pr| pr.lifecycle = PrLifecycle::Closed);
        assert_eq!(step(b), human(HumanReason::PrClosed));
    }

    #[test]
    fn merged_cleans_up_once() {
        let merged = |pr: &mut PrState| pr.lifecycle = PrLifecycle::Merged;
        assert_eq!(step(TaskB::new().with_pr(merged)), NextStep::Cleanup);
        assert_eq!(step(TaskB::new().with_pr(merged).with(|t| t.cleaned = true)), NextStep::Nothing);
    }

    #[test]
    fn cancel_closes_pr_then_cleans_up() {
        let cancelled = |close_pr| TaskB::new().with_pr(|_| {}).with(move |t| t.cancelled = Some(close_pr));
        assert_eq!(step(cancelled(true)), NextStep::ClosePr);
        assert_eq!(step(cancelled(true).with(|t| t.pr_closed_by_regie = true)), NextStep::Cleanup);
        assert_eq!(step(cancelled(false)), NextStep::Cleanup);
        assert_eq!(
            step(cancelled(true).with(|t| {
                t.pr_closed_by_regie = true;
                t.cleaned = true;
            })),
            NextStep::Nothing
        );
        // already closed on GitHub: nothing to close
        let closed = cancelled(true).with(|t| t.pr.as_mut().unwrap().lifecycle = PrLifecycle::Closed);
        assert_eq!(step(closed), NextStep::Cleanup);
        // no PR yet
        assert_eq!(step(TaskB::new().with(|t| t.cancelled = Some(true))), NextStep::Cleanup);
    }

    #[test]
    fn setup_failure_needs_human() {
        let b = TaskB::new().with(|t| t.setup_ok = Some(false));
        assert_eq!(step(b), human(HumanReason::SetupFailed));
        assert_eq!(step(TaskB::new().with(|t| t.setup_ok = Some(true))), start(Role::Builder, Purpose::Initial));
    }

    #[test]
    fn crash_needs_human_interrupt_resumes() {
        let b = TaskB::new().run(Role::Builder, Purpose::Initial, Some(RunOutcome::Crashed));
        assert_eq!(step(b), human(HumanReason::RunCrashed));
        for outcome in [RunOutcome::Interrupted, RunOutcome::Paused, RunOutcome::LimitHit] {
            let resume = |role| NextStep::StartRun {
                role,
                purpose: Purpose::Resume,
                input: PromptInput::Resume { note: RESUME_NOTE.into() },
            };
            let b = TaskB::new().run(Role::Builder, Purpose::Initial, Some(outcome));
            assert_eq!(step(b), resume(Role::Builder), "{outcome:?}");
        }
        assert_eq!(
            RESUME_NOTE,
            "You were interrupted. Continue where you left off; check the state of the worktree first."
        );
    }

    #[test]
    fn stopped_agent_waits_for_frederic() {
        for role in [Role::Builder, Role::Reviewer] {
            let b = TaskB::new().built().run(role, Purpose::Review, Some(RunOutcome::Stopped));
            assert_eq!(step(b), human(HumanReason::AgentStopped), "{role:?}");
        }
    }

    #[test]
    fn unfinished_reviewer_restarts_fresh_for_the_same_round() {
        for outcome in [RunOutcome::Interrupted, RunOutcome::Paused, RunOutcome::LimitHit] {
            // round 1: nothing reported yet
            let b = TaskB::new().built().run(Role::Reviewer, Purpose::Review, Some(outcome));
            assert_eq!(step(b), review_round(1), "{outcome:?}");
            // round 2: round 1 was reported, ruled and fixed
            let b = TaskB::new()
                .built()
                .review(1, vec![finding(1, true)])
                .ruled(&[(1, Decision::Fix)])
                .fix_done()
                .run(Role::Reviewer, Purpose::Review, Some(outcome));
            assert_eq!(step(b), review_round(2), "{outcome:?}");
        }
    }

    #[test]
    fn unfinished_verify_restarts_fresh_with_the_findings() {
        for outcome in [RunOutcome::Interrupted, RunOutcome::Paused, RunOutcome::LimitHit] {
            let b = TaskB::new().two_rounds_fixed().run(Role::Verify, Purpose::Verify, Some(outcome));
            assert_eq!(
                step(b),
                NextStep::StartRun {
                    role: Role::Verify,
                    purpose: Purpose::Verify,
                    input: PromptInput::Verify { findings: vec![finding(1, true)] },
                },
                "{outcome:?}"
            );
        }
    }
}
