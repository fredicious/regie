//! The engine takes tasks end to end on fakes: real git, fake Claude, fake GitHub.
mod support;

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use tokio::sync::Semaphore;

use regie::agents::AgentControl;
use regie::bus::BusMsg;
use regie_core::{
    Actor, Checks, Command, EventKind, Flag, HumanReason, Lane, LoopKind, MergeState, ProjectFacts, Purpose,
    QueuedReason, Role, RunId, RESUME_NOTE,
};
use support::claude::{FakeAction, FakeOutcome};
use support::harness::{
    blocking_review, branch_of, builds, exists, hangs, pr, says, Harness, CLEAN_REVIEW, MINUTE,
};
use support::repo::git;

const SECS: u64 = 15;

fn needs_me(h: &Harness, t: regie_core::TaskId, reason: HumanReason) -> bool {
    h.card(t).flags.contains(&Flag::NeedsMe(reason))
}

fn roles(h: &Harness, t: regie_core::TaskId) -> Vec<(Role, Purpose)> {
    h.specs_for(t).iter().map(|s| (s.role, s.purpose)).collect()
}

#[tokio::test]
async fn one_task_happy_path() {
    let reviews = AtomicUsize::new(0);
    let h = Harness::new(move |spec, gh| match (spec.role, spec.purpose) {
        (Role::Builder, Purpose::Initial) => builds("src.txt", "one\n"),
        (Role::Reviewer, Purpose::Review) => match reviews.fetch_add(1, Ordering::SeqCst) {
            0 => says(&blocking_review("panics on empty input")),
            _ => says(CLEAN_REVIEW),
        },
        (Role::Builder, Purpose::Fix) => builds("src.txt", "two\n"),
        (Role::Builder, Purpose::OpenPr) => {
            gh.set(&branch_of(spec), pr(7, "sha1", Checks::Pending, MergeState::Blocked));
            says("Opened the PR.\n\nStatus: done")
        }
        other => panic!("unexpected run {other:?}"),
    })
    .await;
    let mut bus = h.engine.bus.subscribe();
    let t = h.create_session_and_task("Add src").await;
    h.wait_lane(t, Lane::Ci, SECS).await;
    assert_eq!(
        h.lane_history(t),
        [Lane::Queued, Lane::Working, Lane::InReview, Lane::Working, Lane::InReview, Lane::Working, Lane::Ci]
    );
    assert_eq!(
        roles(&h, t),
        [
            (Role::Builder, Purpose::Initial),
            (Role::Reviewer, Purpose::Review),
            (Role::Builder, Purpose::Fix),
            (Role::Reviewer, Purpose::Review),
            (Role::Builder, Purpose::OpenPr),
        ]
    );
    let specs = h.specs_for(t);
    // the builder keeps its session; each reviewer is a fresh agent
    let resumes: Vec<Option<&str>> = specs.iter().map(|s| s.resume.as_deref()).collect();
    let native = Some("fake-session-1");
    assert_eq!(resumes, [None, None, native, None, native]);
    assert!(specs[2].prompt.contains("panics on empty input"), "{}", specs[2].prompt);
    let state = h.state();
    let task = &state.tasks[&t];
    let wt = task.worktree.clone().unwrap();
    assert_eq!(std::fs::read_to_string(format!("{}/src.txt", wt.path)).unwrap(), "two\n");
    assert_eq!(specs[0].cwd.to_str(), Some(wt.path.as_str()));
    assert!(specs[0].env.contains(&("REGIE_PORT_BASE".into(), "41000".into())));
    assert_eq!(task.reviews.len(), 2);
    let ruled = h.events().into_iter().find(|e| matches!(e.kind, EventKind::FindingsRuled { round: 1, .. })).unwrap();
    assert_eq!(ruled.actor, Actor::Regie);
    let detail = h.engine.task(t).unwrap();
    assert_eq!(detail.runs.len(), 5);
    assert!(detail.timeline.iter().all(|e| e.task == Some(t)));
    assert!(detail.timeline.iter().any(|e| matches!(e.kind, EventKind::WorktreeCreated { .. })));
    // watchers saw the events and the agents' output lines
    let (mut created, mut output) = (false, false);
    while let Ok(msg) = bus.try_recv() {
        match msg {
            BusMsg::Event(e) => created |= matches!(e.kind, EventKind::TaskCreated { .. }),
            BusMsg::Output { task, line, .. } => output |= task == t && line.contains("Status: done"),
        }
    }
    assert!(created && output);

    // CI goes green, then Frédéric merges.
    h.gh.set(&wt.branch, pr(7, "sha1", Checks::Success, MergeState::Clean));
    h.tick_until("ready to merge", 30_000, SECS, |h| h.lane(t) == Lane::ReadyToMerge).await;
    h.gh.merge(&wt.branch);
    h.tick_until("cleaned up", 30_000, SECS, |h| h.state().tasks[&t].cleaned).await;
    assert_eq!(h.lane(t), Lane::Merged);
    assert!(!exists(&wt.path), "worktree still at {}", wt.path);
    assert!(!git(&h.repo.work, &["branch", "--list"]).contains("regie/"));
    assert_eq!(h.lane_history(t)[6..], [Lane::Ci, Lane::ReadyToMerge, Lane::Merged]);
}

