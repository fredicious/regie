//! Frédéric's controls (Standby, Fade to black, stop, cancel, retry) and picking
//! up after Régie is killed, on fakes.
mod support;

use std::process::Stdio;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use tokio::io::{AsyncBufReadExt, BufReader};

use regie::agents::AgentControl;
use regie::engine::recover;
use regie_core::{
    Actor, Checks, Command, Effort, Event, EventId, EventKind, Flag, HumanReason, Lane, Level,
    MergeState, Millis, NewEvent, ProjectFacts, Provider, Purpose, QueuedReason, RepoInfo, Role,
    RunId, RunOutcome, SessionId, State, TaskId, RESUME_NOTE,
};
use support::claude::{FakeAction, FakeOutcome};
use support::harness::{
    branch_of, builds, exists, hangs, pr, says, Harness, CLEAN_REVIEW, HOUR, MINUTE, START,
};
use support::repo::git;

const SECS: u64 = 15;
const DAY: i64 = 24 * HOUR;

fn needs_me(h: &Harness, t: TaskId, reason: HumanReason) -> bool {
    h.card(t).flags.contains(&Flag::NeedsMe(reason))
}

fn outcome(h: &Harness, run: RunId) -> Option<RunOutcome> {
    h.state().runs.get(&run).and_then(|r| r.outcome)
}

/// Ticks a few times, minutes apart, so anything that would start has had its chance.
async fn idle_ticks(h: &Harness) {
    for _ in 0..3 {
        h.advance_clock(MINUTE);
        h.tick().await;
    }
    tokio::time::sleep(Duration::from_millis(100)).await;
}

/// A builder that hangs on every run (initial or resumed) until it gets a control.
async fn hanging_builder() -> Harness {
    Harness::new(|spec, _| match (spec.role, spec.purpose) {
        (Role::Builder, Purpose::Initial | Purpose::Resume) => hangs(),
        other => panic!("unexpected run {other:?}"),
    })
    .await
}

fn paused_flags() -> [Flag; 2] {
    [Flag::Waiting(QueuedReason::Paused), Flag::Paused]
}

#[tokio::test]
async fn pause_interrupts_and_resume_continues() {
    let h = hanging_builder().await;
    let t = h.create_session_and_task("Long job").await;
    let s = h.state().tasks[&t].session;
    h.wait_for("builder running", SECS, |h| {
        h.state().active_run(t).is_some()
    })
    .await;

    h.command(Command::PauseSession { session: s }).await;
    h.wait_for("run paused", SECS, |h| {
        outcome(h, RunId(1)) == Some(RunOutcome::Paused)
    })
    .await;
    assert_eq!(h.claude.controls(), [(RunId(1), AgentControl::Interrupt)]);
    assert!(h.state().sessions[&s].paused);
    assert!(h
        .events()
        .iter()
        .any(|e| e.kind == EventKind::SessionPaused && e.session == Some(s)));
    h.wait_for("held while paused", SECS, |h| {
        h.card(t).flags == paused_flags()
    })
    .await;
    idle_ticks(&h).await;
    assert_eq!(h.lane(t), Lane::Working);
    assert_eq!(h.card(t).flags, paused_flags());
    assert_eq!(h.specs_for(t).len(), 1);

    h.command(Command::ResumeSession { session: s }).await;
    h.wait_for("resumed", SECS, |h| h.specs_for(t).len() == 2)
        .await;
    let spec = &h.specs_for(t)[1];
    assert_eq!((spec.role, spec.purpose), (Role::Builder, Purpose::Resume));
    assert_eq!(spec.resume.as_deref(), Some("fake-session-1"));
    assert!(spec.prompt.contains(RESUME_NOTE), "{}", spec.prompt);
    assert!(!h.state().sessions[&s].paused);
    h.wait_for("no flags once running", SECS, |h| {
        h.card(t).flags.is_empty()
    })
    .await;
    assert_eq!(h.lane(t), Lane::Working);
}

