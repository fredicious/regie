//! The event log vocabulary: everything that happens is one of these.

use serde::{Deserialize, Serialize};
use ts_rs::TS;

use crate::facts::ProjectFacts;
use crate::findings::{ReviewOutcome, Ruling, VerifyOutcome};
use crate::github::{PrComment, PrState};
use crate::ids::{EventId, Millis, RunId, SessionId, TaskId};
use crate::settings::{Effort, Level, Provider};
use crate::usage::Reading;

#[derive(Serialize, Deserialize, TS, Clone, Copy, Debug, PartialEq, Eq)]
#[ts(export)]
pub enum Actor {
    Frederic,
    Lead,
    Run(RunId),
    Regie,
}

#[derive(Serialize, Deserialize, TS, Clone, Copy, Debug, PartialEq, Eq)]
#[ts(export)]
pub enum Role {
    Builder,
    Reviewer,
    Verify,
}

#[derive(Serialize, Deserialize, TS, Clone, Copy, Debug, PartialEq, Eq)]
#[ts(export)]
pub enum Purpose {
    Initial,
    Fix,
    OpenPr,
    CiFix,
    ConflictFix,
    Message,
    Resume,
    Review,
    Verify,
}

#[derive(Serialize, Deserialize, TS, Clone, Copy, Debug, PartialEq, Eq)]
#[ts(export)]
pub enum RunOutcome {
    Finished,
    Paused,
    Stopped,
    LimitHit,
    Crashed,
    Interrupted,
}

#[derive(Serialize, Deserialize, TS, Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
#[ts(export)]
pub enum LoopKind {
    Ci,
    Conflict,
    PrFeedback,
}

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub struct RepoInfo {
    pub path: String,
    pub github: Option<String>,
}

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub struct NewEvent {
    pub at: Millis,
    pub session: Option<SessionId>,
    pub task: Option<TaskId>,
    pub actor: Actor,
    pub kind: EventKind,
}

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub struct Event {
    pub id: EventId,
    pub at: Millis,
    pub session: Option<SessionId>,
    pub task: Option<TaskId>,
    pub actor: Actor,
    pub kind: EventKind,
}

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[serde(tag = "type")]
#[ts(export)]
pub enum EventKind {
    SessionCreated { name: String, repos: Vec<RepoInfo> },
    ProjectFactsSet { repo: String, facts: ProjectFacts },
    TaskCreated {
        repo: String,
        title: String,
        instruction: String,
        tickets: Vec<String>,
        blocked_by: Vec<TaskId>,
        level: Level,
        paths: Vec<String>,
        provider: Option<Provider>,
    },
    LevelRaised { level: Level },
    WorktreeCreated { path: String, branch: String, port_base: u16 },
    SetupFinished { ok: bool, output_tail: String },
    RunStarted {
        run: RunId,
        role: Role,
        purpose: Purpose,
        provider: Provider,
        model: String,
        effort: Effort,
        resume_of: Option<String>,
    },
    RunAttached { run: RunId, native_session: String, pid: Option<u32> },
    RunFinished { run: RunId, outcome: RunOutcome, final_message: Option<String> },
    RunUsage {
        run: RunId,
        #[ts(type = "number")]
        input_tokens: u64,
        #[ts(type = "number")]
        output_tokens: u64,
        #[ts(type = "number")]
        cache_read_tokens: u64,
        #[ts(type = "number")]
        duration_ms: u64,
    },
    MessageQueued { text: String },
    MessageDelivered,
    ReviewReported { run: RunId, round: u32, outcome: ReviewOutcome },
    FindingsRuled { round: u32, rulings: Vec<Ruling> },
    VerifyReported { run: RunId, outcome: VerifyOutcome },
    PrObserved { pr: PrState },
    PrMissing,
    PrCommentObserved { comment: PrComment },
    TaskCancelled { close_pr: bool },
    TaskRetried,
    PrClosedByRegie,
    WorktreeCleaned,
    SessionPaused,
    SessionResumed,
    AllPaused,
    AllResumed,
    UsageReading { provider: Provider, reading: Reading },
    LimitHit { provider: Provider, resets_at: Millis },
}
