//! The engine: one loop owns the store, the state and the running agents. Every
//! change is `append → state.apply → snapshot → bus`, then `advance` acts on each
//! task's next step. Slow work (git, `gh`, setup, the usage probe) runs in spawned
//! tasks that report back through `Input`, so the loop never waits on it.

mod commands;
mod control;
mod input;
mod poll;
mod recover;
mod runs;
mod start;

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, RwLock};
use std::time::Duration;

use anyhow::Context;
use regie_core::{
    auto_rule, board, next_step, task_detail, Actor, BoardView, Command, CommandResult, Event,
    EventKind, LiveView, Millis, NewEvent, NextStep, Reading, ReviewOutcome, SessionId, Settings,
    State, TaskDetail, TaskId,
};
use tokio::sync::{mpsc, oneshot};

use crate::agents::AgentRunner;
use crate::bus::{Bus, BusMsg};
use crate::caffeinate::Caffeinate;
use crate::git::RepoLocks;
use crate::github::GitHub;
use crate::paths::DataDir;
use crate::store::Store;

pub use input::{Input, Polled, Reply};
pub use recover::{prune_run_files, recover};

pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

const TICK: Duration = Duration::from_secs(5);
/// How long to wait before retrying a cleanup or PR close that failed.
const RETRY_MS: i64 = 60_000;

/// How long a quitting engine waits for agent processes to end.
const QUIT_WAIT: Duration = Duration::from_secs(8);

/// What the engine needs from the outside world; tests pass fakes.
pub struct Deps {
    pub runner: Arc<dyn AgentRunner>,
    pub github: Arc<dyn GitHub>,
    pub probe: Arc<dyn Fn() -> BoxFuture<'static, anyhow::Result<Reading>> + Send + Sync>,
    pub clock: Arc<dyn Fn() -> Millis + Send + Sync>,
    pub caffeinate: bool,
    /// `REGIE_RUN_ANYWAY=1`: every provider counts as open (usage thresholds
    /// and limit blocks are ignored; the slot caps still hold).
    pub run_anyway: bool,
}

/// What readers see: the state as of the latest event, and the live view.
#[derive(Clone, Debug)]
pub struct Snapshot {
    pub state: State,
    pub live: LiveView,
    pub settings: Settings,
}

#[derive(Clone)]
pub struct EngineHandle {
    pub tx: mpsc::Sender<Input>,
    pub bus: Bus,
    pub snapshot: Arc<RwLock<Snapshot>>,
    clock: Arc<dyn Fn() -> Millis + Send + Sync>,
    data: DataDir,
}

impl EngineHandle {
    pub async fn command(&self, cmd: Command, actor: Actor) -> Result<CommandResult, String> {
        let (reply, rx) = oneshot::channel();
        let stopped = || "the engine has stopped".to_string();
        self.tx
            .send(Input::Command(cmd, actor, reply))
            .await
            .map_err(|_| stopped())?;
        rx.await.map_err(|_| stopped())?
    }

    /// Stops the loop without finishing the runs it started (the next start recovers them).
    pub async fn shutdown(&self) {
        if self.tx.send(Input::Shutdown).await.is_ok() {
            self.tx.closed().await;
        }
    }

    /// What `regie serve` does on SIGINT/SIGTERM: stops every agent, records each
    /// run as `Interrupted` (so the next start resumes it) and ends the loop.
    pub async fn stop_all_and_shutdown(&self) {
        let (reply, rx) = oneshot::channel();
        if self.tx.send(Input::Quit(reply)).await.is_ok() {
            let _ = rx.await;
            self.tx.closed().await;
        }
    }

    pub fn board(&self, s: SessionId) -> Option<BoardView> {
        let snap = self.snapshot.read().unwrap();
        board(&snap.state, &snap.live, s, &snap.settings, (self.clock)())
    }