#[tokio::test]
async fn message_to_running_builder_is_delivered_live() {
    let h = Harness::new(|spec, _| match spec.purpose {
        Purpose::Initial => hangs(),
        other => panic!("unexpected run {other:?}"),
    })
    .await;
    let t = h.create_session_and_task("Long job").await;
    h.wait_for("builder running", SECS, |h| h.state().active_run(t).is_some()).await;
    h.message(t, "Also add a test.").await;
    h.wait_for("message delivered", SECS, |h| h.state().tasks[&t].pending_messages.is_empty()).await;
    assert!(h.events().iter().any(|e| e.kind == EventKind::MessageDelivered));
    assert_eq!(h.claude.controls(), [(RunId(1), AgentControl::Message("Also add a test.".into()))]);
    assert!(h.state().tasks[&t].pending_messages.is_empty());
    assert_eq!(h.specs_for(t).len(), 1);
    assert_eq!(h.lane(t), Lane::Working);
    assert_eq!(h.card(t).last_output.as_deref(), Some("message received: Also add a test."));
    // two in a row both go live, in order
    h.message(t, "First.").await;
    h.message(t, "Second.").await;
    h.wait_for("both delivered", SECS, |h| h.state().tasks[&t].pending_messages.is_empty()).await;
    let sent: Vec<AgentControl> = h.claude.controls().into_iter().skip(1).map(|(_, c)| c).collect();
    assert_eq!(sent, [AgentControl::Message("First.".into()), AgentControl::Message("Second.".into())]);
    assert_eq!(h.events().iter().filter(|e| e.kind == EventKind::MessageDelivered).count(), 3);
    assert_eq!(h.specs_for(t).len(), 1);
}

