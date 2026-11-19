//! State folded from the event log: sessions, tasks, runs and provider readings.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use ts_rs::TS;

use crate::event::{Event, EventKind, LoopKind, Purpose, RepoInfo, Role, RunOutcome};
use crate::facts::ProjectFacts;
use crate::findings::{parse_builder_status, BuilderStatus, ReviewOutcome, Ruling, VerifyOutcome};
use crate::flow;
use crate::github::{PrComment, PrState, ReviewDecision};
use crate::ids::{Millis, RunId, SessionId, TaskId};
use crate::settings::{Effort, Level, Provider};
use crate::usage::Reading;

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub struct Session {
    pub id: SessionId,
    pub name: String,
    pub repos: Vec<RepoInfo>,
    pub paused: bool,
}

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub struct Worktree {
    pub path: String,
    pub branch: String,
    pub port_base: u16,
    /// The repo's default branch, which the worktree was cut from.
    #[serde(default)]
    pub base: String,
}

/// One review round: the reviewer's outcome, the rulings on it, and whether the fix ran.
#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub struct ReviewRecord {
    pub round: u32,
    pub outcome: ReviewOutcome,
    pub rulings: Option<Vec<Ruling>>,
    pub fix_done: bool,
}

#[derive(Serialize, Deserialize, TS, Clone, Copy, Debug, Default, PartialEq, Eq)]
#[ts(export)]
pub struct LoopCounts {
    pub ci: u32,
    pub conflict: u32,
    pub pr_feedback: u32,
}

impl LoopCounts {
    pub fn get(&self, kind: LoopKind) -> u32 {
        match kind {
            LoopKind::Ci => self.ci,
            LoopKind::Conflict => self.conflict,
            LoopKind::PrFeedback => self.pr_feedback,
        }
    }

    fn bump(&mut self, kind: LoopKind) {
        match kind {
            LoopKind::Ci => self.ci += 1,
            LoopKind::Conflict => self.conflict += 1,
            LoopKind::PrFeedback => self.pr_feedback += 1,
        }
    }
}

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub struct Task {
    pub id: TaskId,
    pub session: SessionId,
    pub repo: String,
    pub title: String,
    pub instruction: String,
    pub tickets: Vec<String>,
    pub blocked_by: Vec<TaskId>,
    pub level: Level,
    pub paths: Vec<String>,
    pub created_at: Millis,
    pub worktree: Option<Worktree>,
    /// When the worktree was made: a PR on the branch older than this belongs
    /// to an earlier task with the same branch name.
    pub worktree_created_at: Option<Millis>,
    pub setup_ok: Option<bool>,
    pub setup_output: String,
    pub builder_native_session: Option<String>,
    pub last_builder_status: Option<BuilderStatus>,
    pub pending_messages: Vec<String>,
    pub reviews: Vec<ReviewRecord>,
    pub verify: Option<VerifyOutcome>,
    pub open_pr_requested: bool,
    pub pr: Option<PrState>,
    pub pr_missing: bool,
    pub comments: Vec<PrComment>,
    pub loops: LoopCounts,
    pub fixed_shas: BTreeMap<LoopKind, String>,
    /// `Some(close_pr)` once cancelled.
    pub cancelled: Option<bool>,
    pub pr_closed_by_regie: bool,
    pub cleaned: bool,
    pub last_run_outcome: Option<RunOutcome>,
}

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub struct Run {
    pub id: RunId,
    pub task: TaskId,
    pub role: Role,
    pub purpose: Purpose,
    pub provider: Provider,
    pub model: String,
    pub effort: Effort,
    pub native_session: Option<String>,
    pub pid: Option<u32>,
    pub started_at: Millis,
    pub outcome: Option<RunOutcome>,
    pub final_message: Option<String>,
}

#[derive(Serialize, Deserialize, TS, Clone, Debug, Default, PartialEq)]
#[ts(export)]
pub struct State {
    pub sessions: BTreeMap<SessionId, Session>,
    pub tasks: BTreeMap<TaskId, Task>,
    pub runs: BTreeMap<RunId, Run>,
    pub project_facts: BTreeMap<String, ProjectFacts>,
    pub paused_all: bool,
    pub readings: BTreeMap<Provider, Reading>,
    pub blocked_until: BTreeMap<Provider, Millis>,
    /// The highest ids ever seen in the log, so a deleted session's, task's or
    /// run's id is never handed out again.
    #[serde(default)]
    #[ts(skip)]
    last_ids: LastIds,
}

#[derive(Serialize, Deserialize, Clone, Copy, Debug, Default, PartialEq, Eq)]
struct LastIds {
    session: i64,
    task: i64,
    run: i64,
}

impl Task {
    /// Merged or cancelled.
    pub fn is_finished(&self) -> bool {
        self.cancelled.is_some()
            || self
                .pr
                .as_ref()
                .is_some_and(|pr| pr.lifecycle == crate::github::PrLifecycle::Merged)
    }
}

impl State {
    pub fn replay(events: &[Event]) -> State {
        let mut s = State::default();
        for e in events {
            s.apply(e);
        }
        s
    }

