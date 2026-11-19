//! The board and task views the HTTP API returns.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use ts_rs::TS;

use crate::event::{Event, Role};
use crate::flow::next_step;
use crate::ids::{Millis, RunId, SessionId, TaskId};
use crate::lane::{lane, Flag, Lane};
use crate::scheduler::QueuedReason;
use crate::settings::{Level, Provider, Settings};
use crate::state::{ReviewRecord, Run, Session, State, Task, Worktree};
use crate::usage::{openness, Openness, Reading};

/// What only the running process knows.
#[derive(Serialize, Deserialize, TS, Clone, Debug, Default, PartialEq)]
#[ts(export)]
pub struct LiveView {
    pub active: BTreeMap<TaskId, (RunId, Role)>,
    pub last_output: BTreeMap<TaskId, String>,
    pub waiting: BTreeMap<TaskId, QueuedReason>,
}

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub struct TaskCard {
    pub id: TaskId,
    pub title: String,
    pub lane: Lane,
    pub flags: Vec<Flag>,
    pub level: Level,
    pub model: Option<String>,
    pub tickets: Vec<String>,
    #[ts(type = "[number, string] | null")]
    pub pr: Option<(u64, String)>,
    pub last_output: Option<String>,
    /// The role of the run in flight, if any.
    pub active: Option<Role>,
}

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub struct UsageRow {
    pub provider: Provider,
    pub reading: Option<Reading>,
    pub openness: Openness,
}

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub struct BoardView {
    pub session: Session,
    pub cards: Vec<TaskCard>,
    pub running: u32,
    pub usage: Vec<UsageRow>,
    pub paused_all: bool,
}

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub struct TaskDetail {
    pub card: TaskCard,
    pub instruction: String,
    pub worktree: Option<Worktree>,
    pub runs: Vec<Run>,
    pub timeline: Vec<Event>,
    pub reviews: Vec<ReviewRecord>,
}

fn card(task: &Task, state: &State, live: &LiveView, settings: &Settings) -> TaskCard {
    let active = state.active_run(task.id);
    let waiting = live.waiting.get(&task.id).copied();
    let paused = state.sessions.get(&task.session).is_some_and(|s| s.paused) || state.paused_all;
    let next = next_step(task, state, settings);
    let (lane, flags) = lane(task, active.map(|r| r.role), &next, paused, waiting);
    TaskCard {
        id: task.id,
        title: task.title.clone(),
        lane,
        flags,
        level: task.level,
        model: active
            .or_else(|| state.last_run(task.id))
            .map(|r| r.model.clone()),
        tickets: task.tickets.clone(),
        pr: task.pr.as_ref().map(|pr| (pr.number, pr.url.clone())),
        last_output: live.last_output.get(&task.id).cloned(),
        active: active.map(|r| r.role),
    }
}

fn needs_me(card: &TaskCard) -> bool {
    card.flags.iter().any(|f| matches!(f, Flag::NeedsMe(_)))
}

/// One row per provider, in `Provider` order.
fn usage(state: &State, settings: &Settings, now: Millis) -> Vec<UsageRow> {
    [Provider::Claude, Provider::Codex]
        .into_iter()
        .map(|provider| {
            let reading = state.readings.get(&provider);
            let blocked = state.blocked_until.get(&provider).copied();
            UsageRow {
                provider,
                reading: reading.cloned(),
                openness: openness(reading, blocked, now, &settings.thresholds),
            }
        })
        .collect()
}

/// The session's board: cards with `NeedsMe` first, then oldest first.
pub fn board(
    state: &State,
    live: &LiveView,
    session: SessionId,
    settings: &Settings,
    now: Millis,
) -> Option<BoardView> {
    let session = state.sessions.get(&session)?;
    let mut tasks: Vec<&Task> = state
        .tasks
        .values()
        .filter(|t| t.session == session.id)
        .collect();
    tasks.sort_by_key(|t| (t.created_at, t.id));
    let mut cards: Vec<TaskCard> = tasks
        .iter()
        .map(|t| card(t, state, live, settings))
        .collect();
    // Stable: ties keep the oldest-first order.
    cards.sort_by_key(|c| !needs_me(c));
    let running = tasks
        .iter()
        .filter(|t| state.active_run(t.id).is_some())
        .count() as u32;
    Some(BoardView {
        session: session.clone(),
        cards,
        running,
        usage: usage(state, settings, now),
        paused_all: state.paused_all,
    })
}