#[tokio::test]
async fn message_queued_before_start_is_not_overtaken() {
    let gate = Arc::new(Semaphore::new(0));
    let g = gate.clone();
    let h = Harness::new(move |spec, _| match (spec.role, spec.purpose) {
        (Role::Builder, Purpose::Initial) => FakeOutcome {
            final_message: "Status: done".into(),
            actions: vec![FakeAction::WaitFor(g.clone())],
        },
        (Role::Builder, Purpose::Message) => says("Done.\nStatus: done"),
        (Role::Reviewer, Purpose::Review) => hangs(),
        other => panic!("unexpected run {other:?}"),
    })
    .await;
    // A is sent while the task waits for quota, B while the first builder run works.
    *h.probe_five_hour.lock().unwrap() = 0.80;
    let t = h.create_session_and_task("Ordered").await;
    h.wait_for("held for quota", SECS, |h| h.card(t).flags == [Flag::Waiting(QueuedReason::Quota)]).await;
    h.message(t, "A").await;
    *h.probe_five_hour.lock().unwrap() = 0.20;
    h.tick_until("builder started", MINUTE, SECS, |h| h.state().active_run(t).is_some()).await;
    h.message(t, "B").await;
    tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    gate.add_permits(1);
    h.wait_for("review", SECS, |h| h.specs_for(t).iter().any(|s| s.role == Role::Reviewer)).await;

    // every way a message reaches a builder: live, or as a Message run's prompt
    let live = h.claude.controls().into_iter().filter_map(|(_, c)| match c {
        AgentControl::Message(text) => Some(text),
        _ => None,
    });
    let runs = h.specs_for(t).into_iter().filter(|s| s.purpose == Purpose::Message).map(|s| s.prompt);
    let mut reached: Vec<String> = live.chain(runs).collect();
    reached.sort();
    assert_eq!(reached, ["A", "B"]);
    assert!(h.state().tasks[&t].pending_messages.is_empty());
}

#[tokio::test]
async fn builder_question_then_message() {
    let h = Harness::new(|spec, _| match spec.purpose {
        Purpose::Initial => says("Which database should I use?\nStatus: question"),
        Purpose::Message => hangs(),
        other => panic!("unexpected run {other:?}"),
    })
    .await;
    let t = h.create_session_and_task("Store things").await;
    h.wait_for("question", SECS, |h| needs_me(h, t, HumanReason::BuilderQuestion)).await;
    assert_eq!(h.lane(t), Lane::Working);
    h.message(t, "Use SQLite.").await;
    h.wait_for("message run", SECS, |h| h.specs_for(t).len() == 2).await;
    let spec = &h.specs_for(t)[1];
    assert_eq!((spec.role, spec.purpose), (Role::Builder, Purpose::Message));
    assert_eq!(spec.resume.as_deref(), Some("fake-session-1"));
    assert_eq!(spec.prompt, "Use SQLite.");
    assert!(h.state().tasks[&t].pending_messages.is_empty());
}

#[tokio::test]
async fn two_rounds_then_verify_then_needs_me() {
    let h = Harness::new(|spec, _| match (spec.role, spec.purpose) {
        (Role::Builder, Purpose::Initial) => builds("a.txt", "a\n"),
        (Role::Reviewer, Purpose::Review) => says(&blocking_review("still wrong")),
        (Role::Builder, Purpose::Fix) => builds("a.txt", &format!("{}\n", spec.run.0)),
        (Role::Verify, Purpose::Verify) => {
            says("```json\n{\"results\":[{\"finding\":1,\"status\":\"not_fixed\",\"note\":\"still panics\"}]}\n```")
        }
        other => panic!("unexpected run {other:?}"),
    })
    .await;
    let t = h.create_session_and_task("Tricky").await;
    h.wait_for("verify failed", SECS, |h| needs_me(h, t, HumanReason::VerifyFailed)).await;
    assert_eq!(h.lane(t), Lane::InReview);
    assert_eq!(
        roles(&h, t),
        [
            (Role::Builder, Purpose::Initial),
            (Role::Reviewer, Purpose::Review),
            (Role::Builder, Purpose::Fix),
            (Role::Reviewer, Purpose::Review),
            (Role::Builder, Purpose::Fix),
            (Role::Verify, Purpose::Verify),
        ]
    );
    let verify = h.specs_for(t).pop().unwrap();
    assert_eq!(verify.resume, None);
    assert!(verify.prompt.contains("still wrong"));
}

