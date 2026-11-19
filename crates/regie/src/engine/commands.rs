//! Commands from Frédéric (and, later, the lead), turned into events. The
//! controls (pause, stop, cancel, retry) are in `control.rs`.

use std::path::Path;

use regie_core::{validate, Actor, Command, CommandResult, EventKind, Level, RepoInfo, Role, TaskId};

use super::{Engine, Input, Reply};
use crate::agents::AgentControl;

impl Engine {
    pub(super) fn on_command(&mut self, cmd: Command, actor: Actor, reply: Reply) {
        let cmd = canonical_repos(cmd);
        if let Err(e) = validate(&cmd, &self.state) {
            let _ = reply.send(Err(e.to_string()));
            return;
        }
        let result = match cmd {
            Command::CreateSession { name, repos } => {
                // `github_remote` runs git: resolve off the loop, then come back.
                let tx = self.tx.clone();
                tokio::spawn(async move {
                    let repos = resolve_repos(&repos).await;
                    let _ = tx.send(Input::SessionReady { name, repos, actor, reply }).await;
                });
                return;
            }
            Command::SetProjectFacts { repo, facts } => {
                self.emit(None, None, actor, EventKind::ProjectFactsSet { repo, facts }).map(|_| CommandResult::Ok)
            }
            Command::CreateTask { session, repo, title, instruction, tickets, blocked_by, level, paths, provider } => {
                let id = self.state.next_task_id();
                let kind = EventKind::TaskCreated { repo, title, instruction, tickets, blocked_by, level, paths, provider };
                self.emit(Some(session), Some(id), actor, kind).map(|_| CommandResult::Task(id))
            }
            Command::MessageAgent { task, text, level } => self.message(task, text, level, actor),
            Command::StopAgent { task } => self.stop_agent(task),
            Command::CancelTask { task, close_pr } => self.cancel(task, close_pr, actor),
            Command::RetryTask { task } => self.retry(task, actor),
            Command::PauseSession { session } => self.pause(Some(session), actor),
            Command::ResumeSession { session } => self.resume(Some(session), actor),
            Command::PauseAll => self.pause(None, actor),
            Command::ResumeAll => self.resume(None, actor),
        };
        let _ = reply.send(result.map_err(|e| format!("{e:#}")));
    }

    /// Queues the message; a running builder gets it at once if every older
    /// queued message is already out to that run. `MessageDelivered` carries no
    /// text and pops the oldest queued message, so live sends must stay in queue
    /// order; an older message still waiting goes first, as a `Message` run, and
    /// this one follows it. Delivery is confirmed when the agent answers (`runs.rs`).
    fn message(
        &mut self,
        task: TaskId,
        text: String,
        level: Option<Level>,
        actor: Actor,
    ) -> anyhow::Result<CommandResult> {
        if let Some(level) = level {
            self.emit_task(task, actor, EventKind::LevelRaised { level })?;
        }
        let older = self.state.tasks[&task].pending_messages.len();
        self.emit_task(task, actor, EventKind::MessageQueued { text: text.clone() })?;
        let running = self.state.active_run(task).filter(|r| r.role == Role::Builder).map(|r| r.id);
        if let Some(ctl) = running.and_then(|run| self.runs.get_mut(&run)).filter(|ctl| ctl.awaiting == older) {
            if ctl.control.try_send(AgentControl::Message(text)).is_ok() {
                ctl.awaiting += 1;
            }
        }
        Ok(CommandResult::Ok)
    }
}

/// Repo paths are stored canonical, so the same folder always matches.
fn canonical_repos(cmd: Command) -> Command {
    let canon = |p: String| std::fs::canonicalize(&p).ok().and_then(|c| c.to_str().map(String::from)).unwrap_or(p);
    match cmd {
        Command::CreateSession { name, repos } => {
            Command::CreateSession { name, repos: repos.into_iter().map(canon).collect() }
        }
        Command::SetProjectFacts { repo, facts } => Command::SetProjectFacts { repo: canon(repo), facts },
        Command::CreateTask { session, repo, title, instruction, tickets, blocked_by, level, paths, provider } => {
            Command::CreateTask { session, repo: canon(repo), title, instruction, tickets, blocked_by, level, paths, provider }
        }
        other => other,
    }
}

async fn resolve_repos(repos: &[String]) -> Result<Vec<RepoInfo>, String> {
    let mut out = Vec::new();
    for repo in repos {
        let path = Path::new(repo);
        if !tokio::fs::metadata(path).await.is_ok_and(|m| m.is_dir()) {
            return Err(format!("{repo} is not a folder"));
        }
        out.push(RepoInfo { path: repo.clone(), github: crate::git::github_remote(path).await });
    }
    Ok(out)
}
