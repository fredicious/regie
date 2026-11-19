//! A scripted `AgentRunner` for integration tests: no Claude, real git.
//!
//! Each run asks the script what to do, then performs the actions in order and
//! finishes with the script's final message. `HangUntilControl` waits for a
//! control message: `Interrupt` ends the run `Paused`, `Stop` ends it
//! `Stopped`, and `Message` is recorded and answered with an output line while
//! the run keeps waiting. `WaitFor` does the same until the test opens its gate.
//! The run attaches (`Attached`) just before its first action other than
//! `Spawned` and `HangBeforeAttach`, or before finishing.

use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{Arc, Mutex};

use regie::agents::{AgentControl, AgentEvent, AgentRunner, RunHandle, RunSpec};
use regie_core::{Reading, RunId, RunOutcome};
use tokio::sync::mpsc;

pub enum FakeAction {
    /// Writes a file, relative to the run's cwd unless absolute.
    WriteFile(PathBuf, String),
    /// `git add -A` and `git commit` in the run's cwd.
    Commit(String),
    Reading(Reading),
    /// Ends the run `Crashed` with no final message; later actions are skipped.
    Crash,
    HangUntilControl,
    /// Waits for a permit on the gate, answering messages like `HangUntilControl`.
    WaitFor(Arc<tokio::sync::Semaphore>),
    /// Reports this pid as the agent's process (`AgentEvent::Spawned`).
    Spawned(u32),
    /// Hangs like `HangUntilControl` before the run attaches, as Claude does while
    /// it loads its setup; ended by a control, the run never attaches.
    HangBeforeAttach,
}

pub struct FakeOutcome {
    pub final_message: String,
    pub actions: Vec<FakeAction>,
}

type Script = dyn Fn(&RunSpec) -> FakeOutcome + Send + Sync;

pub struct FakeClaude {
    script: Box<Script>,
    specs: Arc<Mutex<Vec<RunSpec>>>,
    controls: Arc<Mutex<Vec<(RunId, AgentControl)>>>,
}

impl FakeClaude {
    pub fn new(script: impl Fn(&RunSpec) -> FakeOutcome + Send + Sync + 'static) -> Self {
        FakeClaude { script: Box::new(script), specs: Default::default(), controls: Default::default() }
    }

    /// A fake whose every run finishes at once with `final_message`.
    pub fn always(final_message: &str) -> Self {
        let m = final_message.to_string();
        Self::new(move |_| FakeOutcome { final_message: m.clone(), actions: vec![] })
    }

    /// Every spec started, in order.
    pub fn specs(&self) -> Vec<RunSpec> {
        self.specs.lock().unwrap().clone()
    }

    /// Every control message received, in order, with its run.
    pub fn controls(&self) -> Vec<(RunId, AgentControl)> {
        self.controls.lock().unwrap().clone()
    }
}

/// The native session a fake run reports: the resumed one, or a new name.
pub fn fake_session(spec: &RunSpec) -> String {
    spec.resume.clone().unwrap_or_else(|| format!("fake-session-{}", spec.run.0))
}

fn commit(cwd: &Path, message: &str) -> Result<(), String> {
    let git = |args: &[&str]| {
        let out = Command::new("git")
            .current_dir(cwd)
            .args(["-c", "user.name=Fake Claude", "-c", "user.email=fake-claude@example.com", "-c", "commit.gpgsign=false"])
            .args(args)
            .output()
            .map_err(|e| e.to_string())?;
        if out.status.success() {
            Ok(())
        } else {
            Err(format!("git {args:?}: {}", String::from_utf8_lossy(&out.stderr)))
        }
    };
    git(&["add", "-A"])?;
    git(&["commit", "-m", message])
}