#[tokio::test]
async fn pause_all_holds_every_session() {
    let h = hanging_builder().await;
    let s1 = h.create_session(ProjectFacts::default()).await;
    let s2 = h.create_session(ProjectFacts::default()).await;
    let t1 = h.create_task(s1, "one", vec![]).await;
    let t2 = h.create_task(s2, "two", vec![]).await;
    h.wait_for("both running", SECS, |h| h.claude.specs().len() == 2)
        .await;

    h.command(Command::PauseAll).await;
    h.wait_for("both paused", SECS, |h| {
        [t1, t2]
            .iter()
            .all(|t| h.state().tasks[t].last_run_outcome == Some(RunOutcome::Paused))
    })
    .await;
    let mut interrupted: Vec<RunId> = h
        .claude
        .controls()
        .into_iter()
        .map(|(run, c)| {
            assert_eq!(c, AgentControl::Interrupt);
            run
        })
        .collect();
    interrupted.sort();
    assert_eq!(interrupted, [RunId(1), RunId(2)]);
    assert!(h.state().paused_all);
    // a new task in either session waits too
    let t3 = h.create_task(s1, "three", vec![]).await;
    h.wait_for("all held", SECS, |h| {
        [t1, t2, t3]
            .iter()
            .all(|t| h.card(*t).flags == paused_flags())
    })
    .await;
    idle_ticks(&h).await;
    assert_eq!(h.claude.specs().len(), 2);
    assert_eq!(
        (h.lane(t1), h.lane(t2), h.lane(t3)),
        (Lane::Working, Lane::Working, Lane::Queued)
    );

    h.command(Command::ResumeAll).await;
    h.wait_for("all running", SECS, |h| h.claude.specs().len() == 5)
        .await;
    assert!(!h.state().paused_all);
    for (t, native) in [(t1, "fake-session-1"), (t2, "fake-session-2")] {
        let spec = &h.specs_for(t)[1];
        assert_eq!(
            (spec.purpose, spec.resume.as_deref()),
            (Purpose::Resume, Some(native))
        );
    }
    assert_eq!(h.specs_for(t3)[0].purpose, Purpose::Initial);
}

#[tokio::test]
async fn pause_holds_a_task_in_setup() {
    let h = hanging_builder().await;
    let gate_dir = tempfile::tempdir().unwrap();
    let gate = gate_dir.path().join("go");
    // waits (up to 10 s) for the test to create the gate file
    let setup = format!(
        "i=0; while [ ! -f '{}' ] && [ $i -lt 500 ]; do sleep 0.02; i=$((i+1)); done",
        gate.display()
    );
    let s = h
        .create_session(ProjectFacts {
            setup: Some(setup),
            ..Default::default()
        })
        .await;
    let t = h.create_task(s, "Slow setup", vec![]).await;
    h.wait_for("worktree made, setup running", SECS, |h| {
        h.state().tasks[&t].worktree.is_some()
    })
    .await;
    assert_eq!(h.state().tasks[&t].setup_ok, None);

    h.command(Command::PauseSession { session: s }).await;
    std::fs::write(&gate, "").unwrap();
    h.wait_for("setup done", SECS, |h| {
        h.state().tasks[&t].setup_ok == Some(true)
    })
    .await;
    h.wait_for("held while paused", SECS, |h| {
        h.card(t).flags == paused_flags()
    })
    .await;
    idle_ticks(&h).await;
    assert!(h.claude.specs().is_empty());
    assert_eq!(h.lane(t), Lane::Working);

    h.command(Command::ResumeSession { session: s }).await;
    h.wait_for("first run", SECS, |h| h.specs_for(t).len() == 1)
        .await;
    assert_eq!(h.specs_for(t)[0].purpose, Purpose::Initial);
}

