//! Settings and the level to model mapping.

use serde::{Deserialize, Serialize};
use ts_rs::TS;

#[derive(Serialize, Deserialize, TS, Clone, Copy, Debug, PartialEq, Eq, Hash)]
#[ts(export)]
pub enum Provider {
    Claude,
    Codex,
}

#[derive(Serialize, Deserialize, TS, Clone, Copy, Debug, Default, PartialEq, Eq, PartialOrd, Ord, Hash)]
#[ts(export)]
pub enum Level {
    Light,
    #[default]
    Standard,
    Hard,
}

#[derive(Serialize, Deserialize, TS, Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
#[ts(export)]
pub enum Effort {
    Low,
    Medium,
    High,
    Xhigh,
    Max,
}

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub struct ModelChoice {
    pub model: String,
    pub effort: Effort,
}

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub struct Caps {
    pub overall: u32,
    pub per_session: u32,
}

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub struct Thresholds {
    pub five_hour_max: f64,
    pub weekly_floor: f64,
    pub weekly_ahead_max: f64,
}

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub struct LoopCaps {
    pub review_rounds: u32,
    pub ci: u32,
    pub conflict: u32,
    pub pr_feedback: u32,
}

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub struct Settings {
    pub caps: Caps,
    pub thresholds: Thresholds,
    pub loops: LoopCaps,
    #[ts(type = "number")]
    pub probe_after_ms: i64,
    pub output_retention_days: u32,
    pub port_base: u16,
    pub port_blocks: u16,
}

impl Default for Settings {
    fn default() -> Self {
        Settings {
            caps: Caps { overall: 4, per_session: 2 },
            thresholds: Thresholds { five_hour_max: 0.75, weekly_floor: 0.20, weekly_ahead_max: 0.10 },
            loops: LoopCaps { review_rounds: 2, ci: 3, conflict: 3, pr_feedback: 3 },
            probe_after_ms: 600_000,
            output_retention_days: 14,
            port_base: 41000,
            port_blocks: 200,
        }
    }
}

impl Settings {
    /// The model and effort a provider uses for a task level.
    pub fn model_for(&self, provider: Provider, level: Level) -> ModelChoice {
        let (model, effort) = match (provider, level) {
            (Provider::Claude, Level::Light) => ("claude-sonnet-5-5", Effort::High),
            (Provider::Claude, Level::Standard) => ("claude-opus-5-5", Effort::Medium),
            (Provider::Claude, Level::Hard) => ("claude-opus-5-5", Effort::High),
            (Provider::Codex, Level::Light) => ("gpt-6.1-sol", Effort::Low),
            (Provider::Codex, Level::Standard) => ("gpt-6.1-sol", Effort::Medium),
            (Provider::Codex, Level::Hard) => ("gpt-6.1-sol", Effort::High),
        };
        ModelChoice { model: model.into(), effort }
    }
}