    /// The task with its timeline, read from the store on a read-only second
    /// connection. `Ok(None)` when there is no such task; a store that cannot be
    /// read is an error, not an empty timeline.
    pub fn task(&self, t: TaskId) -> anyhow::Result<Option<TaskDetail>> {
        if !self.snapshot.read().unwrap().state.tasks.contains_key(&t) {
            return Ok(None);
        }
        let events = Store::open_reader(&self.data.db())
            .and_then(|s| s.load_task(t))
            .with_context(|| format!("reading the timeline of {t}"))?;
        let snap = self.snapshot.read().unwrap();
        Ok(task_detail(
            &snap.state,
            &snap.live,
            &events,
            t,
            &snap.settings,
            (self.clock)(),
        ))
    }
}

/// Opens the store, replays it, ends the runs a previous Régie left unfinished,
/// prunes old run files, and spawns the loop and its 5-second tick.
pub async fn start(data: DataDir, settings: Settings, deps: Deps) -> anyhow::Result<EngineHandle> {
    data.ensure()?;
    let mut store = Store::open(&data.db())?;
    let mut state = State::replay(&store.load_all()?);
    let now = (deps.clock)();
    for e in recover(&state, now).await {
        let e = store.append(e)?;
        state.apply(&e);
    }
    prune_run_files(&data.runs(), settings.output_retention_days, now);
    let snapshot = Arc::new(RwLock::new(Snapshot {
        state: state.clone(),
        live: LiveView::default(),
        settings: settings.clone(),
    }));
    let (tx, rx) = mpsc::channel(1024);
    let bus = Bus::default();
    let handle = EngineHandle {
        tx: tx.clone(),
        bus: bus.clone(),
        snapshot: snapshot.clone(),
        clock: deps.clock.clone(),
        data: data.clone(),
    };
    let engine = Engine {
        store,
        state,
        live: LiveView::default(),
        settings,
        caffeinate: Caffeinate::new(deps.caffeinate),
        deps,
        data,
        tx: tx.clone(),
        bus,
        snapshot,
        locks: Arc::new(RepoLocks::default()),
        runs: HashMap::new(),
        prep: BTreeMap::new(),
        busy: BTreeSet::new(),
        retry_at: BTreeMap::new(),
        probing: false,
        probe_retry_at: Millis(0),
        polls: poll::Polls::default(),
        prune_at: recover::next_prune(now),
    };
    tokio::spawn(engine.run(rx));
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(TICK).await;
            if tx.send(Input::Tick).await.is_err() {
                break;
            }
        }
    });
    Ok(handle)
}

struct Engine {
    store: Store,
    state: State,
    live: LiveView,
    settings: Settings,
    deps: Deps,
    data: DataDir,
    /// For spawned work to report back.
    tx: mpsc::Sender<Input>,
    bus: Bus,
    snapshot: Arc<RwLock<Snapshot>>,
    locks: Arc<RepoLocks>,
    runs: HashMap<regie_core::RunId, runs::RunCtl>,
    /// Tasks getting a worktree or running setup, with their port block.
    prep: BTreeMap<TaskId, u16>,
    /// Tasks with a cleanup or PR close in flight.
    busy: BTreeSet<TaskId>,
    /// When a failed cleanup or PR close may be tried again.
    retry_at: BTreeMap<TaskId, Millis>,
    probing: bool,
    probe_retry_at: Millis,
    polls: poll::Polls,
    /// When old run files are next pruned.
    prune_at: Millis,
    caffeinate: Caffeinate,
}

impl Engine {
    /// Acts on what the store holds at once (recovered runs resume), then on each
    /// input. `Shutdown` ends the loop and records nothing for the runs still going.
    async fn run(mut self, mut rx: mpsc::Receiver<Input>) {
        let mut input = None;
        loop {
            let handled = input.take().map_or(Ok(()), |i| self.handle(i));
            for result in [handled, self.advance()] {
                if let Err(e) = result {
                    eprintln!("regie: {e:#}");
                }
            }
            self.snapshot.write().unwrap().live = self.live.clone();
            self.caffeinate.set_running(self.runs.len());
            match rx.recv().await {
                Some(Input::Shutdown) | None => break,
                Some(Input::Quit(reply)) => {
                    self.quit(&mut rx).await;
                    let _ = reply.send(());
                    break;
                }
                Some(i) => input = Some(i),
            }
        }
    }

