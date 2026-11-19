//! Everything the engine loop reacts to: commands, agent events, the tick, and
//! the results of slow work it handed to spawned tasks.

use regie_core::{
    Actor, Command, CommandResult, PrComment, PrState, Reading, RepoInfo, RunId, TaskId, Worktree,
};
use tokio::sync::oneshot;

use crate::agents::AgentEvent;

pub type Reply = oneshot::Sender<Result<CommandResult, String>>;

pub enum Input {
    Command(Command, Actor, Reply),
    Agent(RunId, AgentEvent),
    Tick,
    Shutdown,
    /// Ends every run `Interrupted`, stops the agents, then ends the loop and replies.
    Quit(oneshot::Sender<()>),
    /// A new session's repos, made canonical and with their GitHub remotes.
    SessionReady {
        name: String,
        repos: Result<Vec<RepoInfo>, String>,
        actor: Actor,
        reply: Reply,
    },
    WorktreeReady {
        task: TaskId,
        result: Result<Worktree, String>,
    },
    SetupDone {
        task: TaskId,
        ok: bool,
        output: String,
    },
    Polled(Polled),
    Probed(Result<Reading, String>),
    CleanedUp {
        task: TaskId,
        result: Result<(), String>,
    },
    PrClosed {
        task: TaskId,
        result: Result<(), String>,
    },
}

/// One look at a task's PR on GitHub.
pub struct Polled {
    pub task: TaskId,
    /// Polls are numbered; a result older than one already applied is stale.
    pub seq: u64,
    /// One of the looks that decide whether a requested PR is missing.
    pub check: bool,
    /// The PR for the branch, and its comments when it is open.
    pub result: Result<(Option<PrState>, Vec<PrComment>), String>,
}