#[tokio::test]
async fn stop_agent_waits_for_frederic() {
    let h = hanging_builder().await;
    let t = h.create_session_and_task("Wrong track").await;
    h.wait_for("builder running", SECS, |h| {
        h.state().active_run(t).is_some()
    })
    .await;

    h.command(Command::StopAgent { task: t }).await;
    h.wait_for("stopped", SECS, |h| {
        needs_me(h, t, HumanReason::AgentStopped)
    })
    .await;
    assert_eq!(h.claude.controls(), [(RunId(1), AgentControl::Stop)]);
    assert_eq!(outcome(&h, RunId(1)), Some(RunOutcome::Stopped));
    assert_eq!(h.lane(t), Lane::Working);
    idle_ticks(&h).await;
    assert_eq!(h.specs_for(t).len(), 1);
    assert!(needs_me(&h, t, HumanReason::AgentStopped));
    // nothing left to stop
    let again = h
        .engine
        .command(Command::StopAgent { task: t }, Actor::Frederic)
        .await;
    assert_eq!(again, Err(format!("{t} has no running agent")));

    h.command(Command::RetryTask { task: t }).await;
    h.wait_for("resumed", SECS, |h| h.specs_for(t).len() == 2)
        .await;
    let spec = &h.specs_for(t)[1];
    assert_eq!((spec.role, spec.purpose), (Role::Builder, Purpose::Resume));
    assert_eq!(spec.resume.as_deref(), Some("fake-session-1"));
    assert!(spec.prompt.contains(RESUME_NOTE), "{}", spec.prompt);
    h.wait_for("running again", SECS, |h| h.card(t).flags.is_empty())
        .await;
}

#[tokio::test]
async fn cancel_closes_pr_and_cleans_up() {
    let failing = || Checks::Failure {
        failed: vec!["test".into()],
    };
    let h = Harness::new(move |spec, gh| match (spec.role, spec.purpose) {
        (Role::Builder, Purpose::Initial) => builds("a.txt", "a\n"),
        (Role::Reviewer, Purpose::Review) => says(CLEAN_REVIEW),
        (Role::Builder, Purpose::OpenPr) => {
            gh.set(
                &branch_of(spec),
                pr(9, "sha0", failing(), MergeState::Unstable),
            );
            says("Status: done")
        }
        (Role::Builder, Purpose::CiFix) => hangs(),
        other => panic!("unexpected run {other:?}"),
    })
    .await;
    let t = h.create_session_and_task("Not needed after all").await;
    h.wait_for("CI fix running", SECS, |h| {
        h.state()
            .active_run(t)
            .is_some_and(|r| r.purpose == Purpose::CiFix)
    })
    .await;
    let fix = h.state().active_run(t).unwrap().id;
    let wt = h.state().tasks[&t].worktree.clone().unwrap();
    let runs_before = h.specs_for(t).len();

    h.command(Command::CancelTask {
        task: t,
        close_pr: true,
    })
    .await;
    h.wait_for("cleaned up", SECS, |h| h.state().tasks[&t].cleaned)
        .await;
    // the running fix was stopped first, then the PR closed, then the worktree removed
    assert_eq!(h.claude.controls(), [(fix, AgentControl::Stop)]);
    assert_eq!(outcome(&h, fix), Some(RunOutcome::Stopped));
    assert_eq!(h.gh.closed(), [9]);
    let events = h.events();
    let at = |what: &str, f: &dyn Fn(&EventKind) -> bool| {
        events
            .iter()
            .position(|e| e.task == Some(t) && f(&e.kind))
            .unwrap_or_else(|| panic!("no {what}"))
    };
    let finished = at(
        "fix finished",
        &|k| matches!(k, EventKind::RunFinished { run, .. } if *run == fix),
    );
    let closed = at("PR closed", &|k| *k == EventKind::PrClosedByRegie);
    let cleaned = at("cleaned", &|k| *k == EventKind::WorktreeCleaned);
    assert!(
        finished < closed && closed < cleaned,
        "{finished} {closed} {cleaned}"
    );
    assert_eq!(h.lane(t), Lane::Cancelled);
    assert_eq!(h.lane_history(t).last(), Some(&Lane::Cancelled));
    assert!(!exists(&wt.path), "worktree still at {}", wt.path);
    assert!(!git(&h.repo.work, &["branch", "--list"]).contains("regie/"));
    idle_ticks(&h).await;
    assert_eq!(h.specs_for(t).len(), runs_before);
    assert_eq!(h.gh.closed(), [9]);
}