    /// Records every live run as `Interrupted` and stops its agent, waiting (a
    /// few seconds at most) for the processes to go.
    async fn quit(&mut self, rx: &mut mpsc::Receiver<Input>) {
        let runs: Vec<_> = self.runs.drain().collect();
        let mut pending = BTreeSet::new();
        for (run, ctl) in runs {
            let kind = EventKind::RunFinished {
                run,
                outcome: regie_core::RunOutcome::Interrupted,
                final_message: None,
            };
            if let Err(e) = self.emit_task(ctl.task, Actor::Regie, kind) {
                eprintln!("regie: {e:#}");
            }
            self.live.active.remove(&ctl.task);
            let _ = ctl.control.try_send(crate::agents::AgentControl::Stop);
            pending.insert(run);
        }
        // Each runner ends with `Finished` once its process is gone; nothing else is acted on.
        let wait = async {
            while !pending.is_empty() {
                match rx.recv().await {
                    Some(Input::Agent(run, crate::agents::AgentEvent::Finished { .. })) => {
                        pending.remove(&run);
                    }
                    Some(_) => {}
                    None => break,
                }
            }
        };
        let _ = tokio::time::timeout(QUIT_WAIT, wait).await;
        self.snapshot.write().unwrap().live = self.live.clone();
        self.caffeinate.set_running(0);
    }

    fn handle(&mut self, input: Input) -> anyhow::Result<()> {
        match input {
            Input::Command(cmd, actor, reply) => self.on_command(cmd, actor, reply),
            Input::SessionReady {
                name,
                repos,
                actor,
                reply,
            } => {
                let result = match repos {
                    Ok(repos) => {
                        let id = self.state.next_session_id();
                        self.emit(
                            Some(id),
                            None,
                            actor,
                            EventKind::SessionCreated { name, repos },
                        )
                        .map(|_| CommandResult::Session(id))
                        .map_err(|e| format!("{e:#}"))
                    }
                    Err(e) => Err(e),
                };
                let _ = reply.send(result);
            }
            Input::Agent(run, event) => self.on_agent(run, event)?,
            Input::Tick => {
                self.on_tick();
                self.prune_if_due();
            }
            Input::Shutdown | Input::Quit(_) => {}
            Input::WorktreeReady { task, result } => self.on_worktree(task, result)?,
            Input::SetupDone { task, ok, output } => {
                self.prep.remove(&task);
                self.emit_task(
                    task,
                    Actor::Regie,
                    EventKind::SetupFinished {
                        ok,
                        output_tail: output,
                    },
                )?;
            }
            Input::Polled(p) => self.on_polled(p)?,
            Input::Probed(result) => self.on_probed(result)?,
            Input::CleanedUp { task, result } => {
                self.busy.remove(&task);
                match result {
                    Ok(()) => self.emit_task(task, Actor::Regie, EventKind::WorktreeCleaned)?,
                    Err(e) => self.retry_later(task, &format!("cleaning up {task}: {e}")),
                }
            }
            Input::PrClosed { task, result } => {
                self.busy.remove(&task);
                match result {
                    Ok(()) => self.emit_task(task, Actor::Regie, EventKind::PrClosedByRegie)?,
                    Err(e) => self.retry_later(task, &format!("closing the PR of {task}: {e}")),
                }
            }
        }
        Ok(())
    }

    fn now(&self) -> Millis {
        (self.deps.clock)()
    }

    /// Appends an event, applies it, and tells the snapshot and the watchers.
    fn emit(
        &mut self,
        session: Option<SessionId>,
        task: Option<TaskId>,
        actor: Actor,
        kind: EventKind,
    ) -> anyhow::Result<Event> {
        let e = self.store.append(NewEvent {
            at: self.now(),
            session,
            task,
            actor,
            kind,
        })?;
        self.state.apply(&e);
        self.snapshot.write().unwrap().state.apply(&e);
        self.bus.publish(BusMsg::Event(e.clone()));
        Ok(e)
    }

