//! Starting runs: the scheduler's pick, the usage probe, the worktree and setup a
//! task needs first, and the run itself.

use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;

use regie_core::{
    allocate_port_base, needs_probe, pick, Actor, Candidate, Capacity, EventKind, NextStep, Openness, Provider,
    PrLifecycle, Purpose, Reading, Role, RunOutcome, TaskId, Worktree,
};
use tokio::sync::mpsc;

use super::runs::RunCtl;
use super::{Engine, Input};
use crate::agents::prompts::prompt_for;
use crate::agents::{AgentControl, AgentEvent, RunSpec};
use crate::git::{create_worktree, run_setup, task_env};
use crate::usage::openness_now;

/// How long to go without a probe after one fails.
const PROBE_RETRY_MS: i64 = 60_000;

impl Engine {
    /// Picks which waiting runs start now. If the Claude reading is stale and
    /// something would start, the probe runs first and the pick waits for it.
    pub(super) fn schedule(&mut self, mut waiting: BTreeMap<TaskId, NextStep>) -> anyhow::Result<()> {
        let now = self.now();
        let cands: Vec<Candidate> = waiting.keys().map(|id| self.candidate(*id)).collect();
        let cap = self.capacity();
        if self.probing {
            return Ok(());
        }
        let reading = self.state.readings.get(&Provider::Claude);
        if needs_probe(reading, now, &self.settings) && now >= self.probe_retry_at {
            let mut open = cap.clone();
            open.openness.values_mut().for_each(|o| *o = Openness::Open);
            if !pick(&cands, &open, &self.settings).0.is_empty() {
                self.probe();
                return Ok(());
            }
        }
        let (start, queued) = pick(&cands, &cap, &self.settings);
        self.live.waiting = queued;
        for id in start {
            if let Some(step) = waiting.remove(&id) {
                self.begin(id, step)?;
            }
        }
        Ok(())
    }

    fn candidate(&self, id: TaskId) -> Candidate {
        let t = &self.state.tasks[&id];
        let merged = |b: &TaskId| {
            let pr = self.state.tasks.get(b).and_then(|b| b.pr.as_ref());
            pr.is_some_and(|pr| pr.lifecycle == PrLifecycle::Merged)
        };
        Candidate {
            task: id,
            session: t.session,
            // Milestone 1 runs every agent on Claude.
            provider: Provider::Claude,
            started_before: t.worktree.is_some(),
            created_at: t.created_at,
            deps_merged: t.blocked_by.iter().all(merged),
        }
    }

    /// Running agents, plus tasks getting ready to run one.
    fn capacity(&self) -> Capacity {
        let mut by_session = BTreeMap::new();
        let busy = self.state.tasks.values().filter(|t| self.prep.contains_key(&t.id) || self.state.active_run(t.id).is_some());
        let mut total = 0;
        for t in busy {
            total += 1;
            *by_session.entry(t.session).or_insert(0) += 1;
        }
        Capacity {
            running_total: total,
            running_by_session: by_session,
            openness: openness_now(&self.state, self.now(), &self.settings),
            paused_sessions: self.state.sessions.values().filter(|s| s.paused).map(|s| s.id).collect(),
            paused_all: self.state.paused_all,
        }
    }

    fn probe(&mut self) {
        self.probing = true;
        let (probe, tx) = (self.deps.probe.clone(), self.tx.clone());
        tokio::spawn(async move {
            let result = probe().await.map_err(|e| format!("{e:#}"));
            let _ = tx.send(Input::Probed(result)).await;
        });
    }

    pub(super) fn on_probed(&mut self, result: Result<Reading, String>) -> anyhow::Result<()> {
        self.probing = false;
        match result {
            Ok(reading) => {
                let kind = EventKind::UsageReading { provider: Provider::Claude, reading };
                self.emit(None, None, Actor::Regie, kind)?;
            }
            Err(e) => {
                // Go on without a fresh reading rather than hold all work.
                eprintln!("regie: usage probe failed: {e}");
                self.probe_retry_at = regie_core::Millis(self.now().0 + PROBE_RETRY_MS);
            }
        }
        Ok(())
    }

    /// A picked task: get its worktree and setup first if needed, else start the run.
    fn begin(&mut self, id: TaskId, step: NextStep) -> anyhow::Result<()> {
        let task = &self.state.tasks[&id];
        let facts = self.state.project_facts.get(&task.repo).cloned().unwrap_or_default();
        match &task.worktree {
            None => {
                let in_use: BTreeSet<u16> = self
                    .state
                    .tasks
                    .values()
                    .filter(|t| !t.cleaned)
                    .filter_map(|t| t.worktree.as_ref().map(|w| w.port_base))
                    .chain(self.prep.values().copied())
                    .collect();
                let Some(port) = allocate_port_base(&in_use, &self.settings) else {
                    let output_tail = "no free port block: every block is taken by another task".into();
                    return self.emit_task(id, Actor::Regie, EventKind::SetupFinished { ok: false, output_tail });
                };
                self.prep.insert(id, port);
                let (locks, tx) = (self.locks.clone(), self.tx.clone());
                let (repo, root, title) = (PathBuf::from(&task.repo), self.data.worktrees(), task.title.clone());
                tokio::spawn(async move {
                    let result = create_worktree(&locks, &repo, &root, id, &title, &facts.copy_files, port)
                        .await
                        .map_err(|e| format!("could not create the worktree: {e:#}"));
                    let _ = tx.send(Input::WorktreeReady { task: id, result }).await;
                });
            }
            Some(wt) if facts.setup.is_some() && task.setup_ok != Some(true) => {
                let (wt, setup) = (wt.clone(), facts.setup.unwrap_or_default());
                self.setup(id, wt, setup);
            }
            Some(_) => {
                if let NextStep::StartRun { role, purpose, input } = step {
                    self.start_run(id, role, purpose, input)?;
                }
            }
        }
        Ok(())
    }