#[async_trait::async_trait]
impl AgentRunner for FakeClaude {
    async fn start(&self, spec: RunSpec) -> anyhow::Result<RunHandle> {
        self.specs.lock().unwrap().push(spec.clone());
        let outcome = (self.script)(&spec);
        let (events_tx, events) = mpsc::channel(64);
        let (control, mut control_rx) = mpsc::channel::<AgentControl>(16);

        // Record every control message, whenever it comes, and pass it on.
        let (pass_tx, mut pass_rx) = mpsc::unbounded_channel();
        let controls = self.controls.clone();
        let run = spec.run;
        tokio::spawn(async move {
            while let Some(c) = control_rx.recv().await {
                controls.lock().unwrap().push((run, c.clone()));
                let _ = pass_tx.send(c);
            }
        });

        tokio::spawn(async move {
            let send = |e: AgentEvent| {
                let tx = events_tx.clone();
                async move {
                    let _ = tx.send(e).await;
                }
            };
            let attach = AgentEvent::Attached { native_session: fake_session(&spec), pid: None };
            let mut attached = false;
            let mut end = RunOutcome::Finished;
            'actions: for action in outcome.actions {
                if !attached && !matches!(action, FakeAction::Spawned(_) | FakeAction::HangBeforeAttach) {
                    send(attach.clone()).await;
                    attached = true;
                }
                match action {
                    FakeAction::Spawned(pid) => send(AgentEvent::Spawned { pid }).await,
                    FakeAction::HangBeforeAttach => loop {
                        match pass_rx.recv().await {
                            Some(AgentControl::Interrupt) => {
                                end = RunOutcome::Paused;
                                break 'actions;
                            }
                            // not attached yet: nothing answers a message
                            Some(AgentControl::Message(_)) => {}
                            Some(AgentControl::Stop) | None => {
                                end = RunOutcome::Stopped;
                                break 'actions;
                            }
                        }
                    },
                    FakeAction::WriteFile(path, content) => {
                        let path = spec.cwd.join(path);
                        if let Some(dir) = path.parent() {
                            std::fs::create_dir_all(dir).unwrap();
                        }
                        std::fs::write(&path, content).unwrap();
                    }
                    FakeAction::Commit(message) => {
                        if let Err(e) = commit(&spec.cwd, &message) {
                            send(AgentEvent::Output(e)).await;
                            end = RunOutcome::Crashed;
                            break;
                        }
                    }
                    FakeAction::Reading(r) => send(AgentEvent::Reading(r)).await,
                    FakeAction::Crash => {
                        end = RunOutcome::Crashed;
                        break;
                    }
                    FakeAction::WaitFor(gate) => loop {
                        tokio::select! {
                            permit = gate.acquire() => {
                                permit.unwrap().forget();
                                break;
                            }
                            c = pass_rx.recv() => match c {
                                Some(AgentControl::Message(text)) => {
                                    send(AgentEvent::Output(format!("message received: {text}"))).await;
                                }
                                Some(AgentControl::Interrupt) => {
                                    end = RunOutcome::Paused;
                                    break 'actions;
                                }
                                Some(AgentControl::Stop) | None => {
                                    end = RunOutcome::Stopped;
                                    break 'actions;
                                }
                            },
                        }
                    },
                    FakeAction::HangUntilControl => loop {
                        match pass_rx.recv().await {
                            // Claude answers a message it got mid-run; so does the fake.
                            Some(AgentControl::Message(text)) => {
                                send(AgentEvent::Output(format!("message received: {text}"))).await;
                            }
                            Some(AgentControl::Interrupt) => {
                                end = RunOutcome::Paused;
                                break 'actions;
                            }
                            Some(AgentControl::Stop) | None => {
                                end = RunOutcome::Stopped;
                                break 'actions;
                            }
                        }
                    },
                }
            }
            if !attached && end == RunOutcome::Finished {
                send(attach).await;
            }
            let final_message = match end {
                RunOutcome::Finished => {
                    send(AgentEvent::Output(outcome.final_message.clone())).await;
                    send(AgentEvent::Usage { input_tokens: 10, output_tokens: 5, cache_read_tokens: 0, duration_ms: 1 })
                        .await;
                    Some(outcome.final_message)
                }
                _ => None,
            };
            send(AgentEvent::Finished { final_message, outcome: end, limit_resets_at: None }).await;
        });
        Ok(RunHandle { events, control })
    }
}