    fn emit_task(&mut self, task: TaskId, actor: Actor, kind: EventKind) -> anyhow::Result<()> {
        let session = self.state.tasks.get(&task).map(|t| t.session);
        self.emit(session, Some(task), actor, kind)?;
        Ok(())
    }

    fn next(&self, task: TaskId) -> NextStep {
        match self.state.tasks.get(&task) {
            Some(t) => next_step(t, &self.state, &self.settings),
            None => NextStep::Nothing,
        }
    }

    fn retry_later(&mut self, task: TaskId, what: &str) {
        eprintln!("regie: {what}; retrying in a minute");
        self.retry_at.insert(task, Millis(self.now().0 + RETRY_MS));
    }

    /// Acts on every task's next step; runs that may start go through the scheduler.
    fn advance(&mut self) -> anyhow::Result<()> {
        let now = self.now();
        let ids: Vec<TaskId> = self
            .state
            .tasks
            .values()
            .filter(|t| !t.cleaned)
            .map(|t| t.id)
            .collect();
        let mut waiting = BTreeMap::new();
        for id in ids {
            if self.prep.contains_key(&id) || self.busy.contains(&id) {
                continue;
            }
            let mut step = self.next(id);
            if let NextStep::AwaitRuling { round } = step {
                // Milestone 2 wakes the lead here; until then the fixed rule decides.
                let findings = self.state.tasks[&id]
                    .reviews
                    .iter()
                    .find(|r| r.round == round)
                    .map(|r| &r.outcome);
                let rulings = match findings {
                    Some(ReviewOutcome::Findings(f)) => auto_rule(f),
                    _ => Vec::new(),
                };
                self.emit_task(
                    id,
                    Actor::Regie,
                    EventKind::FindingsRuled { round, rulings },
                )?;
                step = self.next(id);
            }
            let can_touch = self.state.active_run(id).is_none()
                && self.retry_at.get(&id).is_none_or(|t| *t <= now);
            match step {
                NextStep::Cleanup if can_touch => self.cleanup(id)?,
                NextStep::ClosePr if can_touch => self.close_pr(id),
                NextStep::StartRun { .. } if !self.polls.holds(id) => {
                    waiting.insert(id, step);
                }
                _ => {}
            }
        }
        self.schedule(waiting)
    }

    fn cleanup(&mut self, id: TaskId) -> anyhow::Result<()> {
        let task = &self.state.tasks[&id];
        let Some(wt) = task.worktree.clone() else {
            return self.emit_task(id, Actor::Regie, EventKind::WorktreeCleaned);
        };
        let repo = std::path::PathBuf::from(&task.repo);
        let (locks, tx) = (self.locks.clone(), self.tx.clone());
        self.busy.insert(id);
        tokio::spawn(async move {
            let result = crate::git::remove_worktree(&locks, &repo, &wt)
                .await
                .map_err(|e| format!("{e:#}"));
            let _ = tx.send(Input::CleanedUp { task: id, result }).await;
        });
        Ok(())
    }

    /// The task's repo as `gh` sees it, with `owner/name` from its session.
    fn gh_repo(&self, id: TaskId) -> crate::github::GhRepo {
        let task = &self.state.tasks[&id];
        let github = self
            .state
            .sessions
            .get(&task.session)
            .and_then(|s| s.repos.iter().find(|r| r.path == task.repo))
            .and_then(|r| r.github.clone());
        crate::github::GhRepo {
            path: std::path::PathBuf::from(&task.repo),
            github,
        }
    }

    fn close_pr(&mut self, id: TaskId) {
        let task = &self.state.tasks[&id];
        let Some(number) = task.pr.as_ref().map(|pr| pr.number) else {
            return;
        };
        let repo = self.gh_repo(id);
        let (github, tx) = (self.deps.github.clone(), self.tx.clone());
        self.busy.insert(id);
        tokio::spawn(async move {
            let result = github
                .close_pr(&repo, number)
                .await
                .map_err(|e| format!("{e:#}"));
            let _ = tx.send(Input::PrClosed { task: id, result }).await;
        });
    }
}
