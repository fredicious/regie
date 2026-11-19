//! The scripted `FakeClaude` the engine tests rely on.
mod support;

use std::path::PathBuf;

use regie::agents::{AgentControl, AgentEvent, AgentRunner, RunHandle, RunSpec};
use regie_core::{Effort, Millis, Purpose, Reading, Role, RunId, RunOutcome, Window};
use support::claude::{FakeAction, FakeClaude, FakeOutcome};
use support::repo::{git, TempRepo};

fn spec(run: i64, cwd: PathBuf) -> RunSpec {
    RunSpec {
        run: RunId(run),
        role: Role::Builder,
        purpose: Purpose::Initial,
        model: "claude-opus-5-5".into(),
        effort: Effort::Medium,
        cwd,
        prompt: "build it".into(),
        resume: None,
        env: vec![],
    }
}

async fn drain(h: &mut RunHandle) -> Vec<AgentEvent> {
    let mut out = Vec::new();
    while let Some(e) = h.events.recv().await {
        out.push(e);
    }
    out
}

#[tokio::test]
async fn fake_writes_commits_and_finishes() {
    let repo = TempRepo::new();
    let reading = Reading {
        five_hour: Window { used: 0.3, resets_at: Millis(1) },
        seven_day: Window { used: 0.5, resets_at: Millis(2) },
        read_at: Millis(0),
    };
    let r = reading.clone();
    let fake = FakeClaude::new(move |s| FakeOutcome {
        final_message: format!("did {}\n\nStatus: done", s.prompt),
        actions: vec![
            FakeAction::WriteFile("src/new.txt".into(), "hi\n".into()),
            FakeAction::Commit("add new".into()),
            FakeAction::Reading(r.clone()),
        ],
    });
    let mut h = fake.start(spec(1, repo.work.clone())).await.unwrap();
    let events = drain(&mut h).await;
    assert_eq!(
        events.first(),
        Some(&AgentEvent::Attached { native_session: "fake-session-1".into(), pid: None })
    );
    assert!(events.contains(&AgentEvent::Reading(reading)));
    assert_eq!(
        events.last(),
        Some(&AgentEvent::Finished {
            final_message: Some("did build it\n\nStatus: done".into()),
            outcome: RunOutcome::Finished,
            limit_resets_at: None
        })
    );
    assert_eq!(git(&repo.work, &["log", "-1", "--format=%s %an"]), "add new Fake Claude");
    assert_eq!(fake.specs().len(), 1);
}

#[tokio::test]
async fn fake_hangs_until_interrupt_and_records_controls() {
    let repo = TempRepo::new();
    let fake = FakeClaude::new(|_| FakeOutcome { final_message: "x".into(), actions: vec![FakeAction::HangUntilControl] });
    let mut s = spec(2, repo.work.clone());
    s.resume = Some("native-1".into());
    let mut h = fake.start(s).await.unwrap();
    assert!(matches!(h.events.recv().await, Some(AgentEvent::Attached { native_session, .. }) if native_session == "native-1"));
    h.control.send(AgentControl::Message("also this".into())).await.unwrap();
    h.control.send(AgentControl::Interrupt).await.unwrap();
    let events = drain(&mut h).await;
    assert_eq!(
        events,
        [AgentEvent::Finished { final_message: None, outcome: RunOutcome::Paused, limit_resets_at: None }]
    );
    assert_eq!(
        fake.controls(),
        [(RunId(2), AgentControl::Message("also this".into())), (RunId(2), AgentControl::Interrupt)]
    );

    let fake = FakeClaude::new(|_| FakeOutcome { final_message: "x".into(), actions: vec![FakeAction::Crash] });
    let mut h = fake.start(spec(3, repo.work.clone())).await.unwrap();
    assert!(matches!(
        drain(&mut h).await.last(),
        Some(AgentEvent::Finished { outcome: RunOutcome::Crashed, final_message: None, .. })
    ));
}
