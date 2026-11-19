//! Commands the lead or Frederic can issue, and why they get rejected.

use serde::{Deserialize, Serialize};
use ts_rs::TS;

use crate::facts::ProjectFacts;
use crate::github::PrLifecycle;
use crate::ids::{SessionId, TaskId};
use crate::settings::{Level, Provider};
use crate::state::State;

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[serde(tag = "type")]
#[ts(export)]
pub enum Command {
    CreateSession {
        name: String,
        repos: Vec<String>,
    },
    SetProjectFacts {
        repo: String,
        facts: ProjectFacts,
    },
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
    MessageAgent {
        task: TaskId,
        text: String,
        level: Option<Level>,
    },
    RaiseLevel {
        task: TaskId,
        level: Level,
    },
    StopAgent {
        task: TaskId,
    },
    RetryTask {
        task: TaskId,
    },
    CancelTask {
        task: TaskId,
        close_pr: bool,
    },
    PauseSession {
        session: SessionId,
    },
    ResumeSession {
        session: SessionId,
    },
    /// Forget a session whose tasks are all merged or cancelled.
    DeleteSession {
        session: SessionId,
    },
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
    #[error("session {session} still has unfinished tasks ({}); cancel them first", unfinished.iter().map(|t| t.to_string()).collect::<Vec<_>>().join(", "))]
    SessionHasUnfinishedTasks {
        session: SessionId,
        unfinished: Vec<TaskId>,
    },
}