#[tokio::test]
async fn restart_resumes_interrupted_run() {
    let mut h = hanging_builder().await;
    let t = h.create_session_and_task("Survives a restart").await;
    h.wait_for("builder attached", SECS, |h| {
        h.state().tasks[&t].builder_native_session.is_some()
    })
    .await;

    // Régie is killed mid-run: the old loop records nothing more.
    h.restart().await;
    // at once, not on the first 5-second tick
    h.wait_for("resumed", 3, |h| h.specs_for(t).len() == 2)
        .await;
    let finishes: Vec<Event> = h
        .events()
        .into_iter()
        .filter(|e| matches!(e.kind, EventKind::RunFinished { run: RunId(1), .. }))
        .collect();
    assert_eq!(finishes.len(), 1, "{finishes:?}");
    assert_eq!(
        finishes[0].kind,
        EventKind::RunFinished {
            run: RunId(1),
            outcome: RunOutcome::Interrupted,
            final_message: None
        }
    );
    assert_eq!(finishes[0].actor, Actor::Regie);
    let spec = &h.specs_for(t)[1];
    assert_eq!((spec.role, spec.purpose), (Role::Builder, Purpose::Resume));
    assert_eq!(spec.resume.as_deref(), Some("fake-session-1"));
    assert!(spec.prompt.contains(RESUME_NOTE), "{}", spec.prompt);
    assert!(
        spec.prompt.contains("You were interrupted"),
        "{}",
        spec.prompt
    );
    h.wait_for("running again", SECS, |h| {
        h.state().active_run(t).is_some_and(|r| r.id == RunId(2))
    })
    .await;
    assert_eq!(h.lane(t), Lane::Working);
}

/// A run that hangs before it attaches (Claude still loading its setup).
fn before_attach(pid: Option<u32>) -> FakeOutcome {
    let spawned = pid.map(FakeAction::Spawned);
    FakeOutcome {
        final_message: String::new(),
        actions: spawned
            .into_iter()
            .chain([FakeAction::HangBeforeAttach])
            .collect(),
    }
}

fn attached(h: &Harness, run: RunId) -> bool {
    h.state()
        .runs
        .get(&run)
        .is_some_and(|r| r.native_session.is_some())
}

#[tokio::test]
async fn pause_before_attach_sends_the_instruction_again() {
    let h = Harness::new(|spec, _| match (spec.role, spec.purpose) {
        (Role::Builder, Purpose::Initial) => before_attach(None),
        (Role::Builder, Purpose::Resume) => hangs(),
        other => panic!("unexpected run {other:?}"),
    })
    .await;
    let t = h.create_session_and_task("Load slowly").await;
    let s = h.state().tasks[&t].session;
    h.wait_for("builder started", SECS, |h| {
        h.state().active_run(t).is_some()
    })
    .await;
    h.command(Command::PauseSession { session: s }).await;
    h.wait_for("run paused", SECS, |h| {
        outcome(h, RunId(1)) == Some(RunOutcome::Paused)
    })
    .await;
    assert!(!attached(&h, RunId(1)));

    h.command(Command::ResumeSession { session: s }).await;
    h.wait_for("sent again", SECS, |h| h.specs_for(t).len() == 2)
        .await;
    let spec = &h.specs_for(t)[1];
    assert_eq!((spec.role, spec.purpose), (Role::Builder, Purpose::Resume));
    // nothing to continue: a fresh agent with the task's instruction, not "continue"
    assert_eq!(spec.resume, None);
    assert!(
        spec.prompt.contains("Please do: Load slowly"),
        "{}",
        spec.prompt
    );
    assert!(!spec.prompt.contains(RESUME_NOTE), "{}", spec.prompt);
    h.wait_for("attached", SECS, |h| attached(h, RunId(2)))
        .await;
}