    pub fn apply(&mut self, e: &Event) {
        if let Some(s) = e.session {
            self.last_ids.session = self.last_ids.session.max(s.0);
        }
        if let Some(t) = e.task {
            self.last_ids.task = self.last_ids.task.max(t.0);
        }
        if let EventKind::RunStarted { run, .. } = &e.kind {
            self.last_ids.run = self.last_ids.run.max(run.0);
        }
        match &e.kind {
            EventKind::SessionDeleted => {
                if let Some(id) = e.session {
                    self.sessions.remove(&id);
                    self.tasks.retain(|_, t| t.session != id);
                    let tasks = &self.tasks;
                    self.runs.retain(|_, r| tasks.contains_key(&r.task));
                }
            }
            EventKind::SessionCreated { name, repos } => {
                if let Some(id) = e.session {
                    let session = Session {
                        id,
                        name: name.clone(),
                        repos: repos.clone(),
                        paused: false,
                    };
                    self.sessions.insert(id, session);
                }
            }
            EventKind::ProjectFactsSet { repo, facts } => {
                self.project_facts.insert(repo.clone(), facts.clone());
            }
            EventKind::TaskCreated {
                repo,
                title,
                instruction,
                tickets,
                blocked_by,
                level,
                paths,
                provider: _,
            } => {
                if let (Some(id), Some(session)) = (e.task, e.session) {
                    let task = Task {
                        id,
                        session,
                        repo: repo.clone(),
                        title: title.clone(),
                        instruction: instruction.clone(),
                        tickets: tickets.clone(),
                        blocked_by: blocked_by.clone(),
                        level: *level,
                        paths: paths.clone(),
                        created_at: e.at,
                        worktree: None,
                        worktree_created_at: None,
                        setup_ok: None,
                        setup_output: String::new(),
                        builder_native_session: None,
                        last_builder_status: None,
                        pending_messages: Vec::new(),
                        reviews: Vec::new(),
                        verify: None,
                        open_pr_requested: false,
                        pr: None,
                        pr_missing: false,
                        comments: Vec::new(),
                        loops: LoopCounts::default(),
                        fixed_shas: BTreeMap::new(),
                        cancelled: None,
                        pr_closed_by_regie: false,
                        cleaned: false,
                        last_run_outcome: None,
                    };
                    self.tasks.insert(id, task);
                }
            }
            EventKind::RunStarted {
                run,
                role,
                purpose,
                provider,
                model,
                effort,
                resume_of: _,
            } => {
                let Some(task) = e.task else { return };
                self.runs.insert(
                    *run,
                    Run {
                        id: *run,
                        task,
                        role: *role,
                        purpose: *purpose,
                        provider: *provider,
                        model: model.clone(),
                        effort: *effort,
                        native_session: None,
                        pid: None,
                        started_at: e.at,
                        outcome: None,
                        final_message: None,
                    },
                );
                if let Some(t) = self.tasks.get_mut(&task) {
                    t.run_started(*purpose);
                }
            }
            EventKind::RunSpawned { run, pid } => {
                if let Some(r) = self.runs.get_mut(run) {
                    r.pid = Some(*pid);
                }
            }
            EventKind::RunAttached {
                run,
                native_session,
                pid,
            } => {
                // A Message run takes its message off the queue only once its chain
                // first attaches: interrupted before that, the message was never sent.
                let takes_message = !self.chain_attached(*run)
                    && self.effective_purpose(*run) == Some(Purpose::Message);
                let Some(r) = self.runs.get_mut(run) else {
                    return;
                };
                r.native_session = Some(native_session.clone());
                r.pid = pid.or(r.pid);
                if r.role == Role::Builder {
                    if let Some(t) = self.tasks.get_mut(&r.task) {
                        t.builder_native_session = Some(native_session.clone());
                        if takes_message && !t.pending_messages.is_empty() {
                            t.pending_messages.remove(0);
                        }
                    }
                }
            }
            EventKind::RunFinished {
                run,
                outcome,
                final_message,
            } => {
                let purpose = self.effective_purpose(*run);
                let Some(r) = self.runs.get_mut(run) else {
                    return;
                };
                r.outcome = Some(*outcome);
                r.final_message = final_message.clone();
                let role = r.role;
                let Some(t) = self.tasks.get_mut(&r.task) else {
                    return;
                };
                t.last_run_outcome = Some(*outcome);
                if role == Role::Builder && *outcome == RunOutcome::Finished {
                    t.last_builder_status =
                        Some(parse_builder_status(final_message.as_deref().unwrap_or("")));
                    if purpose == Some(Purpose::Fix) {
                        if let Some(review) = t.reviews.last_mut() {
                            review.fix_done = true;
                        }
                    }
                }
            }
            EventKind::SessionPaused | EventKind::SessionResumed => {
                if let Some(s) = e.session.and_then(|id| self.sessions.get_mut(&id)) {
                    s.paused = matches!(e.kind, EventKind::SessionPaused);
                }
            }
            EventKind::AllPaused => self.paused_all = true,
            EventKind::AllResumed => self.paused_all = false,
            EventKind::UsageReading { provider, reading } => {
                self.readings.insert(*provider, reading.clone());
            }
            EventKind::LimitHit {
                provider,
                resets_at,
            } => {
                self.blocked_until.insert(*provider, *resets_at);
            }
            // Usage totals are not part of the state.
            EventKind::RunUsage { .. } => {}
            kind => {
                if let Some(t) = e.task.and_then(|id| self.tasks.get_mut(&id)) {
                    t.apply(kind, e.at);
                }
            }
        }
    }

    pub fn next_session_id(&self) -> SessionId {
        SessionId(self.last_ids.session + 1)
    }

    pub fn next_task_id(&self) -> TaskId {
        TaskId(self.last_ids.task + 1)
    }

    pub fn next_run_id(&self) -> RunId {
        RunId(self.last_ids.run + 1)
    }