/// Checks a command against the state before it is turned into events.
pub fn validate(cmd: &Command, state: &State) -> Result<(), CommandError> {
    let task = |id: &TaskId| state.tasks.get(id).ok_or(CommandError::TaskNotFound(*id));
    let session = |id: &SessionId| {
        state
            .sessions
            .get(id)
            .ok_or(CommandError::SessionNotFound(*id))
    };
    match cmd {
        Command::CreateSession { repos, .. } => {
            if repos.is_empty() {
                return Err(CommandError::NoRepos);
            }
        }
        Command::CreateTask {
            session: sid,
            repo,
            blocked_by,
            ..
        } => {
            let s = session(sid)?;
            let info = s.repos.iter().find(|r| &r.path == repo).ok_or_else(|| {
                CommandError::RepoNotInSession {
                    session: *sid,
                    repo: repo.clone(),
                }
            })?;
            if info.github.is_none() {
                return Err(CommandError::NoGithubRemote(repo.clone()));
            }
            if !state.project_facts.contains_key(repo) {
                return Err(CommandError::NoProjectFacts(repo.clone()));
            }
            for b in blocked_by {
                if state.tasks.get(b).is_none_or(|t| t.session != *sid) {
                    return Err(CommandError::BlockedByNotInSession(*b));
                }
            }
        }
        Command::RaiseLevel { task: id, level } => {
            let t = task(id)?;
            let merged =
                t.pr.as_ref()
                    .is_some_and(|pr| pr.lifecycle == PrLifecycle::Merged);
            if merged || t.cancelled.is_some() {
                return Err(CommandError::TaskFinished(*id));
            }
            if *level <= t.level {
                return Err(CommandError::LevelNotHigher {
                    current: t.level,
                    requested: *level,
                });
            }
        }
        Command::MessageAgent {
            task: id, level, ..
        } => {
            let t = task(id)?;
            let merged =
                t.pr.as_ref()
                    .is_some_and(|pr| pr.lifecycle == PrLifecycle::Merged);
            if merged || t.cancelled.is_some() {
                return Err(CommandError::TaskFinished(*id));
            }
            if let Some(requested) = level.filter(|l| *l <= t.level) {
                return Err(CommandError::LevelNotHigher {
                    current: t.level,
                    requested,
                });
            }
        }
        Command::StopAgent { task: id }
        | Command::RetryTask { task: id }
        | Command::CancelTask { task: id, .. } => {
            task(id)?;
        }
        Command::PauseSession { session: id } | Command::ResumeSession { session: id } => {
            session(id)?;
        }
        Command::DeleteSession { session: id } => {
            session(id)?;
            let unfinished: Vec<TaskId> = state
                .tasks
                .values()
                .filter(|t| t.session == *id && !t.is_finished())
                .map(|t| t.id)
                .collect();
            if !unfinished.is_empty() {
                return Err(CommandError::SessionHasUnfinishedTasks {
                    session: *id,
                    unfinished,
                });
            }
        }
        Command::SetProjectFacts { .. } | Command::PauseAll | Command::ResumeAll => {}
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::event::{Actor, Event, EventKind, RepoInfo};
    use crate::ids::{EventId, Millis};

    fn ev(id: i64, session: Option<i64>, task: Option<i64>, kind: EventKind) -> Event {
        Event {
            id: EventId(id),
            at: Millis(id),
            session: session.map(SessionId),
            task: task.map(TaskId),
            actor: Actor::Regie,
            kind,
        }
    }

    fn created(task: i64, session: i64, level: Level) -> Event {
        ev(
            10 + task,
            Some(session),
            Some(task),
            EventKind::TaskCreated {
                repo: "/r".into(),
                title: "t".into(),
                instruction: "i".into(),
                tickets: vec![],
                blocked_by: vec![],
                level,
                paths: vec![],
                provider: None,
            },
        )
    }

    /// Session 1 has /r (GitHub) and /nogh; session 2 has /r. Facts only when asked.
    fn state(facts: bool) -> State {
        let repos = |paths: &[(&str, Option<&str>)]| EventKind::SessionCreated {
            name: "s".into(),
            repos: paths
                .iter()
                .map(|(p, g)| RepoInfo {
                    path: (*p).into(),
                    github: g.map(Into::into),
                })
                .collect(),
        };
        let mut events = vec![
            ev(
                1,
                Some(1),
                None,
                repos(&[("/r", Some("o/r")), ("/nogh", None)]),
            ),
            ev(2, Some(2), None, repos(&[("/r", Some("o/r"))])),
        ];
        if facts {
            for repo in ["/r", "/nogh"] {
                events.push(ev(
                    3,
                    None,
                    None,
                    EventKind::ProjectFactsSet {
                        repo: repo.into(),
                        facts: ProjectFacts::default(),
                    },
                ));
            }
        }
        events.push(created(1, 1, Level::Standard));
        events.push(created(2, 2, Level::Standard));
        State::replay(&events)
    }

    fn create(repo: &str, blocked_by: Vec<TaskId>) -> Command {
        create_in(1, repo, blocked_by)
    }

    fn create_in(session: i64, repo: &str, blocked_by: Vec<TaskId>) -> Command {
        Command::CreateTask {
            session: SessionId(session),
            repo: repo.into(),
            title: "t".into(),
            instruction: "i".into(),
            tickets: vec![],
            blocked_by,
            level: Level::Standard,
            paths: vec![],
            provider: None,
        }
    }

    #[test]
    fn create_task_requires_project_facts() {
        assert_eq!(
            validate(&create("/r", vec![]), &state(false)),
            Err(CommandError::NoProjectFacts("/r".into()))
        );
        assert_eq!(validate(&create("/r", vec![]), &state(true)), Ok(()));
        assert_eq!(
            validate(&create_in(9, "/r", vec![]), &state(true)),
            Err(CommandError::SessionNotFound(SessionId(9)))
        );
        assert_eq!(
            validate(&create("/other", vec![]), &state(true)),
            Err(CommandError::RepoNotInSession {
                session: SessionId(1),
                repo: "/other".into()
            })
        );
    }

    #[test]
    fn create_task_rejects_repo_without_github() {
        assert_eq!(
            validate(&create("/nogh", vec![]), &state(true)),
            Err(CommandError::NoGithubRemote("/nogh".into()))
        );
    }

    #[test]
    fn blocked_by_must_exist_in_session() {
        let s = state(true);
        assert_eq!(validate(&create("/r", vec![TaskId(1)]), &s), Ok(()));
        // T2 is in another session, T9 does not exist.
        assert_eq!(
            validate(&create("/r", vec![TaskId(2)]), &s),
            Err(CommandError::BlockedByNotInSession(TaskId(2)))
        );
        assert_eq!(
            validate(&create("/r", vec![TaskId(1), TaskId(9)]), &s),
            Err(CommandError::BlockedByNotInSession(TaskId(9)))
        );
    }

    #[test]
    fn level_can_only_go_up() {
        let s = state(true);
        let msg = |level| Command::MessageAgent {
            task: TaskId(1),
            text: "x".into(),
            level,
        };
        assert_eq!(validate(&msg(None), &s), Ok(()));
        assert_eq!(validate(&msg(Some(Level::Hard)), &s), Ok(()));
        let err = |l| {
            Err(CommandError::LevelNotHigher {
                current: Level::Standard,
                requested: l,
            })
        };
        assert_eq!(
            validate(&msg(Some(Level::Standard)), &s),
            err(Level::Standard)
        );
        assert_eq!(validate(&msg(Some(Level::Light)), &s), err(Level::Light));
    }

    #[test]
    fn raise_level_checks_task_and_direction() {
        let mut s = state(true);
        let raise = |t, level| Command::RaiseLevel {
            task: TaskId(t),
            level,
        };
        assert_eq!(validate(&raise(1, Level::Hard), &s), Ok(()));
        assert_eq!(
            validate(&raise(1, Level::Standard), &s),
            Err(CommandError::LevelNotHigher {
                current: Level::Standard,
                requested: Level::Standard
            })
        );
        assert_eq!(
            validate(&raise(9, Level::Hard), &s),
            Err(CommandError::TaskNotFound(TaskId(9)))
        );
        s.tasks.get_mut(&TaskId(1)).unwrap().cancelled = Some(false);
        assert_eq!(
            validate(&raise(1, Level::Hard), &s),
            Err(CommandError::TaskFinished(TaskId(1)))
        );
    }

    #[test]
    fn message_to_finished_or_missing_task_is_rejected() {
        let mut s = state(true);
        let msg = |t| Command::MessageAgent {
            task: TaskId(t),
            text: "x".into(),
            level: None,
        };
        assert_eq!(
            validate(&msg(9), &s),
            Err(CommandError::TaskNotFound(TaskId(9)))
        );
        s.tasks.get_mut(&TaskId(1)).unwrap().cancelled = Some(false);
        assert_eq!(
            validate(&msg(1), &s),
            Err(CommandError::TaskFinished(TaskId(1)))
        );
    }

    #[test]
    fn other_commands_check_their_target() {
        let s = state(true);
        for c in [
            Command::StopAgent { task: TaskId(9) },
            Command::RetryTask { task: TaskId(9) },
            Command::CancelTask {
                task: TaskId(9),
                close_pr: false,
            },
        ] {
            assert_eq!(validate(&c, &s), Err(CommandError::TaskNotFound(TaskId(9))));
        }
        assert_eq!(
            validate(
                &Command::CancelTask {
                    task: TaskId(1),
                    close_pr: true
                },
                &s
            ),
            Ok(())
        );
        for c in [
            Command::PauseSession {
                session: SessionId(9),
            },
            Command::ResumeSession {
                session: SessionId(9),
            },
        ] {
            assert_eq!(
                validate(&c, &s),
                Err(CommandError::SessionNotFound(SessionId(9)))
            );
        }
        assert_eq!(
            validate(
                &Command::PauseSession {
                    session: SessionId(1)
                },
                &s
            ),
            Ok(())
        );
        assert_eq!(
            validate(
                &Command::CreateSession {
                    name: "n".into(),
                    repos: vec![]
                },
                &s
            ),
            Err(CommandError::NoRepos)
        );
        assert_eq!(validate(&Command::PauseAll, &s), Ok(()));
    }

    #[test]
    fn delete_session_needs_every_task_finished() {
        let mut s = state(true);
        let del = |n| Command::DeleteSession {
            session: SessionId(n),
        };
        assert_eq!(
            validate(&del(9), &s),
            Err(CommandError::SessionNotFound(SessionId(9)))
        );
        s.tasks.get_mut(&TaskId(2)).unwrap().cancelled = Some(false);
        // S1's T1 is still going; S2's only task is cancelled.
        let err = validate(&del(1), &s).unwrap_err();
        assert_eq!(
            err.to_string(),
            "session S1 still has unfinished tasks (T1); cancel them first"
        );
        assert_eq!(validate(&del(2), &s), Ok(()));
        s.tasks.get_mut(&TaskId(1)).unwrap().pr = Some(crate::github::PrState {
            number: 1,
            url: String::new(),
            lifecycle: PrLifecycle::Merged,
            checks: crate::github::Checks::Success,
            conflicting: false,
            merge_state: crate::github::MergeState::Unknown,
            review_decision: None,
            head_sha: String::new(),
        });
        assert_eq!(validate(&del(1), &s), Ok(()));
        // An empty session can go too.
        s.tasks.clear();
        assert_eq!(validate(&del(1), &s), Ok(()));
    }

    #[test]
    fn unfinished_tasks_are_all_named() {
        let mut s = state(true);
        s.tasks.insert(TaskId(3), s.tasks[&TaskId(1)].clone());
        s.tasks.get_mut(&TaskId(3)).unwrap().id = TaskId(3);
        let err = validate(
            &Command::DeleteSession {
                session: SessionId(1),
            },
            &s,
        )
        .unwrap_err();
        assert_eq!(
            err.to_string(),
            "session S1 still has unfinished tasks (T1, T3); cancel them first"
        );
    }
}
