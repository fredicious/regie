//! Commands the lead or Frederic can issue, and why they get rejected.

use serde::{Deserialize, Serialize};
use ts_rs::TS;

use crate::facts::ProjectFacts;
use crate::ids::{SessionId, TaskId};
use crate::settings::{Level, Provider};

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[serde(tag = "type")]
#[ts(export)]
pub enum Command {
    CreateSession { name: String, repos: Vec<String> },
    SetProjectFacts { repo: String, facts: ProjectFacts },
    CreateTask {
        session: SessionId,
        repo: String,
        title: String,
        instruction: String,
        tickets: Vec<String>,
        blocked_by: Vec<TaskId>,
        level: Level,
        paths: Vec<String>,
        provider: Option<Provider>,
    },
    MessageAgent { task: TaskId, text: String, level: Option<Level> },
    StopAgent { task: TaskId },
    RetryTask { task: TaskId },
    CancelTask { task: TaskId, close_pr: bool },
    PauseSession { session: SessionId },
    ResumeSession { session: SessionId },
    PauseAll,
    ResumeAll,
}

#[derive(Serialize, Deserialize, TS, Clone, Copy, Debug, PartialEq, Eq)]
#[ts(export)]
pub enum CommandResult {
    Session(SessionId),
    Task(TaskId),
    Ok,
}

#[derive(thiserror::Error, Serialize, Deserialize, TS, Clone, Debug, PartialEq, Eq)]
#[ts(export)]
pub enum CommandError {
    #[error("session {0} not found")]
    SessionNotFound(SessionId),
    #[error("{repo} is not a repo of session {session}")]
    RepoNotInSession { session: SessionId, repo: String },
    #[error("{0} has no GitHub remote")]
    NoGithubRemote(String),
    #[error("no project facts for {0}; set them with `regie project set`")]
    NoProjectFacts(String),
    #[error("blocked_by task {0} is not in this session")]
    BlockedByNotInSession(TaskId),
    #[error("task {0} not found")]
    TaskNotFound(TaskId),
    #[error("task {0} is finished (merged or cancelled)")]
    TaskFinished(TaskId),
    #[error("level {requested:?} is not higher than the current level {current:?}")]
    LevelNotHigher { current: Level, requested: Level },
    #[error("a session needs at least one repo")]
    NoRepos,
}
