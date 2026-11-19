//! Régie core: domain types and logic. Does no I/O.

pub mod command;
pub mod event;
pub mod facts;
pub mod findings;
pub mod flow;
pub mod github;
pub mod ids;
pub mod settings;
pub mod state;
pub mod usage;

pub use command::{Command, CommandError, CommandResult};
pub use event::{Actor, Event, EventKind, LoopKind, NewEvent, Purpose, RepoInfo, Role, RunOutcome};
pub use facts::ProjectFacts;
pub use findings::*;
pub use flow::{next_step, HumanReason, NextStep, PromptInput, RESUME_NOTE};
pub use github::{Checks, MergeState, PrComment, PrLifecycle, PrState, ReviewDecision};
pub use ids::{EventId, Millis, RunId, SessionId, TaskId};
pub use settings::{Caps, Effort, Level, LoopCaps, ModelChoice, Provider, Settings, Thresholds};
pub use state::{LoopCounts, ReviewRecord, Run, Session, State, Task, Worktree};
pub use usage::{Reading, Window};

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn event_kind_round_trips_with_type_tag() {
        let k = EventKind::TaskCreated {
            repo: "/r".into(),
            title: "t".into(),
            instruction: "i".into(),
            tickets: vec!["ABC-1".into()],
            blocked_by: vec![],
            level: Level::Light,
            paths: vec![],
            provider: None,
        };
        let j = serde_json::to_value(&k).unwrap();
        assert_eq!(j["type"], "TaskCreated");
        assert_eq!(serde_json::from_value::<EventKind>(j).unwrap(), k);
    }

    #[test]
    fn default_settings_match_spec() {
        let s = Settings::default();
        assert_eq!((s.caps.overall, s.caps.per_session), (4, 2));
        assert_eq!(
            (s.thresholds.five_hour_max, s.thresholds.weekly_floor, s.thresholds.weekly_ahead_max),
            (0.75, 0.20, 0.10)
        );
        assert_eq!(
            (s.loops.review_rounds, s.loops.ci, s.loops.conflict, s.loops.pr_feedback),
            (2, 3, 3, 3)
        );
        assert_eq!(
            (s.probe_after_ms, s.output_retention_days, s.port_base, s.port_blocks),
            (600_000, 14, 41000, 200)
        );
    }

    #[test]
    fn levels_map_to_claude_models() {
        let s = Settings::default();
        assert_eq!(
            s.model_for(Provider::Claude, Level::Light),
            ModelChoice { model: "claude-sonnet-5-5".into(), effort: Effort::High }
        );
        assert_eq!(
            s.model_for(Provider::Claude, Level::Standard),
            ModelChoice { model: "claude-opus-5-5".into(), effort: Effort::Medium }
        );
        assert_eq!(
            s.model_for(Provider::Claude, Level::Hard),
            ModelChoice { model: "claude-opus-5-5".into(), effort: Effort::High }
        );
    }

    #[test]
    fn levels_map_to_codex_models() {
        let s = Settings::default();
        assert_eq!(
            s.model_for(Provider::Codex, Level::Light),
            ModelChoice { model: "gpt-6.1-sol".into(), effort: Effort::Low }
        );
        assert_eq!(s.model_for(Provider::Codex, Level::Standard).effort, Effort::Medium);
        assert_eq!(s.model_for(Provider::Codex, Level::Hard).effort, Effort::High);
    }

    #[test]
    fn ids_display_and_serialize_transparently() {
        assert_eq!(TaskId(12).to_string(), "T12");
        assert_eq!(RunId(3).to_string(), "R3");
        assert_eq!(serde_json::to_string(&TaskId(12)).unwrap(), "12");
    }

    #[test]
    fn level_order_and_default() {
        assert!(Level::Light < Level::Standard && Level::Standard < Level::Hard);
        assert_eq!(Level::default(), Level::Standard);
    }

    #[test]
    fn command_round_trips_and_error_message() {
        let c = Command::PauseAll;
        let j = serde_json::to_value(&c).unwrap();
        assert_eq!(j["type"], "PauseAll");
        assert_eq!(serde_json::from_value::<Command>(j).unwrap(), c);
        assert_eq!(
            CommandError::NoProjectFacts("/r".into()).to_string(),
            "no project facts for /r; set them with `regie project set`"
        );
    }
}
