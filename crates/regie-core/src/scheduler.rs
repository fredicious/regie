//! Which waiting runs start now.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use ts_rs::TS;

use crate::ids::{Millis, SessionId, TaskId};
use crate::settings::{Provider, Settings};
use crate::usage::Openness;

#[derive(Serialize, Deserialize, TS, Clone, Copy, Debug, PartialEq, Eq)]
#[ts(export)]
pub enum QueuedReason {
    Slot,
    Quota,
    Dependency,
    Paused,
}

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub struct Candidate {
    pub task: TaskId,
    pub session: SessionId,
    pub provider: Provider,
    pub started_before: bool,
    pub created_at: Millis,
    pub deps_merged: bool,
}

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub struct Capacity {
    pub running_total: u32,
    pub running_by_session: BTreeMap<SessionId, u32>,
    pub openness: BTreeMap<Provider, Openness>,
    pub paused_sessions: BTreeSet<SessionId>,
    pub paused_all: bool,
}

/// Decide which waiting runs start now, and why the others wait.
pub fn pick(
    cands: &[Candidate],
    cap: &Capacity,
    s: &Settings,
) -> (Vec<TaskId>, BTreeMap<TaskId, QueuedReason>) {
    let mut order: Vec<&Candidate> = cands.iter().collect();
    order.sort_by_key(|c| (!c.started_before, c.created_at));

    let mut total = cap.running_total;
    let mut by_session = cap.running_by_session.clone();
    let mut start = Vec::new();
    let mut queued = BTreeMap::new();

    for c in order {
        let running_here = by_session.get(&c.session).copied().unwrap_or(0);
        let reason = if cap.paused_all || cap.paused_sessions.contains(&c.session) {
            Some(QueuedReason::Paused)
        } else if !c.deps_merged {
            Some(QueuedReason::Dependency)
        } else if cap
            .openness
            .get(&c.provider)
            .is_some_and(|o| *o != Openness::Open)
        {
            Some(QueuedReason::Quota)
        } else if running_here >= s.caps.per_session || total >= s.caps.overall {
            Some(QueuedReason::Slot)
        } else {
            None
        };
        match reason {
            Some(r) => {
                queued.insert(c.task, r);
            }
            None => {
                total += 1;
                *by_session.entry(c.session).or_insert(0) += 1;
                start.push(c.task);
            }
        }
    }
    (start, queued)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cand(task: i64, session: i64, started: bool, created: i64) -> Candidate {
        Candidate {
            task: TaskId(task),
            session: SessionId(session),
            provider: Provider::Claude,
            started_before: started,
            created_at: Millis(created),
            deps_merged: true,
        }
    }

    fn cap() -> Capacity {
        Capacity {
            running_total: 0,
            running_by_session: BTreeMap::new(),
            openness: BTreeMap::from([
                (Provider::Claude, Openness::Open),
                (Provider::Codex, Openness::Open),
            ]),
            paused_sessions: BTreeSet::new(),
            paused_all: false,
        }
    }

    #[test]
    fn started_work_goes_first() {
        let cands = [
            cand(1, 1, false, 10),
            cand(2, 1, true, 30),
            cand(3, 1, false, 5),
            cand(4, 1, true, 20),
        ];
        let (start, queued) = pick(&cands, &cap(), &Settings::default());
        // per_session = 2: the two started tasks (by created_at) win
        assert_eq!(start, vec![TaskId(4), TaskId(2)]);
        assert_eq!(queued.get(&TaskId(3)), Some(&QueuedReason::Slot));
        assert_eq!(queued.get(&TaskId(1)), Some(&QueuedReason::Slot));
    }

    #[test]
    fn caps_hold_with_slot_reason() {
        let s = Settings::default();
        let cands = [cand(1, 1, false, 1)];
        let mut c = cap();
        c.running_total = 4;
        let (start, queued) = pick(&cands, &c, &s);
        assert!(start.is_empty());
        assert_eq!(queued[&TaskId(1)], QueuedReason::Slot);

        let mut c = cap();
        c.running_total = 2;
        c.running_by_session.insert(SessionId(1), 2);
        let (start, queued) = pick(&cands, &c, &s);
        assert!(start.is_empty());
        assert_eq!(queued[&TaskId(1)], QueuedReason::Slot);

        // another session still starts
        let cands = [cand(1, 1, false, 1), cand(2, 2, false, 2)];
        let (start, _) = pick(&cands, &c, &s);
        assert_eq!(start, vec![TaskId(2)]);
    }

    #[test]
    fn started_ones_count_toward_caps() {
        let cands: Vec<_> = (1..=6).map(|i| cand(i, i, false, i)).collect();
        let mut c = cap();
        c.running_total = 1;
        let (start, queued) = pick(&cands, &c, &Settings::default());
        assert_eq!(start, vec![TaskId(1), TaskId(2), TaskId(3)]);
        assert_eq!(queued.len(), 3);
    }

    #[test]
    fn quota_dependency_paused_reasons() {
        let s = Settings::default();
        let mut c = cap();
        c.openness
            .insert(Provider::Codex, Openness::Blocked { until: Millis(99) });
        c.paused_sessions.insert(SessionId(3));
        let mut quota = cand(1, 1, false, 1);
        quota.provider = Provider::Codex;
        let mut dep = cand(2, 1, false, 2);
        dep.deps_merged = false;
        let paused = cand(3, 3, false, 3);
        let ok = cand(4, 1, false, 4);
        let (start, queued) = pick(&[quota, dep, paused, ok.clone()], &c, &s);
        assert_eq!(start, vec![TaskId(4)]);
        assert_eq!(queued[&TaskId(1)], QueuedReason::Quota);
        assert_eq!(queued[&TaskId(2)], QueuedReason::Dependency);
        assert_eq!(queued[&TaskId(3)], QueuedReason::Paused);

        c.paused_all = true;
        let (start, queued) = pick(&[ok], &c, &s);
        assert!(start.is_empty());
        assert_eq!(queued[&TaskId(4)], QueuedReason::Paused);
    }
}
