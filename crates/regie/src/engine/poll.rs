//! Watching PRs on GitHub: every 30 seconds for each task with a PR asked for or
//! open, at once after the builder opens or fixes one, and three looks ten
//! seconds apart before a requested PR counts as missing.

use std::collections::BTreeMap;
use std::path::PathBuf;

use regie_core::{Actor, EventKind, Millis, PrLifecycle, Task, TaskId};

use super::{Engine, Input, Polled};

const EVERY_MS: i64 = 30_000;
const CHECK_EVERY_MS: i64 = 10_000;
const CHECKS: u32 = 3;

#[derive(Default)]
pub(super) struct Polls {
    next_due: i64,
    seq: u64,
    tasks: BTreeMap<TaskId, Track>,
}

#[derive(Default)]
struct Track {
    sent: u64,
    applied: u64,
    /// The task's runs wait until the poll with this number is back.
    hold: Option<u64>,
    /// Looking for a PR the builder was asked to open.
    check: Option<Check>,
}

struct Check {
    misses: u32,
    /// `None` while a look is out.
    next_at: Option<Millis>,
}

impl Polls {
    pub(super) fn holds(&self, task: TaskId) -> bool {
        self.tasks.get(&task).is_some_and(|t| t.hold.is_some())
    }
}

/// Whether the task has a PR worth watching.
fn watched(t: &Task) -> bool {
    let lifecycle = t.pr.as_ref().map(|pr| pr.lifecycle);
    !t.cleaned
        && t.cancelled.is_none()
        && t.worktree.is_some()
        && lifecycle != Some(PrLifecycle::Merged)
        && (t.open_pr_requested || lifecycle == Some(PrLifecycle::Open))
}

impl Engine {
    pub(super) fn on_tick(&mut self) {
        let now = self.now();
        let periodic = now.0 >= self.polls.next_due;
        if periodic {
            self.polls.next_due = now.0 + EVERY_MS;
        }
        let ids: Vec<TaskId> = self.state.tasks.values().filter(|t| watched(t)).map(|t| t.id).collect();
        for id in ids {
            let track = self.polls.tasks.entry(id).or_default();
            let check_due = track.check.as_ref().is_some_and(|c| c.next_at.is_some_and(|at| now >= at));
            let idle = track.sent <= track.applied;
            if check_due || (periodic && idle) {
                self.send_poll(id, check_due);
            }
        }
    }

    /// Looks at the task's PR now: `check` starts the missing-PR looks, `hold`
    /// keeps the task's runs waiting until this look is back.
    pub(super) fn poll_now(&mut self, id: TaskId, check: bool, hold: bool) {
        if !self.state.tasks.get(&id).is_some_and(watched) {
            return;
        }
        if check {
            self.polls.tasks.entry(id).or_default().check = Some(Check { misses: 0, next_at: None });
        }
        let seq = self.send_poll(id, check);
        if hold {
            self.polls.tasks.entry(id).or_default().hold = Some(seq);
        }
    }

    fn send_poll(&mut self, id: TaskId, check: bool) -> u64 {
        self.polls.seq += 1;
        let seq = self.polls.seq;
        let track = self.polls.tasks.entry(id).or_default();
        track.sent = seq;
        if let Some(c) = track.check.as_mut().filter(|_| check) {
            c.next_at = None;
        }
        let task = &self.state.tasks[&id];
        let repo = PathBuf::from(&task.repo);
        let branch = task.worktree.as_ref().map(|w| w.branch.clone()).unwrap_or_default();
        let (github, tx) = (self.deps.github.clone(), self.tx.clone());
        tokio::spawn(async move {
            let look = async {
                let pr = github.pr_for_branch(&repo, &branch).await?;
                let comments = match &pr {
                    Some(pr) if pr.lifecycle == PrLifecycle::Open => github.pr_activity(&repo, pr.number).await?,
                    _ => Vec::new(),
                };
                anyhow::Ok((pr, comments))
            };
            let result = look.await.map_err(|e| format!("{e:#}"));
            let _ = tx.send(Input::Polled(Polled { task: id, seq, check, result })).await;
        });
        seq
    }

    pub(super) fn on_polled(&mut self, p: Polled) -> anyhow::Result<()> {
        let now = self.now();
        let track = self.polls.tasks.entry(p.task).or_default();
        let stale = p.seq <= track.applied;
        track.applied = track.applied.max(p.seq);
        if track.hold.is_some_and(|h| p.seq >= h) {
            track.hold = None;
        }
        let (pr, comments) = match p.result {
            Ok(found) => found,
            Err(e) => {
                eprintln!("regie: looking at the PR of {}: {e}", p.task);
                if let Some(c) = track.check.as_mut().filter(|_| p.check) {
                    c.next_at = Some(Millis(now.0 + CHECK_EVERY_MS));
                }
                return Ok(());
            }
        };
        let mut missing = false;
        if pr.is_some() {
            track.check = None;
        } else if let Some(c) = track.check.as_mut().filter(|_| p.check) {
            c.misses += 1;
            if c.misses >= CHECKS {
                track.check = None;
                missing = true;
            } else {
                c.next_at = Some(Millis(now.0 + CHECK_EVERY_MS));
            }
        }
        let Some(task) = self.state.tasks.get(&p.task) else { return Ok(()) };
        let new_comments: Vec<_> =
            comments.into_iter().filter(|c| !task.comments.iter().any(|seen| seen.id == c.id)).collect();
        if let Some(pr) = pr.filter(|pr| !stale && task.pr.as_ref() != Some(pr)) {
            self.emit_task(p.task, Actor::Regie, EventKind::PrObserved { pr })?;
        }
        for comment in new_comments {
            self.emit_task(p.task, Actor::Regie, EventKind::PrCommentObserved { comment })?;
        }
        if missing {
            self.emit_task(p.task, Actor::Regie, EventKind::PrMissing)?;
        }
        Ok(())
    }
}
