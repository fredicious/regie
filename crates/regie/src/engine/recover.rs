//! Picking up after Régie was killed or restarted: runs it started and never saw
//! finish end `Interrupted` (after their agent is stopped, if it still runs), so
//! the next step resumes them. Old run files are pruned here too.

use std::path::Path;
use std::time::{Duration, UNIX_EPOCH};

use regie_core::{Actor, EventKind, Millis, NewEvent, RunOutcome, State};
use tokio::time::Instant;

use super::Engine;

/// How long a left-over agent gets between SIGTERM and SIGKILL.
const STOP_GRACE: Duration = Duration::from_secs(5);
/// A live pid is the run's agent only if that process started no later than
/// this after the run did; otherwise the pid was reused (after a reboot, say).
const START_SLACK_MS: i64 = 60_000;
const DAY_MS: i64 = 86_400_000;

/// For each run started and not finished: if its agent still runs, signal its
/// process group (SIGTERM, then SIGKILL after 5 seconds), then end the run
/// `Interrupted`. Returns the events to append.
pub async fn recover(state: &State, now: Millis) -> Vec<NewEvent> {
    let open: Vec<_> = state
        .runs
        .values()
        .filter(|r| r.outcome.is_none())
        .collect();
    let mut groups = Vec::new();
    for r in &open {
        if let Some(pid) = r.pid.and_then(|p| i32::try_from(p).ok()) {
            if is_agent(pid, r.started_at, now).await {
                groups.push(pid);
            }
        }
    }
    stop_groups(&groups).await;
    open.iter()
        .map(|r| NewEvent {
            at: now,
            session: state.tasks.get(&r.task).map(|t| t.session),
            task: Some(r.task),
            actor: Actor::Regie,
            kind: EventKind::RunFinished {
                run: r.id,
                outcome: RunOutcome::Interrupted,
                final_message: None,
            },
        })
        .collect()
}

/// Whether `pid` still looks like the agent a run started: alive, not Régie
/// itself, and started no later than shortly after the run. (Agents lead their
/// own process group, and the group signal to `-pid` reaches only a group that
/// `pid` leads, so a pid reused by any other process gets nothing.)
async fn is_agent(pid: i32, started_at: Millis, now: Millis) -> bool {
    // SAFETY: signal 0, getpid and getpgrp only read the process table.
    let alive = unsafe {
        pid > 1 && pid != libc::getpid() && pid != libc::getpgrp() && libc::kill(pid, 0) == 0
    };
    if !alive {
        return false;
    }
    match process_age_ms(pid).await {
        Some(age) => now.0 - age <= started_at.0 + START_SLACK_MS,
        None => false,
    }
}

/// How long ago the process started, from `ps -o etime=`.
async fn process_age_ms(pid: i32) -> Option<i64> {
    let out = tokio::process::Command::new("ps")
        .args(["-o", "etime=", "-p", &pid.to_string()])
        .env("LC_ALL", "C")
        .output()
        .await
        .ok()?;
    out.status.success().then_some(())?;
    parse_etime(String::from_utf8_lossy(&out.stdout).trim()).map(|s| s * 1000)
}

/// `[[dd-]hh:]mm:ss` in seconds.
fn parse_etime(s: &str) -> Option<i64> {
    let (days, rest) = match s.split_once('-') {
        Some((d, rest)) => (d.parse::<i64>().ok()?, rest),
        None => (0, s),
    };
    let parts: Vec<i64> = rest
        .split(':')
        .map(|p| p.parse().ok())
        .collect::<Option<_>>()?;
    let (h, m, sec) = match parts[..] {
        [m, s] => (0, m, s),
        [h, m, s] => (h, m, s),
        _ => return None,
    };
    Some(((days * 24 + h) * 60 + m) * 60 + sec)
}

fn signal_group(leader: i32, sig: libc::c_int) -> bool {
    // SAFETY: kill(2) has no memory effects.
    unsafe { libc::kill(-leader, sig) == 0 }
}

/// SIGTERM to every group, then SIGKILL to those still there after the grace.
async fn stop_groups(groups: &[i32]) {
    if groups.is_empty() {
        return;
    }
    for &g in groups {
        signal_group(g, libc::SIGTERM);
    }
    let deadline = Instant::now() + STOP_GRACE;
    while Instant::now() < deadline && groups.iter().any(|&g| signal_group(g, 0)) {
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    for &g in groups {
        if signal_group(g, 0) {
            signal_group(g, libc::SIGKILL);
        }
    }
}

/// Deletes run files (`<run id>.jsonl`) last written more than `days` days before `now`.
pub fn prune_run_files(dir: &Path, days: u32, now: Millis) {
    let cutoff = now.0 - i64::from(days) * DAY_MS;
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().is_none_or(|x| x != "jsonl") {
            continue;
        }
        let written = entry
            .metadata()
            .and_then(|m| m.modified())
            .ok()
            .and_then(|t| t.duration_since(UNIX_EPOCH).ok());
        if written.is_some_and(|w| (w.as_millis() as i64) < cutoff) {
            if let Err(e) = std::fs::remove_file(&path) {
                eprintln!("regie: removing {}: {e}", path.display());
            }
        }
    }
}

impl Engine {
    /// Once a day, prunes old run files off the loop.
    pub(super) fn prune_if_due(&mut self) {
        let now = self.now();
        if now < self.prune_at {
            return;
        }
        self.prune_at = next_prune(now);
        let (dir, days) = (self.data.runs(), self.settings.output_retention_days);
        tokio::task::spawn_blocking(move || prune_run_files(&dir, days, now));
    }
}

/// When the next daily prune is due, counted from `now`.
pub(super) fn next_prune(now: Millis) -> Millis {
    Millis(now.0 + DAY_MS)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn etime_formats() {
        assert_eq!(parse_etime("00:07"), Some(7));
        assert_eq!(parse_etime("12:34"), Some(12 * 60 + 34));
        assert_eq!(parse_etime("01:02:03"), Some(3723));
        assert_eq!(parse_etime("2-01:02:03"), Some(2 * 86_400 + 3723));
        assert_eq!(parse_etime(""), None);
        assert_eq!(parse_etime("x"), None);
    }
}
