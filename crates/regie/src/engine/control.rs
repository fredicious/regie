//! Frédéric's controls: Standby (pause a session), Fade to black (pause all),
//! stop an agent, cancel a task, retry it.
//!
//! Pausing interrupts the session's running agents; each run ends `Paused` and
//! the scheduler holds the task (`QueuedReason::Paused`) until resume, when the
//! next step resumes it. Tasks still getting a worktree or running setup are
//! held the same way once that is done.

use regie_core::{Actor, CommandResult, EventKind, RunId, SessionId, TaskId};

use super::Engine;
use crate::agents::AgentControl;

impl Engine {
    /// Pauses one session, or all of them with `None`.
    pub(super) fn pause(&mut self, session: Option<SessionId>, actor: Actor) -> anyhow::Result<CommandResult> {
        match session {
            Some(s) if !self.state.sessions[&s].paused => {
                self.emit(Some(s), None, actor, EventKind::SessionPaused)?;
            }
            None if !self.state.paused_all => {
                self.emit(None, None, actor, EventKind::AllPaused)?;
            }
            _ => {}
        }
        let runs: Vec<RunId> = self
            .runs
            .iter()
            .filter(|(_, ctl)| session.is_none_or(|s| self.state.tasks[&ctl.task].session == s))
            .map(|(run, _)| *run)
            .collect();
        for run in runs {
            self.control(run, AgentControl::Interrupt);
        }
        Ok(CommandResult::Ok)
    }

    /// Resumes one session, or lifts the pause on all with `None` (a session
    /// paused on its own stays paused).
    pub(super) fn resume(&mut self, session: Option<SessionId>, actor: Actor) -> anyhow::Result<CommandResult> {
        match session {
            Some(s) if self.state.sessions[&s].paused => {
                self.emit(Some(s), None, actor, EventKind::SessionResumed)?;
            }
            None if self.state.paused_all => {
                self.emit(None, None, actor, EventKind::AllResumed)?;
            }
            _ => {}
        }
        Ok(CommandResult::Ok)
    }

    /// Stops the task's running agent; the run ends `Stopped` and the task waits
    /// for Frédéric (`AgentStopped`).
    pub(super) fn stop_agent(&mut self, task: TaskId) -> anyhow::Result<CommandResult> {
        let Some(run) = self.run_of(task) else { anyhow::bail!("{task} has no running agent") };
        self.control(run, AgentControl::Stop);
        Ok(CommandResult::Ok)
    }

    /// Stops any running agent first; the PR close and cleanup the next step asks
    /// for wait until that run has ended (see `advance`).
    pub(super) fn cancel(&mut self, task: TaskId, close_pr: bool, actor: Actor) -> anyhow::Result<CommandResult> {
        if let Some(run) = self.run_of(task) {
            self.control(run, AgentControl::Stop);
        }
        self.emit_task(task, actor, EventKind::TaskCancelled { close_pr })?;
        Ok(CommandResult::Ok)
    }

    /// The next step resumes the last run's role (or reruns an unreadable review
    /// or a failed verify: R9, in core).
    pub(super) fn retry(&mut self, task: TaskId, actor: Actor) -> anyhow::Result<CommandResult> {
        self.emit_task(task, actor, EventKind::TaskRetried)?;
        Ok(CommandResult::Ok)
    }

    fn run_of(&self, task: TaskId) -> Option<RunId> {
        self.runs.iter().find(|(_, ctl)| ctl.task == task).map(|(run, _)| *run)
    }

    fn control(&self, run: RunId, c: AgentControl) {
        if let Some(ctl) = self.runs.get(&run) {
            if let Err(e) = ctl.control.try_send(c) {
                eprintln!("regie: could not send a control to {run}: {e}");
            }
        }
    }
}