#[tokio::test]
async fn message_run_stopped_before_attach_keeps_the_message() {
    let h = Harness::new(|spec, _| match (spec.role, spec.purpose) {
        (Role::Builder, Purpose::Initial) => says("Which database should I use?\nStatus: question"),
        (Role::Builder, Purpose::Message) => before_attach(None),
        (Role::Builder, Purpose::Resume) => hangs(),
        other => panic!("unexpected run {other:?}"),
    })
    .await;
    let t = h.create_session_and_task("Store things").await;
    h.wait_for("question", SECS, |h| {
        needs_me(h, t, HumanReason::BuilderQuestion)
    })
    .await;
    h.message(t, "Use SQLite.").await;
    h.wait_for("message run", SECS, |h| {
        h.state()
            .active_run(t)
            .is_some_and(|r| r.purpose == Purpose::Message)
    })
    .await;
    // queued until the run attaches
    assert_eq!(h.state().tasks[&t].pending_messages, ["Use SQLite."]);
    h.command(Command::StopAgent { task: t }).await;
    h.wait_for("stopped", SECS, |h| {
        needs_me(h, t, HumanReason::AgentStopped)
    })
    .await;
    assert_eq!(h.state().tasks[&t].pending_messages, ["Use SQLite."]);

    h.command(Command::RetryTask { task: t }).await;
    h.wait_for("sent again", SECS, |h| h.specs_for(t).len() == 3)
        .await;
    let spec = &h.specs_for(t)[2];
    assert_eq!((spec.role, spec.purpose), (Role::Builder, Purpose::Resume));
    assert_eq!(spec.prompt, "Use SQLite.");
    assert_eq!(spec.resume.as_deref(), Some("fake-session-1"));
    h.wait_for("taken off the queue", SECS, |h| {
        h.state().tasks[&t].pending_messages.is_empty()
    })
    .await;
}

#[tokio::test]
async fn restart_stops_an_agent_that_never_attached() {
    // the agent's process, left running by a Régie killed before the agent attached
    let mut agent = tokio::process::Command::new("sleep")
        .arg("60")
        .process_group(0)
        .kill_on_drop(true)
        .spawn()
        .unwrap();
    let pid = agent.id().unwrap();
    tokio::spawn(async move { agent.wait().await });
    let mut h = Harness::new(move |spec, _| match (spec.role, spec.purpose) {
        (Role::Builder, Purpose::Initial) => before_attach(Some(pid)),
        (Role::Builder, Purpose::Resume) => hangs(),
        other => panic!("unexpected run {other:?}"),
    })
    .await;
    let t = h.create_session_and_task("Never attached").await;
    h.wait_for("pid recorded", SECS, |h| {
        h.state()
            .runs
            .get(&RunId(1))
            .is_some_and(|r| r.pid == Some(pid))
    })
    .await;
    assert!(!attached(&h, RunId(1)));

    h.restart().await;
    wait_dead(pid as i32).await;
    h.wait_for("sent again", SECS, |h| h.specs_for(t).len() == 2)
        .await;
    assert_eq!(outcome(&h, RunId(1)), Some(RunOutcome::Interrupted));
    let spec = &h.specs_for(t)[1];
    assert_eq!(
        (spec.purpose, spec.resume.as_deref()),
        (Purpose::Resume, None)
    );
    assert!(
        spec.prompt.contains("Please do: Never attached"),
        "{}",
        spec.prompt
    );
}

#[tokio::test]
async fn old_run_files_pruned() {
    let mut h = Harness::new(|_, _| hangs()).await;
    let runs = h.data.runs();
    let write = |name: &str, age: i64| {
        let path = runs.join(name);
        std::fs::write(&path, "{}\n").unwrap();
        let at = UNIX_EPOCH + Duration::from_millis((START - age) as u64);
        std::fs::File::options()
            .write(true)
            .open(&path)
            .unwrap()
            .set_modified(at)
            .unwrap();
        path
    };
    let old = write("1.jsonl", 15 * DAY);
    let recent = write("2.jsonl", 13 * DAY);
    let other = write("notes.txt", 30 * DAY);

    // kept 14 days: pruned when Régie starts...
    h.restart().await;
    assert!(!old.exists());
    assert!(recent.exists() && other.exists());
    // ...and once a day while it runs
    h.advance_clock(2 * DAY);
    h.tick().await;
    h.wait_for("pruned a day later", SECS, |_| !recent.exists())
        .await;
    assert!(other.exists());
}

fn wall_now() -> Millis {
    Millis(
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis() as i64,
    )
}

fn alive(pid: i32) -> bool {
    // SAFETY: signal 0 only checks that the process exists.
    unsafe { libc::kill(pid, 0) == 0 }
}

