//! What a running agent reports, turned into events.

use regie_core::{
    parse_review, parse_verify, Actor, EventKind, PromptInput, Provider, Purpose, Role, RunId,
    RunOutcome, TaskId,
};
use tokio::sync::mpsc;

use super::Engine;
use crate::agents::claude::MESSAGE_NOT_DELIVERED;
use crate::agents::{AgentControl, AgentEvent};
use crate::bus::BusMsg;

/// The loop's side of a running agent.
pub(super) struct RunCtl {
    pub task: TaskId,
    pub role: Role,
    /// What the prompt was built from (a reviewer's round comes from here).
    pub input: PromptInput,
    pub control: mpsc::Sender<AgentControl>,
    /// Messages sent to the agent and not yet confirmed.
    pub awaiting: usize,
}

impl Engine {
    pub(super) fn on_agent(&mut self, run: RunId, event: AgentEvent) -> anyhow::Result<()> {
        let Some(ctl) = self.runs.get_mut(&run) else {
            return Ok(());
        };
        let task = ctl.task;
        let actor = Actor::Run(run);
        match event {
            AgentEvent::Spawned { pid } => {
                // Recorded before the agent attaches, so a restart can stop it (recover).
                self.emit_task(task, actor, EventKind::RunSpawned { run, pid })?;
            }
            AgentEvent::Attached {
                native_session,
                pid,
            } => {
                self.emit_task(
                    task,
                    actor,
                    EventKind::RunAttached {
                        run,
                        native_session,
                        pid,
                    },
                )?;
            }
            AgentEvent::Output(line) => {
                // A message counts as delivered once the agent says anything after
                // it, unless what it "says" is the runner's note that it was not.
                if ctl.awaiting > 0 {
                    if line.starts_with(MESSAGE_NOT_DELIVERED) {
                        ctl.awaiting -= 1;
                    } else {
                        let delivered = std::mem::take(&mut ctl.awaiting);
                        for _ in 0..delivered {
                            self.emit_task(task, actor, EventKind::MessageDelivered)?;
                        }
                    }
                }
                self.live.last_output.insert(task, line.clone());
                self.bus.publish(BusMsg::Output { task, run, line });
            }
            AgentEvent::Reading(reading) => {
                self.emit(
                    None,
                    None,
                    actor,
                    EventKind::UsageReading {
                        provider: Provider::Claude,
                        reading,
                    },
                )?;
            }
            AgentEvent::Usage {
                input_tokens,
                output_tokens,
                cache_read_tokens,
                duration_ms,
            } => {
                let kind = EventKind::RunUsage {
                    run,
                    input_tokens,
                    output_tokens,
                    cache_read_tokens,
                    duration_ms,
                };
                self.emit_task(task, actor, kind)?;
            }
            AgentEvent::Finished {
                final_message,
                outcome,
                limit_resets_at,
            } => {
                // Messages still unconfirmed stay queued; the next step sends them again.
                let Some(ctl) = self.runs.remove(&run) else {
                    return Ok(());
                };
                self.live.active.remove(&task);
                let text = final_message.clone().unwrap_or_default();
                self.emit_task(
                    task,
                    actor,
                    EventKind::RunFinished {
                        run,
                        outcome,
                        final_message,
                    },
                )?;
                if outcome == RunOutcome::Finished {
                    match ctl.role {
                        Role::Reviewer => {
                            let round = match ctl.input {
                                PromptInput::Review { round } => round,
                                _ => self.state.tasks[&task]
                                    .reviews
                                    .last()
                                    .map_or(1, |r| r.round + 1),
                            };
                            let kind = EventKind::ReviewReported {
                                run,
                                round,
                                outcome: parse_review(&text),
                            };
                            self.emit_task(task, actor, kind)?;
                        }
                        Role::Verify => {
                            self.emit_task(
                                task,
                                actor,
                                EventKind::VerifyReported {
                                    run,
                                    outcome: parse_verify(&text),
                                },
                            )?;
                        }
                        Role::Builder => self.after_builder(task, run),
                    }
                }
                if let Some(resets_at) = limit_resets_at {
                    self.emit(
                        None,
                        None,
                        actor,
                        EventKind::LimitHit {
                            provider: Provider::Claude,
                            resets_at,
                        },
                    )?;
                }
            }
        }
        Ok(())
    }

    /// After the builder opens or fixes the PR, look at it at once. After a fix,
    /// hold the task until that look is back, so a stale head sha is not taken
    /// for a fix that pushed nothing.
    fn after_builder(&mut self, task: TaskId, run: RunId) {
        match self.state.effective_purpose(run) {
            Some(Purpose::OpenPr) => self.poll_now(task, true, false),
            Some(Purpose::CiFix | Purpose::ConflictFix) => self.poll_now(task, false, true),
            _ => {}
        }
    }
}
