//! Coding agents: the runner interface, Claude Code's stream format and process, and prompts.

pub mod claude;
pub mod prompts;
pub mod stream;

use std::path::PathBuf;

use regie_core::{Effort, Millis, Purpose, Reading, Role, RunId, RunOutcome};
use tokio::sync::mpsc;

pub use claude::{probe, ClaudeCli};

/// Everything needed to start one agent run.
#[derive(Clone, Debug, PartialEq)]
pub struct RunSpec {
    pub run: RunId,
    pub role: Role,
    pub purpose: Purpose,
    pub model: String,
    pub effort: Effort,
    pub cwd: PathBuf,
    pub prompt: String,
    /// The agent's own session id to continue, if any.
    pub resume: Option<String>,
    pub env: Vec<(String, String)>,
}

/// What a running agent reports.
#[derive(Clone, Debug, PartialEq)]
pub enum AgentEvent {
    /// The agent's process started; comes before `Attached`.
    Spawned { pid: u32 },
    Attached { native_session: String, pid: Option<u32> },
    /// A line for people to read: text, a tool call, or an error line.
    Output(String),
    Reading(Reading),
    Usage { input_tokens: u64, output_tokens: u64, cache_read_tokens: u64, duration_ms: u64 },
    /// Always the last event of a run.
    Finished { final_message: Option<String>, outcome: RunOutcome, limit_resets_at: Option<Millis> },
}

/// What Régie can ask of a running agent.
#[derive(Clone, Debug, PartialEq)]
pub enum AgentControl {
    /// Another user message for the running session.
    Message(String),
    /// Stop the current turn; the run ends `Paused` and can be resumed.
    Interrupt,
    /// End the process; the run ends `Stopped`.
    Stop,
}

pub struct RunHandle {
    pub events: mpsc::Receiver<AgentEvent>,
    pub control: mpsc::Sender<AgentControl>,
}

#[async_trait::async_trait]
pub trait AgentRunner: Send + Sync {
    async fn start(&self, spec: RunSpec) -> anyhow::Result<RunHandle>;
}
