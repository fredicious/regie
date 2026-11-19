//! Provider usage readings (functions arrive with the usage task).

use serde::{Deserialize, Serialize};
use ts_rs::TS;

use crate::ids::Millis;

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub struct Window {
    pub used: f64,
    pub resets_at: Millis,
}

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub struct Reading {
    pub five_hour: Window,
    pub seven_day: Window,
    pub read_at: Millis,
}

use crate::settings::{Settings, Thresholds};

const WEEK_MS: f64 = 7.0 * 86_400_000.0;

#[derive(Serialize, Deserialize, TS, Clone, Copy, Debug, PartialEq, Eq)]
#[ts(export)]
pub enum WindowKind {
    FiveHour,
    SevenDay,
}

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub enum Openness {
    Open,
    Threshold { window: WindowKind, used: f64, ahead: Option<f64> },
    Blocked { until: Millis },
}

/// Fraction of the seven-day window already gone, clamped to 0..=1.
pub fn week_elapsed(resets_at: Millis, now: Millis) -> f64 {
    (1.0 - (resets_at.0 - now.0) as f64 / WEEK_MS).clamp(0.0, 1.0)
}

/// Whether a provider may take new work. Blocked is checked first, then the
/// five-hour window, then the seven-day pace.
pub fn openness(
    reading: Option<&Reading>,
    blocked_until: Option<Millis>,
    now: Millis,
    t: &Thresholds,
) -> Openness {
    if let Some(until) = blocked_until.filter(|u| u.0 > now.0) {
        return Openness::Blocked { until };
    }
    let Some(r) = reading else { return Openness::Open };
    if r.five_hour.used >= t.five_hour_max {
        return Openness::Threshold { window: WindowKind::FiveHour, used: r.five_hour.used, ahead: None };
    }
    let used = r.seven_day.used;
    let ahead = used - week_elapsed(r.seven_day.resets_at, now);
    if used > t.weekly_floor && ahead > t.weekly_ahead_max {
        return Openness::Threshold { window: WindowKind::SevenDay, used, ahead: Some(ahead) };
    }
    Openness::Open
}

pub fn needs_probe(reading: Option<&Reading>, now: Millis, s: &Settings) -> bool {
    reading.is_none_or(|r| now.0 - r.read_at.0 > s.probe_after_ms)
}

#[cfg(test)]
mod tests {
    use super::*;

    const DAY: i64 = 86_400_000;
    const HOUR: i64 = 3_600_000;

    fn reading(five_used: f64, five_resets: i64, week_used: f64, week_resets: i64) -> Reading {
        Reading {
            five_hour: Window { used: five_used, resets_at: Millis(five_resets) },
            seven_day: Window { used: week_used, resets_at: Millis(week_resets) },
            read_at: Millis(0),
        }
    }

    fn week_left(frac: f64) -> i64 {
        (frac * 7.0 * DAY as f64) as i64
    }

    #[test]
    fn todays_reading_is_open() {
        let now = 0;
        let r = reading(0.30, now + 5 * HOUR, 0.76, now + week_left(0.285));
        assert_eq!(openness(Some(&r), None, Millis(now), &Thresholds::default()), Openness::Open);
    }

    #[test]
    fn eleven_points_ahead_is_threshold() {
        let r = reading(0.30, 5 * HOUR, 0.82, week_left(0.29));
        match openness(Some(&r), None, Millis(0), &Thresholds::default()) {
            Openness::Threshold { window: WindowKind::SevenDay, used, ahead: Some(a) } => {
                assert_eq!(used, 0.82);
                assert!((a - 0.11).abs() < 1e-6, "ahead {a}");
            }
            o => panic!("unexpected {o:?}"),
        }
    }

    #[test]
    fn low_weekly_use_ignores_pace() {
        let r = reading(0.10, 5 * HOUR, 0.15, week_left(0.99));
        assert_eq!(openness(Some(&r), None, Millis(0), &Thresholds::default()), Openness::Open);
    }

    #[test]
    fn five_hour_at_75_is_threshold() {
        let t = Thresholds::default();
        let r = reading(0.75, 5 * HOUR, 0.10, week_left(0.5));
        assert_eq!(
            openness(Some(&r), None, Millis(0), &t),
            Openness::Threshold { window: WindowKind::FiveHour, used: 0.75, ahead: None }
        );
        let r = reading(0.749, 5 * HOUR, 0.10, week_left(0.5));
        assert_eq!(openness(Some(&r), None, Millis(0), &t), Openness::Open);
    }

    #[test]
    fn blocked_until_reset() {
        let t = Thresholds::default();
        assert_eq!(
            openness(None, Some(Millis(100)), Millis(50), &t),
            Openness::Blocked { until: Millis(100) }
        );
        assert_eq!(openness(None, Some(Millis(100)), Millis(100), &t), Openness::Open);
        assert_eq!(openness(None, Some(Millis(100)), Millis(150), &t), Openness::Open);
        // blocked wins over a threshold
        let r = reading(0.9, 5 * HOUR, 0.9, week_left(0.1));
        assert_eq!(
            openness(Some(&r), Some(Millis(100)), Millis(50), &t),
            Openness::Blocked { until: Millis(100) }
        );
    }

    #[test]
    fn week_elapsed_is_clamped() {
        assert_eq!(week_elapsed(Millis(7 * DAY), Millis(0)), 0.0);
        assert_eq!(week_elapsed(Millis(10 * DAY), Millis(0)), 0.0);
        assert_eq!(week_elapsed(Millis(0), Millis(5)), 1.0);
        assert!((week_elapsed(Millis(7 * DAY / 2), Millis(0)) - 0.5).abs() < 1e-9);
    }

    #[test]
    fn probe_when_stale() {
        let s = Settings::default();
        let now = Millis(100 * 60_000);
        let mut r = reading(0.0, 0, 0.0, 0);
        r.read_at = Millis(now.0 - 11 * 60_000);
        assert!(needs_probe(Some(&r), now, &s));
        r.read_at = Millis(now.0 - 9 * 60_000);
        assert!(!needs_probe(Some(&r), now, &s));
        assert!(needs_probe(None, now, &s));
    }
}