#[tokio::test]
async fn ci_failure_three_fixes_then_cap() {
    let fixes = AtomicUsize::new(0);
    let failing = || Checks::Failure { failed: vec!["test".into()] };
    let h = Harness::new(move |spec, gh| match (spec.role, spec.purpose) {
        (Role::Builder, Purpose::Initial) => builds("a.txt", "a\n"),
        (Role::Reviewer, Purpose::Review) => says(CLEAN_REVIEW),
        (Role::Builder, Purpose::OpenPr) => {
            gh.set(&branch_of(spec), pr(9, "sha0", failing(), MergeState::Unstable));
            says("Status: done")
        }
        (Role::Builder, Purpose::CiFix) => {
            let n = fixes.fetch_add(1, Ordering::SeqCst) + 1;
            // the fix is pushed, and CI fails again
            gh.set(&branch_of(spec), pr(9, &format!("sha{n}"), failing(), MergeState::Unstable));
            builds(&format!("fix{n}.txt"), "x\n")
        }
        other => panic!("unexpected run {other:?}"),
    })
    .await;
    let t = h.create_session_and_task("Flaky").await;
    h.wait_for("CI cap", SECS, |h| needs_me(h, t, HumanReason::CapReached(LoopKind::Ci))).await;
    let ci_fixes: Vec<_> = h.specs_for(t).into_iter().filter(|s| s.purpose == Purpose::CiFix).collect();
    assert_eq!(ci_fixes.len(), 3);
    assert!(ci_fixes[0].prompt.contains("test"));
    let state = h.state();
    assert_eq!(state.tasks[&t].loops.ci, 3);
    assert_eq!(state.tasks[&t].pr.as_ref().unwrap().head_sha, "sha3");
    assert_eq!(h.lane(t), Lane::Working);
}

#[tokio::test]
async fn dependent_task_waits_for_merge() {
    let h = Harness::new(|spec, gh| match (spec.role, spec.purpose) {
        (Role::Builder, Purpose::Initial) => builds("a.txt", "a\n"),
        (Role::Reviewer, Purpose::Review) => says(CLEAN_REVIEW),
        (Role::Builder, Purpose::OpenPr) => {
            let number = 100 + spec.run.0 as u64;
            gh.set(&branch_of(spec), pr(number, "sha", Checks::Success, MergeState::Clean));
            says("Status: done")
        }
        other => panic!("unexpected run {other:?}"),
    })
    .await;
    let s = h.create_session(ProjectFacts::default()).await;
    let t1 = h.create_task(s, "first", vec![]).await;
    let t2 = h.create_task(s, "second", vec![t1]).await;
    h.wait_lane(t1, Lane::ReadyToMerge, SECS).await;
    assert_eq!(h.lane(t2), Lane::Queued);
    assert_eq!(h.card(t2).flags, [Flag::Waiting(QueuedReason::Dependency)]);
    assert!(h.specs_for(t2).is_empty());

    let branch = h.state().tasks[&t1].worktree.clone().unwrap().branch;
    h.gh.merge(&branch);
    h.tick_until("second task starts", 30_000, SECS, |h| !h.specs_for(t2).is_empty()).await;
    assert_eq!(h.specs_for(t2)[0].purpose, Purpose::Initial);
    h.wait_for("first cleaned", SECS, |h| h.state().tasks[&t1].cleaned).await;
    assert_eq!(h.lane(t1), Lane::Merged);
}

#[tokio::test]
async fn setup_failure_needs_me() {
    let h = Harness::new(|spec, _| panic!("no run expected, got {:?}", spec.purpose)).await;
    let facts = ProjectFacts { setup: Some("echo broken setup; exit 1".into()), ..Default::default() };
    let s = h.create_session(facts).await;
    let t = h.create_task(s, "Needs setup", vec![]).await;
    h.wait_for("setup failed", SECS, |h| needs_me(h, t, HumanReason::SetupFailed)).await;
    assert_eq!(h.lane(t), Lane::Working);
    assert!(h.claude.specs().is_empty());
    let task = &h.state().tasks[&t];
    assert!(task.worktree.is_some());
    assert!(task.setup_output.contains("broken setup"), "{}", task.setup_output);
}