    /// The task's run that has started and not finished.
    pub fn active_run(&self, task: TaskId) -> Option<&Run> {
        self.runs
            .values()
            .rev()
            .find(|r| r.task == task && r.outcome.is_none())
    }

    /// The task's most recently started run.
    pub fn last_run(&self, task: TaskId) -> Option<&Run> {
        self.runs.values().rev().find(|r| r.task == task)
    }

    /// Whether a builder run was ever started for the task.
    pub fn builder_started(&self, task: TaskId) -> bool {
        self.runs
            .values()
            .any(|r| r.task == task && r.role == Role::Builder)
    }

    /// A run's purpose, looking through `Resume` runs to the run they continue
    /// (the latest earlier non-resume run of the same task and role).
    pub fn effective_purpose(&self, run: RunId) -> Option<Purpose> {
        self.runs
            .get(&self.chain_start(run)?)
            .map(|r| r.purpose)
            .filter(|p| *p != Purpose::Resume)
    }

    /// Whether the run, or an earlier run of the chain it continues, attached to
    /// an agent session (so that session got the chain's prompt).
    pub fn chain_attached(&self, run: RunId) -> bool {
        let (Some(r), Some(start)) = (self.runs.get(&run), self.chain_start(run)) else {
            return false;
        };
        self.runs
            .range(start..=run)
            .map(|(_, p)| p)
            .any(|p| p.task == r.task && p.role == r.role && p.native_session.is_some())
    }

    /// The run a chain of `Resume` runs continues: the run itself unless it is a
    /// resume, else the latest earlier non-resume run of the same task and role.
    fn chain_start(&self, run: RunId) -> Option<RunId> {
        let r = self.runs.get(&run)?;
        if r.purpose != Purpose::Resume {
            return Some(run);
        }
        let start = self
            .runs
            .range(..run)
            .rev()
            .map(|(_, p)| p)
            .find(|p| p.task == r.task && p.role == r.role && p.purpose != Purpose::Resume);
        Some(start.map_or(run, |p| p.id))
    }
}

impl Task {
    fn run_started(&mut self, purpose: Purpose) {
        let fix_loop = match purpose {
            Purpose::Message => {
                // The message leaves the queue when the run attaches (`RunAttached`).
                let changes_requested = self
                    .pr
                    .as_ref()
                    .is_some_and(|pr| pr.review_decision == Some(ReviewDecision::ChangesRequested));
                changes_requested.then_some(LoopKind::PrFeedback)
            }
            Purpose::OpenPr => {
                self.open_pr_requested = true;
                None
            }
            Purpose::CiFix => Some(LoopKind::Ci),
            Purpose::ConflictFix => Some(LoopKind::Conflict),
            _ => None,
        };
        if let Some(kind) = fix_loop {
            self.loops.bump(kind);
            if let Some(pr) = &self.pr {
                self.fixed_shas.insert(kind, pr.head_sha.clone());
            }
        }
    }

    /// R9: a retry while the task waits on a review it could not read, or on a
    /// verify that failed, runs that pass again. Only when nothing comes first:
    /// an unfinished run to resume, a failed setup, a question or a message.
    fn retry_review_or_verify(&mut self) {
        let unfinished = !matches!(self.last_run_outcome, Some(RunOutcome::Finished) | None);
        let waits_first = unfinished
            || self.setup_ok == Some(false)
            || self.last_builder_status == Some(BuilderStatus::Question)
            || !self.pending_messages.is_empty();
        let Some(latest) = self.reviews.last().filter(|_| !waits_first) else {
            return;
        };
        match &latest.outcome {
            ReviewOutcome::Unparsed { .. } => {
                self.reviews.pop();
            }
            ReviewOutcome::Findings(_) => {
                let failed = self
                    .verify
                    .as_ref()
                    .is_some_and(|v| !flow::verified(v, &flow::to_fix(latest)));
                if latest.fix_done && failed {
                    self.verify = None;
                }
            }
        }
    }