    pub(super) fn on_worktree(&mut self, id: TaskId, result: Result<Worktree, String>) -> anyhow::Result<()> {
        let wt = match result {
            Ok(wt) => wt,
            Err(output_tail) => {
                // R10: the task waits for Frédéric with the error as its setup output.
                self.prep.remove(&id);
                return self.emit_task(id, Actor::Regie, EventKind::SetupFinished { ok: false, output_tail });
            }
        };
        let kind = EventKind::WorktreeCreated {
            path: wt.path.clone(),
            branch: wt.branch.clone(),
            port_base: wt.port_base,
            base: wt.base.clone(),
        };
        self.emit_task(id, Actor::Regie, kind)?;
        let repo = &self.state.tasks[&id].repo;
        match self.state.project_facts.get(repo).and_then(|f| f.setup.clone()) {
            Some(setup) => self.setup(id, wt, setup),
            None => {
                self.prep.remove(&id);
            }
        }
        Ok(())
    }

    /// Runs the setup command in the worktree; the task stays in `prep` until it ends.
    fn setup(&mut self, id: TaskId, wt: Worktree, cmd: String) {
        self.prep.insert(id, wt.port_base);
        let tx = self.tx.clone();
        tokio::spawn(async move {
            let (ok, output) = run_setup(PathBuf::from(&wt.path).as_path(), &cmd, &task_env(id, wt.port_base)).await;
            let _ = tx.send(Input::SetupDone { task: id, ok, output }).await;
        });
    }

    /// Appends `RunStarted`, then starts the agent in a spawned task that forwards
    /// its events to the loop. A builder continues its own session except on its
    /// first run; reviewer and verify runs are always fresh agents.
    fn start_run(&mut self, id: TaskId, role: Role, purpose: Purpose, input: regie_core::PromptInput) -> anyhow::Result<()> {
        let task = &self.state.tasks[&id];
        let Some(wt) = task.worktree.clone() else { return Ok(()) };
        let facts = self.state.project_facts.get(&task.repo).cloned().unwrap_or_default();
        let model = self.settings.model_for(Provider::Claude, task.level);
        let prompt = prompt_for(task, &facts, &input, &model, wt.port_base);
        let resume = match (role, purpose) {
            (Role::Builder, Purpose::Initial) | (Role::Reviewer | Role::Verify, _) => None,
            (Role::Builder, _) => task.builder_native_session.clone(),
        };
        let run = self.state.next_run_id();
        let kind = EventKind::RunStarted {
            run,
            role,
            purpose,
            provider: Provider::Claude,
            model: model.model.clone(),
            effort: model.effort,
            resume_of: resume.clone(),
        };
        self.emit_task(id, Actor::Regie, kind)?;
        let prompt = match prompt {
            Ok(p) => p,
            Err(e) => {
                let final_message = Some(format!("could not build the prompt: {e:#}"));
                let kind = EventKind::RunFinished { run, outcome: RunOutcome::Crashed, final_message };
                return self.emit_task(id, Actor::Regie, kind);
            }
        };
        let spec = RunSpec {
            run,
            role,
            purpose,
            model: model.model,
            effort: model.effort,
            cwd: PathBuf::from(&wt.path),
            prompt,
            resume,
            env: task_env(id, wt.port_base),
        };
        let (control, control_rx) = mpsc::channel(16);
        self.runs.insert(run, RunCtl { task: id, role, input, control, awaiting: 0 });
        self.live.active.insert(id, (run, role));
        tokio::spawn(drive(self.deps.runner.clone(), spec, control_rx, self.tx.clone()));
        Ok(())
    }
}

/// Starts the agent, passes controls to it and its events back to the loop.
/// The loop always gets a `Finished`, even if the agent never started.
async fn drive(
    runner: std::sync::Arc<dyn crate::agents::AgentRunner>,
    spec: RunSpec,
    mut controls: mpsc::Receiver<AgentControl>,
    tx: mpsc::Sender<Input>,
) {
    let run = spec.run;
    let crashed = AgentEvent::Finished { final_message: None, outcome: RunOutcome::Crashed, limit_resets_at: None };
    let mut handle = match runner.start(spec).await {
        Ok(h) => h,
        Err(e) => {
            let _ = tx.send(Input::Agent(run, AgentEvent::Output(format!("could not start the agent: {e:#}")))).await;
            let _ = tx.send(Input::Agent(run, crashed)).await;
            return;
        }
    };
    let agent_control = handle.control.clone();
    tokio::spawn(async move {
        while let Some(c) = controls.recv().await {
            if agent_control.send(c).await.is_err() {
                break;
            }
        }
    });
    let mut finished = false;
    while let Some(e) = handle.events.recv().await {
        finished |= matches!(e, AgentEvent::Finished { .. });
        if tx.send(Input::Agent(run, e)).await.is_err() {
            return;
        }
    }
    if !finished {
        let _ = tx.send(Input::Agent(run, crashed)).await;
    }
}