#[tokio::test]
async fn worktree_failure_needs_me() {
    let h = Harness::new(|spec, _| panic!("no run expected, got {:?}", spec.purpose)).await;
    let facts = ProjectFacts { copy_files: vec!["../outside".into()], ..Default::default() };
    let s = h.create_session(facts).await;
    let t = h.create_task(s, "Bad facts", vec![]).await;
    h.wait_for("setup failed", SECS, |h| needs_me(h, t, HumanReason::SetupFailed)).await;
    // R10: no worktree, yet the task is not hidden in Queued
    assert_eq!(h.lane(t), Lane::Working);
    let task = &h.state().tasks[&t];
    assert!(task.worktree.is_none());
    assert!(task.setup_output.contains("../outside"), "{}", task.setup_output);
    assert!(h.claude.specs().is_empty());
}

#[tokio::test]
async fn caps_hold_third_task_in_session() {
    let h = Harness::new(|_, _| hangs()).await;
    let s = h.create_session(ProjectFacts::default()).await;
    let mut tasks = Vec::new();
    for title in ["one", "two", "three"] {
        tasks.push(h.create_task(s, title, vec![]).await);
    }
    h.wait_for("two running", SECS, |h| h.claude.specs().len() == 2).await;
    h.wait_for("third waits for a slot", SECS, |h| h.card(tasks[2]).flags == [Flag::Waiting(QueuedReason::Slot)])
        .await;
    h.tick().await;
    tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    assert_eq!(h.claude.specs().len(), 2);
    assert_eq!(h.lane(tasks[2]), Lane::Queued);
    assert!(h.specs_for(tasks[2]).is_empty());
    assert_eq!((h.lane(tasks[0]), h.lane(tasks[1])), (Lane::Working, Lane::Working));
}

#[tokio::test]
async fn threshold_holds_new_work() {
    let h = Harness::new(|_, _| hangs()).await;
    *h.probe_five_hour.lock().unwrap() = 0.80;
    let t = h.create_session_and_task("Later").await;
    h.wait_for("held for quota", SECS, |h| h.card(t).flags == [Flag::Waiting(QueuedReason::Quota)]).await;
    for _ in 0..3 {
        h.advance_clock(MINUTE);
        h.tick().await;
    }
    tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    assert_eq!(h.lane(t), Lane::Queued);
    assert!(h.claude.specs().is_empty());
    // the reading is fresh, so no second probe
    assert_eq!(h.probe_calls.load(Ordering::SeqCst), 1);

    // once the reading is stale, the probe runs again and the window has room
    *h.probe_five_hour.lock().unwrap() = 0.20;
    h.tick_until("started after a new reading", MINUTE, SECS, |h| !h.claude.specs().is_empty()).await;
    assert_eq!(h.probe_calls.load(Ordering::SeqCst), 2);
}

#[tokio::test]
async fn stale_reading_triggers_probe() {
    let h = Harness::new(|_, _| hangs()).await;
    let probes = || h.probe_calls.load(Ordering::SeqCst);
    let t1 = h.create_session_and_task("one").await;
    h.wait_for("first run", SECS, |h| h.specs_for(t1).len() == 1).await;
    assert_eq!(probes(), 1);

    h.advance_clock(11 * MINUTE);
    let t2 = h.create_session_and_task("two").await;
    h.wait_for("second run", SECS, |h| h.specs_for(t2).len() == 1).await;
    assert_eq!(probes(), 2);
    // the reading came before the run started
    let events = h.events();
    let reading = events.iter().rposition(|e| matches!(e.kind, EventKind::UsageReading { .. })).unwrap();
    let started = events
        .iter()
        .position(|e| e.task == Some(t2) && matches!(e.kind, EventKind::RunStarted { .. }))
        .unwrap();
    assert!(reading < started);

    h.advance_clock(9 * MINUTE);
    let t3 = h.create_session_and_task("three").await;
    h.wait_for("third run", SECS, |h| h.specs_for(t3).len() == 1).await;
    assert_eq!(probes(), 2);
}

