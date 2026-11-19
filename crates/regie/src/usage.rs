//! Whether each provider may take new work right now.

use std::collections::BTreeMap;

use regie_core::{openness, Millis, Openness, Provider, Settings, State};

/// Every provider's openness from the stored readings and limit hits. A provider
/// with no reading is open (Codex has none in milestone 1).
pub fn openness_now(state: &State, now: Millis, s: &Settings) -> BTreeMap<Provider, Openness> {
    [Provider::Claude, Provider::Codex]
        .into_iter()
        .map(|p| {
            let o = openness(
                state.readings.get(&p),
                state.blocked_until.get(&p).copied(),
                now,
                &s.thresholds,
            );
            (p, o)
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use regie_core::{Reading, Window, WindowKind};

    use super::*;

    #[test]
    fn every_provider_from_readings_and_blocks() {
        let mut state = State::default();
        let s = Settings::default();
        let all_open = openness_now(&state, Millis(0), &s);
        assert_eq!(all_open.len(), 2);
        assert!(all_open.values().all(|o| *o == Openness::Open));

        state.readings.insert(
            Provider::Claude,
            Reading {
                five_hour: Window {
                    used: 0.8,
                    resets_at: Millis(100),
                },
                seven_day: Window {
                    used: 0.1,
                    resets_at: Millis(1000),
                },
                read_at: Millis(0),
            },
        );
        state.blocked_until.insert(Provider::Codex, Millis(50));
        let o = openness_now(&state, Millis(10), &s);
        assert_eq!(
            o[&Provider::Claude],
            Openness::Threshold {
                window: WindowKind::FiveHour,
                used: 0.8,
                ahead: None
            }
        );
        assert_eq!(o[&Provider::Codex], Openness::Blocked { until: Millis(50) });
        assert_eq!(
            openness_now(&state, Millis(60), &s)[&Provider::Codex],
            Openness::Open
        );
    }
}