pub fn task_detail(
    state: &State,
    live: &LiveView,
    events: &[Event],
    task: TaskId,
    settings: &Settings,
    _now: Millis,
) -> Option<TaskDetail> {
    let t = state.tasks.get(&task)?;
    Some(TaskDetail {
        card: card(t, state, live, settings),
        instruction: t.instruction.clone(),
        worktree: t.worktree.clone(),
        runs: state
            .runs
            .values()
            .filter(|r| r.task == task)
            .cloned()
            .collect(),
        timeline: events
            .iter()
            .filter(|e| e.task == Some(task))
            .cloned()
            .collect(),
        reviews: t.reviews.clone(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::event::{Actor, EventKind, Purpose, RepoInfo, RunOutcome};
    use crate::findings::{BuilderStatus, ReviewOutcome};
    use crate::github::{Checks, MergeState, PrLifecycle, PrState};
    use crate::ids::EventId;
    use crate::settings::Effort;

    fn ev(id: i64, task: Option<i64>, kind: EventKind) -> Event {
        Event {
            id: EventId(id),
            at: Millis(id * 10),
            session: Some(SessionId(1)),
            task: task.map(TaskId),
            actor: Actor::Regie,
            kind,
        }
    }

    fn created(id: i64, task: i64) -> Event {
        ev(
            id,
            Some(task),
            EventKind::TaskCreated {
                repo: "/r".into(),
                title: format!("task {task}"),
                instruction: format!("do {task}"),
                tickets: vec!["ABC-1".into()],
                blocked_by: vec![],
                level: Level::Standard,
                paths: vec![],
                provider: None,
            },
        )
    }

    fn session_event() -> Event {
        ev(
            1,
            None,
            EventKind::SessionCreated {
                name: "s".into(),
                repos: vec![RepoInfo {
                    path: "/r".into(),
                    github: Some("o/r".into()),
                }],
            },
        )
    }

    fn run_started(id: i64, task: i64, run: i64, model: &str) -> Event {
        ev(
            id,
            Some(task),
            EventKind::RunStarted {
                run: RunId(run),
                role: Role::Builder,
                purpose: Purpose::Initial,
                provider: Provider::Claude,
                model: model.into(),
                effort: Effort::High,
                resume_of: None,
            },
        )
    }

    fn run_finished(id: i64, task: i64, run: i64, outcome: RunOutcome) -> Event {
        ev(
            id,
            Some(task),
            EventKind::RunFinished {
                run: RunId(run),
                outcome,
                final_message: None,
            },
        )
    }

    fn worktree(id: i64, task: i64) -> Event {
        ev(
            id,
            Some(task),
            EventKind::WorktreeCreated {
                path: "/w".into(),
                branch: "b".into(),
                port_base: 41000,
                base: "main".into(),
            },
        )
    }

    #[test]
    fn board_orders_cards_needs_me_first_then_oldest() {
        // T1: working (active run). T2: crashed -> NeedsMe. T3: queued. T4: crashed -> NeedsMe.
        let mut events = vec![session_event()];
        for (i, t) in [1, 2, 3, 4].into_iter().enumerate() {
            events.push(created(10 + i as i64, t));
        }
        events.extend([worktree(20, 1), run_started(21, 1, 1, "m1")]);
        for (t, run) in [(2, 2), (4, 3)] {
            events.extend([
                worktree(30 + t, t),
                run_started(40 + t, t, run, "m2"),
                run_finished(50 + t, t, run, RunOutcome::Crashed),
            ]);
        }
        // Make T4 older than T2 by lowering its created_at.
        let mut state = State::replay(&events);
        state.tasks.get_mut(&TaskId(4)).unwrap().created_at = Millis(1);
        let view = board(
            &state,
            &LiveView::default(),
            SessionId(1),
            &Settings::default(),
            Millis(1000),
        )
        .unwrap();
        let order: Vec<(i64, Lane)> = view.cards.iter().map(|c| (c.id.0, c.lane)).collect();
        assert_eq!(
            order,
            vec![
                (4, Lane::Working),
                (2, Lane::Working),
                (1, Lane::Working),
                (3, Lane::Queued)
            ]
        );
        assert!(needs_me(&view.cards[0]) && needs_me(&view.cards[1]) && !needs_me(&view.cards[2]));
        assert_eq!(view.running, 1);
        assert_eq!(view.session.id, SessionId(1));
        // Only T1 has a run in flight.
        let active: Vec<(i64, Option<Role>)> =
            view.cards.iter().map(|c| (c.id.0, c.active)).collect();
        assert_eq!(
            active,
            vec![(4, None), (2, None), (1, Some(Role::Builder)), (3, None)]
        );
        assert!(!view.paused_all);
    }

    #[test]
    fn board_reports_paused_all() {
        let events = vec![session_event(), ev(11, None, EventKind::AllPaused)];
        let state = State::replay(&events);
        let view = board(
            &state,
            &LiveView::default(),
            SessionId(1),
            &Settings::default(),
            Millis(1000),
        )
        .unwrap();
        assert!(view.paused_all);
    }

    #[test]
    fn board_unknown_session_is_none() {
        let state = State::replay(&[session_event()]);
        assert!(board(
            &state,
            &LiveView::default(),
            SessionId(5),
            &Settings::default(),
            Millis(0)
        )
        .is_none());
    }

    #[test]
    fn board_card_fields_and_usage_rows() {
        let events = vec![
            session_event(),
            created(10, 1),
            worktree(11, 1),
            run_started(12, 1, 1, "old"),
            run_finished(13, 1, 1, RunOutcome::Finished),
            run_started(14, 1, 2, "latest"),
            run_finished(15, 1, 2, RunOutcome::Finished),
            ev(
                16,
                Some(1),
                EventKind::PrObserved {
                    pr: PrState {
                        number: 42,
                        url: "https://gh/42".into(),
                        lifecycle: PrLifecycle::Open,
                        checks: Checks::Pending,
                        conflicting: false,
                        merge_state: MergeState::Blocked,
                        review_decision: None,
                        head_sha: "a".into(),
                    },
                },
            ),
            ev(
                17,
                None,
                EventKind::LimitHit {
                    provider: Provider::Codex,
                    resets_at: Millis(5000),
                },
            ),
        ];
        let mut state = State::replay(&events);
        // Built, reviewed clean and past the PR request, so only the PR decides the lane.
        let t = state.tasks.get_mut(&TaskId(1)).unwrap();
        t.last_builder_status = Some(BuilderStatus::Done);
        t.open_pr_requested = true;
        t.reviews.push(ReviewRecord {
            round: 1,
            outcome: ReviewOutcome::Findings(vec![]),
            rulings: None,
            fix_done: false,
        });
        let mut live = LiveView::default();
        live.last_output.insert(TaskId(1), "tail".into());
        let view = board(
            &state,
            &live,
            SessionId(1),
            &Settings::default(),
            Millis(1000),
        )
        .unwrap();
        let c = &view.cards[0];
        assert_eq!(c.model.as_deref(), Some("latest"));
        assert_eq!(c.pr, Some((42, "https://gh/42".into())));
        assert_eq!(c.last_output.as_deref(), Some("tail"));
        assert_eq!(c.tickets, vec!["ABC-1".to_string()]);
        assert_eq!(c.level, Level::Standard);
        assert_eq!(c.lane, Lane::Ci);
        assert_eq!(view.running, 0);
        let rows: Vec<(Provider, Openness)> = view
            .usage
            .iter()
            .map(|u| (u.provider, u.openness.clone()))
            .collect();
        assert_eq!(
            rows,
            vec![
                (Provider::Claude, Openness::Open),
                (
                    Provider::Codex,
                    Openness::Blocked {
                        until: Millis(5000)
                    }
                )
            ]
        );
    }

    #[test]
    fn card_model_prefers_active_run_and_waiting_comes_from_live() {
        let events = vec![
            session_event(),
            created(10, 1),
            created(11, 2),
            worktree(12, 1),
            run_started(13, 1, 1, "active-m"),
        ];
        let state = State::replay(&events);
        let mut live = LiveView::default();
        live.waiting.insert(TaskId(2), QueuedReason::Slot);
        let view = board(&state, &live, SessionId(1), &Settings::default(), Millis(0)).unwrap();
        assert_eq!(view.cards[0].model.as_deref(), Some("active-m"));
        assert_eq!(view.cards[1].model, None);
        assert_eq!(view.cards[1].flags, vec![Flag::Waiting(QueuedReason::Slot)]);
    }

    #[test]
    fn task_detail_filters_timeline_and_lists_runs() {
        let events = vec![
            session_event(),
            created(10, 1),
            created(11, 2),
            worktree(12, 1),
            run_started(13, 1, 1, "m"),
            run_started(14, 2, 2, "m"),
            run_finished(15, 1, 1, RunOutcome::Finished),
        ];
        let state = State::replay(&events);
        let d = task_detail(
            &state,
            &LiveView::default(),
            &events,
            TaskId(1),
            &Settings::default(),
            Millis(0),
        )
        .unwrap();
        let ids: Vec<i64> = d.timeline.iter().map(|e| e.id.0).collect();
        assert_eq!(ids, vec![10, 12, 13, 15]);
        assert_eq!(d.runs.iter().map(|r| r.id.0).collect::<Vec<_>>(), vec![1]);
        assert_eq!(d.instruction, "do 1");
        assert_eq!(d.worktree.as_ref().map(|w| w.branch.as_str()), Some("b"));
        assert_eq!(d.card.id, TaskId(1));
        assert!(task_detail(
            &state,
            &LiveView::default(),
            &events,
            TaskId(9),
            &Settings::default(),
            Millis(0)
        )
        .is_none());
    }
}