    /// Events that touch only this task, recorded at `at`.
    fn apply(&mut self, kind: &EventKind, at: Millis) {
        match kind {
            EventKind::LevelRaised { level } => self.level = *level,
            EventKind::WorktreeCreated {
                path,
                branch,
                port_base,
                base,
            } => {
                let (path, branch, base) = (path.clone(), branch.clone(), base.clone());
                self.worktree = Some(Worktree {
                    path,
                    branch,
                    port_base: *port_base,
                    base,
                });
                self.worktree_created_at = Some(at);
            }
            EventKind::SetupFinished { ok, output_tail } => {
                self.setup_ok = Some(*ok);
                self.setup_output = output_tail.clone();
            }
            EventKind::MessageQueued { text } => self.pending_messages.push(text.clone()),
            EventKind::MessageDelivered => {
                if !self.pending_messages.is_empty() {
                    self.pending_messages.remove(0);
                }
            }
            EventKind::ReviewReported {
                run: _,
                round,
                outcome,
            } => {
                let record = ReviewRecord {
                    round: *round,
                    outcome: outcome.clone(),
                    rulings: None,
                    fix_done: false,
                };
                match self.reviews.iter_mut().find(|r| r.round == *round) {
                    Some(existing) => *existing = record,
                    None => self.reviews.push(record),
                }
            }
            EventKind::FindingsRuled { round, rulings } => {
                if let Some(r) = self.reviews.iter_mut().find(|r| r.round == *round) {
                    r.rulings = Some(rulings.clone());
                }
            }
            EventKind::VerifyReported { run: _, outcome } => self.verify = Some(outcome.clone()),
            EventKind::PrObserved { pr } => {
                self.pr = Some(pr.clone());
                self.pr_missing = false;
            }
            EventKind::PrMissing => self.pr_missing = true,
            EventKind::PrCommentObserved { comment } => {
                if !self.comments.iter().any(|c| c.id == comment.id) {
                    self.comments.push(comment.clone());
                }
            }
            EventKind::TaskCancelled { close_pr } => self.cancelled = Some(*close_pr),
            EventKind::TaskRetried => {
                self.retry_review_or_verify();
                // An unfinished run (crashed, stopped, interrupted...) is resumed by the
                // next step; a finished one's outcome is simply cleared.
                self.last_run_outcome = match self.last_run_outcome {
                    Some(RunOutcome::Finished) | None => None,
                    Some(_) => Some(RunOutcome::Interrupted),
                };
                if self.setup_ok == Some(false) {
                    self.setup_ok = None;
                }
                self.pr_missing = false;
            }
            EventKind::PrClosedByRegie => self.pr_closed_by_regie = true,
            EventKind::WorktreeCleaned => self.cleaned = true,
            _ => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::event::Actor;
    use crate::findings::{Decision, Finding, FindingKind, VerifyResult, VerifyStatus};
    use crate::flow::{next_step, HumanReason, NextStep, PromptInput};
    use crate::github::{Checks, MergeState, PrLifecycle, ReviewDecision};
    use crate::ids::EventId;
    use crate::usage::Window;

    const T: TaskId = TaskId(1);
    const S: SessionId = SessionId(1);

    fn ev(id: i64, kind: EventKind) -> Event {
        let task = match kind {
            EventKind::SessionCreated { .. }
            | EventKind::ProjectFactsSet { .. }
            | EventKind::SessionPaused
            | EventKind::SessionResumed
            | EventKind::SessionDeleted
            | EventKind::AllPaused
            | EventKind::AllResumed
            | EventKind::UsageReading { .. }
            | EventKind::LimitHit { .. } => None,
            _ => Some(T),
        };
        Event {
            id: EventId(id),
            at: Millis(1000 + id),
            session: Some(S),
            task,
            actor: Actor::Regie,
            kind,
        }
    }

    fn started(run: i64, role: Role, purpose: Purpose) -> EventKind {
        EventKind::RunStarted {
            run: RunId(run),
            role,
            purpose,
            provider: Provider::Claude,
            model: "claude-opus-5-5".into(),
            effort: Effort::Medium,
            resume_of: None,
        }
    }

    fn finished(run: i64, outcome: RunOutcome, msg: &str) -> EventKind {
        EventKind::RunFinished {
            run: RunId(run),
            outcome,
            final_message: Some(msg.into()),
        }
    }

    fn pr(sha: &str, decision: Option<ReviewDecision>) -> PrState {
        PrState {
            number: 7,
            url: "https://github.com/o/r/pull/7".into(),
            lifecycle: PrLifecycle::Open,
            checks: Checks::Pending,
            conflicting: false,
            merge_state: MergeState::Clean,
            review_decision: decision,
            head_sha: sha.into(),
        }
    }

    fn blocking_finding() -> Finding {
        Finding {
            id: 1,
            title: "panics".into(),
            kind: FindingKind::Bug,
            blocking: true,
            evidence: None,
        }
    }

    fn base() -> Vec<EventKind> {
        vec![
            EventKind::SessionCreated {
                name: "s".into(),
                repos: vec![RepoInfo {
                    path: "/r".into(),
                    github: Some("o/r".into()),
                }],
            },
            EventKind::ProjectFactsSet {
                repo: "/r".into(),
                facts: ProjectFacts::default(),
            },
            EventKind::TaskCreated {
                repo: "/r".into(),
                title: "t".into(),
                instruction: "do it".into(),
                tickets: vec!["ABC-1".into()],
                blocked_by: vec![],
                level: Level::Standard,
                paths: vec![],
                provider: None,
            },
        ]
    }

    fn state_of(kinds: Vec<EventKind>) -> State {
        let events: Vec<Event> = base()
            .into_iter()
            .chain(kinds)
            .enumerate()
            .map(|(i, k)| ev(i as i64 + 1, k))
            .collect();
        State::replay(&events)
    }

    fn script() -> Vec<Event> {
        let kinds = base().into_iter().chain([
            EventKind::WorktreeCreated {
                path: "/w/t1".into(),
                branch: "regie/t1-t".into(),
                port_base: 41000,
                base: "main".into(),
            },
            EventKind::SetupFinished {
                ok: true,
                output_tail: "ok".into(),
            },
            started(1, Role::Builder, Purpose::Initial),
            EventKind::RunAttached {
                run: RunId(1),
                native_session: "n1".into(),
                pid: Some(42),
            },
            EventKind::MessageQueued {
                text: "also do X".into(),
            },
            EventKind::MessageDelivered,
            finished(1, RunOutcome::Finished, "Done.\nStatus: done"),
            started(2, Role::Reviewer, Purpose::Review),
            finished(2, RunOutcome::Finished, "review"),
            EventKind::ReviewReported {
                run: RunId(2),
                round: 1,
                outcome: ReviewOutcome::Findings(vec![blocking_finding()]),
            },
            EventKind::FindingsRuled {
                round: 1,
                rulings: vec![Ruling {
                    finding: 1,
                    decision: Decision::Fix,
                    note: String::new(),
                }],
            },
            started(3, Role::Builder, Purpose::Fix),
            finished(3, RunOutcome::Finished, "Status: done"),
            started(4, Role::Verify, Purpose::Verify),
            EventKind::VerifyReported {
                run: RunId(4),
                outcome: crate::findings::VerifyOutcome::Results(vec![VerifyResult {
                    finding: 1,
                    status: VerifyStatus::Fixed,
                    note: String::new(),
                }]),
            },
            finished(4, RunOutcome::Finished, "verify"),
            started(5, Role::Builder, Purpose::OpenPr),
            finished(5, RunOutcome::Finished, "Status: done"),
            EventKind::PrObserved {
                pr: pr("abc", None),
            },
            EventKind::PrCommentObserved {
                comment: PrComment {
                    id: "c1".into(),
                    author: "f".into(),
                    body: "nit".into(),
                    changes_requested: false,
                    at: Millis(5),
                },
            },
            EventKind::LevelRaised { level: Level::Hard },
            EventKind::UsageReading {
                provider: Provider::Claude,
                reading: Reading {
                    five_hour: Window {
                        used: 0.1,
                        resets_at: Millis(9),
                    },
                    seven_day: Window {
                        used: 0.2,
                        resets_at: Millis(99),
                    },
                    read_at: Millis(3),
                },
            },
            EventKind::LimitHit {
                provider: Provider::Claude,
                resets_at: Millis(77),
            },
            EventKind::SessionPaused,
            EventKind::AllPaused,
        ]);
        kinds
            .enumerate()
            .map(|(i, k)| ev(i as i64 + 1, k))
            .collect()
    }

    #[test]
    fn replay_equals_incremental_apply() {
        let events = script();
        let mut inc = State::default();
        for e in &events {
            inc.apply(e);
        }
        let replayed = State::replay(&events);
        assert_eq!(inc, replayed);

        let t = &replayed.tasks[&T];
        assert_eq!(t.session, S);
        assert_eq!(t.created_at, Millis(1003));
        assert_eq!(t.level, Level::Hard);
        assert_eq!(t.worktree.as_ref().unwrap().port_base, 41000);
        assert_eq!((t.setup_ok, t.setup_output.as_str()), (Some(true), "ok"));
        assert_eq!(t.builder_native_session.as_deref(), Some("n1"));
        assert_eq!(t.last_builder_status, Some(BuilderStatus::Done));
        assert!(t.pending_messages.is_empty());
        assert_eq!(t.reviews.len(), 1);
        assert!(t.reviews[0].fix_done);
        assert_eq!(t.reviews[0].rulings.as_ref().unwrap().len(), 1);
        assert!(matches!(t.verify, Some(VerifyOutcome::Results(_))));
        assert!(t.open_pr_requested);
        assert_eq!(t.pr.as_ref().unwrap().head_sha, "abc");
        assert_eq!(t.comments.len(), 1);
        assert_eq!(t.last_run_outcome, Some(RunOutcome::Finished));
        assert_eq!(replayed.runs.len(), 5);
        assert_eq!(replayed.runs[&RunId(1)].pid, Some(42));
        assert_eq!(replayed.runs[&RunId(1)].started_at, Millis(1006));
        assert!(replayed.sessions[&S].paused);
        assert!(replayed.paused_all);
        assert_eq!(replayed.blocked_until[&Provider::Claude], Millis(77));
        assert!(replayed.readings.contains_key(&Provider::Claude));
        assert!(replayed.project_facts.contains_key("/r"));
    }

    #[test]
    fn deleting_a_session_drops_it_from_state_but_replay_agrees() {
        let mut events = script();
        let before = State::replay(&events);
        assert!(!before.runs.is_empty());
        let n = events.len() as i64;
        events.push(Event {
            id: EventId(n + 1),
            at: Millis(5000),
            session: Some(S),
            task: None,
            actor: Actor::Regie,
            kind: EventKind::SessionDeleted,
        });
        let mut inc = State::default();
        for e in &events {
            inc.apply(e);
        }
        let replayed = State::replay(&events);
        assert_eq!(inc, replayed);
        assert!(replayed.sessions.is_empty());
        assert!(replayed.tasks.is_empty());
        assert!(replayed.runs.is_empty());
        // ids of what was deleted are not reused
        assert_eq!(
            (
                replayed.next_session_id(),
                replayed.next_task_id(),
                replayed.next_run_id()
            ),
            (
                before.next_session_id(),
                before.next_task_id(),
                before.next_run_id()
            )
        );
    }

    #[test]
    fn deleting_one_session_keeps_the_others() {
        let mut events = script();
        let other = |id: i64, session: i64, task: Option<i64>, kind| Event {
            id: EventId(id),
            at: Millis(6000 + id),
            session: Some(SessionId(session)),
            task: task.map(TaskId),
            actor: Actor::Regie,
            kind,
        };
        events.push(other(
            100,
            2,
            None,
            EventKind::SessionCreated {
                name: "other".into(),
                repos: vec![],
            },
        ));
        events.push(other(101, 1, None, EventKind::SessionDeleted));
        let s = State::replay(&events);
        assert_eq!(
            s.sessions.keys().copied().collect::<Vec<_>>(),
            [SessionId(2)]
        );
        assert!(s.tasks.is_empty());
    }

    #[test]
    fn next_ids_start_at_one() {
        let s = State::default();
        assert_eq!(
            (s.next_session_id(), s.next_task_id(), s.next_run_id()),
            (SessionId(1), TaskId(1), RunId(1))
        );
        let s = State::replay(&script());
        assert_eq!(
            (s.next_session_id(), s.next_task_id(), s.next_run_id()),
            (SessionId(2), TaskId(2), RunId(6))
        );
    }

    #[test]
    fn active_run_is_started_and_not_finished() {
        let s = state_of(vec![started(1, Role::Builder, Purpose::Initial)]);
        assert_eq!(s.active_run(T).map(|r| r.id), Some(RunId(1)));
        assert!(s.builder_started(T));
        let s = state_of(vec![
            started(1, Role::Builder, Purpose::Initial),
            finished(1, RunOutcome::Finished, ""),
        ]);
        assert_eq!(s.active_run(T), None);
        assert_eq!(s.last_run(T).map(|r| r.id), Some(RunId(1)));
        assert!(!state_of(vec![]).builder_started(T));
    }

    #[test]
    fn builder_question_is_parsed_and_crash_keeps_status() {
        let s = state_of(vec![
            started(1, Role::Builder, Purpose::Initial),
            finished(1, RunOutcome::Finished, "Which DB?\nStatus: question"),
        ]);
        assert_eq!(
            s.tasks[&T].last_builder_status,
            Some(BuilderStatus::Question)
        );
        let s = state_of(vec![
            started(1, Role::Builder, Purpose::Initial),
            finished(1, RunOutcome::Crashed, "Status: done"),
        ]);
        assert_eq!(s.tasks[&T].last_builder_status, None);
        assert_eq!(s.tasks[&T].last_run_outcome, Some(RunOutcome::Crashed));
    }

    #[test]
    fn fix_run_marks_latest_review_fixed_even_when_resumed() {
        let review = |round| EventKind::ReviewReported {
            run: RunId(9),
            round,
            outcome: ReviewOutcome::Findings(vec![blocking_finding()]),
        };
        let s = state_of(vec![
            review(1),
            started(1, Role::Builder, Purpose::Fix),
            finished(1, RunOutcome::Interrupted, ""),
            started(2, Role::Builder, Purpose::Resume),
            finished(2, RunOutcome::Finished, "Status: done"),
        ]);
        assert!(s.tasks[&T].reviews[0].fix_done);
        // a resumed non-fix builder run does not mark it
        let s = state_of(vec![
            review(1),
            started(1, Role::Builder, Purpose::Message),
            finished(1, RunOutcome::Paused, ""),
            started(2, Role::Builder, Purpose::Resume),
            finished(2, RunOutcome::Finished, "Status: done"),
        ]);
        assert!(!s.tasks[&T].reviews[0].fix_done);
    }

    #[test]
    fn findings_ruled_sets_that_rounds_rulings() {
        let rulings = vec![Ruling {
            finding: 1,
            decision: Decision::Advisory,
            note: "meh".into(),
        }];
        let s = state_of(vec![
            EventKind::ReviewReported {
                run: RunId(9),
                round: 1,
                outcome: ReviewOutcome::Findings(vec![]),
            },
            EventKind::ReviewReported {
                run: RunId(10),
                round: 2,
                outcome: ReviewOutcome::Findings(vec![blocking_finding()]),
            },
            EventKind::FindingsRuled {
                round: 2,
                rulings: rulings.clone(),
            },
        ]);
        let r = &s.tasks[&T].reviews;
        assert_eq!(r.iter().map(|x| x.round).collect::<Vec<_>>(), [1, 2]);
        assert_eq!(r[0].rulings, None);
        assert_eq!(r[1].rulings, Some(rulings));
    }

    #[test]
    fn messages_queue_and_pop() {
        let q = |t: &str| EventKind::MessageQueued { text: t.into() };
        let s = state_of(vec![q("a"), q("b"), q("c")]);
        assert_eq!(s.tasks[&T].pending_messages, ["a", "b", "c"]);
        // a Message run takes the first one once it attaches (before that it may never be sent)
        let s = state_of(vec![
            q("a"),
            q("b"),
            q("c"),
            started(1, Role::Builder, Purpose::Message),
        ]);
        assert_eq!(s.tasks[&T].pending_messages, ["a", "b", "c"]);
        let attached = |run| EventKind::RunAttached {
            run: RunId(run),
            native_session: "n".into(),
            pid: None,
        };
        let s = state_of(vec![
            q("a"),
            q("b"),
            started(1, Role::Builder, Purpose::Message),
            attached(1),
        ]);
        assert_eq!(s.tasks[&T].pending_messages, ["b"]);
        // interrupted before attaching: still queued; the run resent with it takes it
        let s = state_of(vec![
            q("a"),
            q("b"),
            started(1, Role::Builder, Purpose::Message),
            finished(1, RunOutcome::Paused, ""),
            started(2, Role::Builder, Purpose::Resume),
            attached(2),
        ]);
        assert_eq!(s.tasks[&T].pending_messages, ["b"]);
        // a resume of a Message run that had attached takes nothing more
        let s = state_of(vec![
            q("a"),
            q("b"),
            started(1, Role::Builder, Purpose::Message),
            attached(1),
            finished(1, RunOutcome::Paused, ""),
            started(2, Role::Builder, Purpose::Resume),
            attached(2),
        ]);
        assert_eq!(s.tasks[&T].pending_messages, ["b"]);
        let s = state_of(vec![q("a"), q("b"), EventKind::MessageDelivered]);
        assert_eq!(s.tasks[&T].pending_messages, ["b"]);
        // other purposes do not consume messages
        let s = state_of(vec![q("a"), started(1, Role::Builder, Purpose::Resume)]);
        assert_eq!(s.tasks[&T].pending_messages, ["a"]);
    }

    #[test]
    fn spawned_run_records_its_pid() {
        let s = state_of(vec![
            started(1, Role::Builder, Purpose::Initial),
            EventKind::RunSpawned {
                run: RunId(1),
                pid: 4242,
            },
        ]);
        assert_eq!(s.runs[&RunId(1)].pid, Some(4242));
        assert_eq!(s.runs[&RunId(1)].native_session, None);
        // attaching keeps it when the runner sends no pid
        let s = state_of(vec![
            started(1, Role::Builder, Purpose::Initial),
            EventKind::RunSpawned {
                run: RunId(1),
                pid: 4242,
            },
            EventKind::RunAttached {
                run: RunId(1),
                native_session: "n1".into(),
                pid: None,
            },
        ]);
        assert_eq!(s.runs[&RunId(1)].pid, Some(4242));
    }

    #[test]
    fn fix_runs_count_loops_and_record_head_sha() {
        let s = state_of(vec![
            EventKind::PrObserved { pr: pr("s1", None) },
            started(1, Role::Builder, Purpose::CiFix),
            finished(1, RunOutcome::Finished, "Status: done"),
            EventKind::PrObserved { pr: pr("s2", None) },
            started(2, Role::Builder, Purpose::CiFix),
            finished(2, RunOutcome::Finished, "Status: done"),
            started(3, Role::Builder, Purpose::ConflictFix),
            finished(3, RunOutcome::Finished, "Status: done"),
            // a message with no changes requested is not PR feedback
            started(4, Role::Builder, Purpose::Message),
            finished(4, RunOutcome::Finished, "Status: done"),
            EventKind::PrObserved {
                pr: pr("s3", Some(ReviewDecision::ChangesRequested)),
            },
            started(5, Role::Builder, Purpose::Message),
        ]);
        let t = &s.tasks[&T];
        assert_eq!(
            t.loops,
            LoopCounts {
                ci: 2,
                conflict: 1,
                pr_feedback: 1
            }
        );
        assert_eq!(t.fixed_shas[&LoopKind::Ci], "s2");
        assert_eq!(t.fixed_shas[&LoopKind::Conflict], "s2");
        assert_eq!(t.fixed_shas[&LoopKind::PrFeedback], "s3");
        assert!(!t.open_pr_requested);
    }

    #[test]
    fn retry_clears_failures_and_turns_a_crash_into_a_resume() {
        let s = state_of(vec![
            EventKind::SetupFinished {
                ok: false,
                output_tail: "boom".into(),
            },
            EventKind::PrMissing,
            started(1, Role::Builder, Purpose::Initial),
            finished(1, RunOutcome::Crashed, ""),
            EventKind::TaskRetried,
        ]);
        let t = &s.tasks[&T];
        assert_eq!((t.setup_ok, t.pr_missing), (None, false));
        assert_eq!(t.last_run_outcome, Some(RunOutcome::Interrupted));
        // a finished run's outcome is simply cleared; a good setup is kept
        let s = state_of(vec![
            EventKind::SetupFinished {
                ok: true,
                output_tail: String::new(),
            },
            started(1, Role::Builder, Purpose::OpenPr),
            finished(1, RunOutcome::Finished, "Status: done"),
            EventKind::PrMissing,
            EventKind::TaskRetried,
        ]);
        let t = &s.tasks[&T];
        assert_eq!(
            (t.setup_ok, t.pr_missing, t.last_run_outcome),
            (Some(true), false, None)
        );
        assert!(t.open_pr_requested);
    }

    fn review(run: i64, round: u32, outcome: ReviewOutcome) -> Vec<EventKind> {
        vec![
            started(run, Role::Reviewer, Purpose::Review),
            finished(run, RunOutcome::Finished, "review"),
            EventKind::ReviewReported {
                run: RunId(run),
                round,
                outcome,
            },
        ]
    }

    fn built() -> Vec<EventKind> {
        vec![
            EventKind::WorktreeCreated {
                path: "/w/t1".into(),
                branch: "regie/t1-t".into(),
                port_base: 41000,
                base: "main".into(),
            },
            started(1, Role::Builder, Purpose::Initial),
            finished(1, RunOutcome::Finished, "Status: done"),
        ]
    }

    fn next(s: &State) -> NextStep {
        next_step(&s.tasks[&T], s, &crate::settings::Settings::default())
    }

    fn unparsed() -> ReviewOutcome {
        ReviewOutcome::Unparsed { raw: "lgtm".into() }
    }

    fn ruled_fix(round: u32) -> EventKind {
        EventKind::FindingsRuled {
            round,
            rulings: vec![Ruling {
                finding: 1,
                decision: Decision::Fix,
                note: String::new(),
            }],
        }
    }

    #[test]
    fn retry_runs_an_unparsed_review_again() {
        // R9: round 1 could not be read; a retry drops it and the reviewer runs again.
        let kinds: Vec<EventKind> = built()
            .into_iter()
            .chain(review(2, 1, unparsed()))
            .collect();
        assert_eq!(
            next(&state_of(kinds.clone())),
            NextStep::AwaitHuman(HumanReason::ReviewUnparsed)
        );
        let s = state_of(kinds.into_iter().chain([EventKind::TaskRetried]).collect());
        assert!(s.tasks[&T].reviews.is_empty());
        let round = |round| NextStep::StartRun {
            role: Role::Reviewer,
            purpose: Purpose::Review,
            input: PromptInput::Review { round },
        };
        assert_eq!(next(&s), round(1));

        // round 2: round 1 stays, round 2 runs again
        let kinds: Vec<EventKind> = built()
            .into_iter()
            .chain(review(
                2,
                1,
                ReviewOutcome::Findings(vec![blocking_finding()]),
            ))
            .chain([
                ruled_fix(1),
                started(3, Role::Builder, Purpose::Fix),
                finished(3, RunOutcome::Finished, "Status: done"),
            ])
            .chain(review(4, 2, unparsed()))
            .chain([EventKind::TaskRetried])
            .collect();
        let s = state_of(kinds);
        assert_eq!(
            s.tasks[&T]
                .reviews
                .iter()
                .map(|r| r.round)
                .collect::<Vec<_>>(),
            [1]
        );
        assert_eq!(next(&s), round(2));

        // a retry that resumes a crashed run keeps the review: the task was not waiting on it
        let kinds: Vec<EventKind> = built()
            .into_iter()
            .chain(review(2, 1, unparsed()))
            .chain([
                EventKind::MessageQueued {
                    text: "look again".into(),
                },
                started(3, Role::Builder, Purpose::Message),
                finished(3, RunOutcome::Crashed, ""),
                EventKind::TaskRetried,
            ])
            .collect();
        assert_eq!(state_of(kinds).tasks[&T].reviews.len(), 1);
    }

    #[test]
    fn retry_runs_a_failed_verify_again() {
        let verify = |status| EventKind::VerifyReported {
            run: RunId(7),
            outcome: VerifyOutcome::Results(vec![VerifyResult {
                finding: 1,
                status,
                note: String::new(),
            }]),
        };
        let two_rounds = |status| -> Vec<EventKind> {
            built()
                .into_iter()
                .chain(review(
                    2,
                    1,
                    ReviewOutcome::Findings(vec![blocking_finding()]),
                ))
                .chain([
                    ruled_fix(1),
                    started(3, Role::Builder, Purpose::Fix),
                    finished(3, RunOutcome::Finished, "Status: done"),
                ])
                .chain(review(
                    4,
                    2,
                    ReviewOutcome::Findings(vec![blocking_finding()]),
                ))
                .chain([
                    ruled_fix(2),
                    started(5, Role::Builder, Purpose::Fix),
                    finished(5, RunOutcome::Finished, "Status: done"),
                ])
                .chain([
                    started(6, Role::Verify, Purpose::Verify),
                    finished(6, RunOutcome::Finished, "v"),
                    verify(status),
                ])
                .collect()
        };
        // Frédéric has the builder fix it, then retries: verify runs again
        let kinds: Vec<EventKind> = two_rounds(VerifyStatus::NotFixed)
            .into_iter()
            .chain([
                EventKind::MessageQueued {
                    text: "fix the panic".into(),
                },
                started(7, Role::Builder, Purpose::Message),
                EventKind::RunAttached {
                    run: RunId(7),
                    native_session: "n1".into(),
                    pid: None,
                },
                finished(7, RunOutcome::Finished, "Status: done"),
            ])
            .collect();
        assert_eq!(
            next(&state_of(kinds.clone())),
            NextStep::AwaitHuman(HumanReason::VerifyFailed)
        );
        let s = state_of(kinds.into_iter().chain([EventKind::TaskRetried]).collect());
        assert_eq!(s.tasks[&T].verify, None);
        assert_eq!(
            next(&s),
            NextStep::StartRun {
                role: Role::Verify,
                purpose: Purpose::Verify,
                input: PromptInput::Verify {
                    findings: vec![blocking_finding()]
                },
            }
        );
        // a verify that passed is kept
        let s = state_of(
            two_rounds(VerifyStatus::Fixed)
                .into_iter()
                .chain([EventKind::TaskRetried])
                .collect(),
        );
        assert!(s.tasks[&T].verify.is_some());
    }

    #[test]
    fn cancel_close_and_cleanup_flags() {
        let s = state_of(vec![
            EventKind::TaskCancelled { close_pr: true },
            EventKind::PrClosedByRegie,
            EventKind::WorktreeCleaned,
        ]);
        let t = &s.tasks[&T];
        assert_eq!(t.cancelled, Some(true));
        assert!(t.pr_closed_by_regie && t.cleaned);
        let s = state_of(vec![
            EventKind::SessionPaused,
            EventKind::SessionResumed,
            EventKind::AllPaused,
            EventKind::AllResumed,
        ]);
        assert!(!s.sessions[&S].paused && !s.paused_all);
    }

    #[test]
    fn state_round_trips_through_json() {
        let s = State::replay(&script());
        let j = serde_json::to_string(&s).unwrap();
        assert_eq!(serde_json::from_str::<State>(&j).unwrap(), s);
    }

    #[test]
    fn worktree_records_its_base_and_time() {
        // A log written before `base` was recorded still reads.
        let old: EventKind = serde_json::from_str(
            r#"{"type":"WorktreeCreated","path":"/w","branch":"b","port_base":41000}"#,
        )
        .unwrap();
        assert_eq!(
            old,
            EventKind::WorktreeCreated {
                path: "/w".into(),
                branch: "b".into(),
                port_base: 41000,
                base: String::new()
            }
        );
        let s = State::replay(&script());
        let t = &s.tasks[&TaskId(1)];
        assert_eq!(t.worktree.as_ref().map(|w| w.base.as_str()), Some("main"));
        assert!(t.worktree_created_at.is_some());
    }
}