async fn wait_dead(pid: i32) {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(SECS);
    while alive(pid) {
        assert!(tokio::time::Instant::now() < deadline, "{pid} still alive");
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

/// A state with one unfinished run per `(pid, started_at)`.
fn state_with_runs(runs: &[(u32, Millis)]) -> State {
    let mut kinds = vec![(
        Millis(0),
        EventKind::SessionCreated {
            name: "s".into(),
            repos: vec![RepoInfo {
                path: "/r".into(),
                github: None,
            }],
        },
    )];
    kinds.push((
        Millis(0),
        EventKind::TaskCreated {
            repo: "/r".into(),
            title: "t".into(),
            instruction: "i".into(),
            tickets: vec![],
            blocked_by: vec![],
            level: Level::Standard,
            paths: vec![],
            provider: None,
        },
    ));
    for (i, (pid, at)) in runs.iter().enumerate() {
        let run = RunId(i as i64 + 1);
        let started = EventKind::RunStarted {
            run,
            role: Role::Builder,
            purpose: Purpose::Initial,
            provider: Provider::Claude,
            model: "m".into(),
            effort: Effort::Medium,
            resume_of: None,
        };
        kinds.push((*at, started));
        kinds.push((
            *at,
            EventKind::RunAttached {
                run,
                native_session: format!("n{}", run.0),
                pid: Some(*pid),
            },
        ));
    }
    let events: Vec<Event> = kinds
        .into_iter()
        .enumerate()
        .map(|(i, (at, kind))| Event {
            id: EventId(i as i64 + 1),
            at,
            session: Some(SessionId(1)),
            task: (i > 0).then_some(TaskId(1)),
            actor: Actor::Regie,
            kind,
        })
        .collect();
    State::replay(&events)
}

fn interrupted(run: i64, at: Millis) -> NewEvent {
    NewEvent {
        at,
        session: Some(SessionId(1)),
        task: Some(TaskId(1)),
        actor: Actor::Regie,
        kind: EventKind::RunFinished {
            run: RunId(run),
            outcome: RunOutcome::Interrupted,
            final_message: None,
        },
    }
}

#[tokio::test]
async fn recover_kills_the_agent_group() {
    // An agent left behind by a killed Régie: it ignores SIGTERM, and so does its tool process.
    let mut child = tokio::process::Command::new("sh")
        .args(["-c", "trap '' TERM; sleep 60 & echo $!; wait"])
        .process_group(0)
        .stdout(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .unwrap();
    let leader = child.id().unwrap();
    let mut lines = BufReader::new(child.stdout.take().unwrap()).lines();
    let tool: i32 = lines
        .next_line()
        .await
        .unwrap()
        .unwrap()
        .trim()
        .parse()
        .unwrap();
    // reap the leader as launchd would once its parent is gone
    tokio::spawn(async move { child.wait().await });
    let now = wall_now();
    let state = state_with_runs(&[(leader, Millis(now.0 - 2_000))]);

    let started = std::time::Instant::now();
    let events = recover(&state, now).await;
    assert!(
        started.elapsed() >= Duration::from_secs(5),
        "SIGKILL too early: {:?}",
        started.elapsed()
    );
    assert_eq!(events, [interrupted(1, now)]);
    wait_dead(leader as i32).await;
    wait_dead(tool).await;
}

#[tokio::test]
async fn recover_leaves_a_reused_pid_alone() {
    // pid reused by a process that leads no group of its own (a group signal to it reaches nobody)
    let mut a = tokio::process::Command::new("sleep")
        .arg("60")
        .kill_on_drop(true)
        .spawn()
        .unwrap();
    // pid reused by a group leader that started an hour after the run
    let mut b = tokio::process::Command::new("sleep")
        .arg("60")
        .process_group(0)
        .kill_on_drop(true)
        .spawn()
        .unwrap();
    let (pa, pb) = (a.id().unwrap(), b.id().unwrap());
    let now = wall_now();
    let state = state_with_runs(&[(pa, now), (pb, Millis(now.0 - HOUR))]);

    let started = std::time::Instant::now();
    let events = recover(&state, now).await;
    assert!(
        started.elapsed() < Duration::from_secs(2),
        "{:?}",
        started.elapsed()
    );
    assert_eq!(events, [interrupted(1, now), interrupted(2, now)]);
    // still running, a moment later (`try_wait` reaps a process that died, so a zombie does not pass)
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert!(a.try_wait().unwrap().is_none() && b.try_wait().unwrap().is_none());
    a.kill().await.unwrap();
    b.kill().await.unwrap();
}