#[tokio::test]
async fn pr_missing_after_open_pr_needs_me() {
    let h = Harness::new(|spec, _| match (spec.role, spec.purpose) {
        (Role::Builder, Purpose::Initial) => builds("a.txt", "a\n"),
        (Role::Reviewer, Purpose::Review) => says(CLEAN_REVIEW),
        (Role::Builder, Purpose::OpenPr) => says("I could not push.\n\nStatus: done"),
        other => panic!("unexpected run {other:?}"),
    })
    .await;
    let t = h.create_session_and_task("No PR").await;
    h.wait_for("PR asked for", SECS, |h| h.state().tasks[&t].open_pr_requested && h.state().active_run(t).is_none())
        .await;
    assert_eq!(h.lane(t), Lane::Ci);
    // one more look ten seconds later is not enough to give up
    h.advance_clock(10_000);
    h.tick().await;
    tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    assert!(!h.state().tasks[&t].pr_missing);
    h.tick_until("PR missing", 10_000, SECS, |h| needs_me(h, t, HumanReason::PrNotFound)).await;
    assert_eq!(h.lane(t), Lane::Working);
    let missing = h.events().iter().filter(|e| e.kind == EventKind::PrMissing).count();
    assert_eq!(missing, 1);
}

#[tokio::test]
async fn crash_needs_me_then_retry() {
    let h = Harness::new(|spec, _| match spec.purpose {
        Purpose::Initial => FakeOutcome { final_message: String::new(), actions: vec![FakeAction::Crash] },
        Purpose::Resume => hangs(),
        other => panic!("unexpected run {other:?}"),
    })
    .await;
    let t = h.create_session_and_task("Crashy").await;
    h.wait_for("crash", SECS, |h| needs_me(h, t, HumanReason::RunCrashed)).await;
    assert_eq!(h.lane(t), Lane::Working);
    h.command(Command::RetryTask { task: t }).await;
    h.wait_for("resumed", SECS, |h| h.specs_for(t).len() == 2).await;
    let spec = &h.specs_for(t)[1];
    assert_eq!((spec.role, spec.purpose), (Role::Builder, Purpose::Resume));
    assert_eq!(spec.resume.as_deref(), Some("fake-session-1"));
    assert!(spec.prompt.contains(RESUME_NOTE), "{}", spec.prompt);
}

#[tokio::test]
async fn commands_are_validated() {
    let h = Harness::new(|_, _| hangs()).await;
    let err = h
        .engine
        .command(Command::MessageAgent { task: regie_core::TaskId(9), text: "x".into(), level: None }, Actor::Frederic)
        .await;
    assert_eq!(err, Err("task T9 not found".to_string()));
    let s = h.create_session(ProjectFacts::default()).await;
    let state = h.state();
    assert_eq!(state.sessions[&s].repos[0].github.as_deref(), Some("o/r"));
    assert_eq!(state.sessions[&s].repos[0].path, h.repo_path());
}

#[tokio::test]
async fn raise_level_only_records_the_new_level() {
    let h = Harness::new(|spec, _| match spec.purpose {
        Purpose::Initial => hangs(),
        other => panic!("unexpected run {other:?}"),
    })
    .await;
    let t = h.create_session_and_task("Long job").await;
    h.wait_for("builder running", SECS, |h| h.state().active_run(t).is_some()).await;
    h.command(Command::RaiseLevel { task: t, level: regie_core::Level::Hard }).await;
    assert_eq!(h.state().tasks[&t].level, regie_core::Level::Hard);
    assert!(h.events().iter().any(|e| e.kind == EventKind::LevelRaised { level: regie_core::Level::Hard }));
    assert!(!h.events().iter().any(|e| matches!(e.kind, EventKind::MessageQueued { .. })));
    assert!(h.claude.controls().is_empty());
    assert_eq!(h.card(t).level, regie_core::Level::Hard);
    assert!(h.card(t).active.is_some());
}
