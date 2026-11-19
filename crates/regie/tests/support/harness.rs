//! An engine on fakes: a real temp repo and data dir, `FakeClaude`, `FakeGitHub`,
//! a clock the test sets and a probe it scripts. Nothing waits on real intervals:
//! tests move the clock and send `Input::Tick` themselves.

use std::path::Path;
use std::sync::atomic::{AtomicI64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use regie::agents::RunSpec;
use regie::engine::{self, BoxFuture, Deps, EngineHandle, Input};
use regie::paths::DataDir;
use regie::store::Store;
use regie_core::{
    lane, next_step, Actor, Checks, Command, CommandResult, Event, Lane, Level, MergeState, Millis, PrLifecycle,
    PrState, ProjectFacts, Reading, SessionId, Settings, State, TaskCard, TaskId, Window,
};

use super::claude::{FakeAction, FakeClaude, FakeOutcome};
use super::github::FakeGitHub;
use super::repo::{git, TempRepo};

pub const MINUTE: i64 = 60_000;
const HOUR: i64 = 60 * MINUTE;
/// 2026-11-19T00:00:00Z.
const START: i64 = 1_795_046_400_000;

pub struct Harness {
    pub repo: TempRepo,
    _data_dir: tempfile::TempDir,
    pub data: DataDir,
    pub claude: Arc<FakeClaude>,
    pub gh: Arc<FakeGitHub>,
    pub now: Arc<AtomicI64>,
    pub probe_calls: Arc<AtomicUsize>,
    /// The five-hour use the probe reports.
    pub probe_five_hour: Arc<Mutex<f64>>,
    pub engine: EngineHandle,
}

impl Harness {
    /// `script` decides each run; it can change the fake GitHub as the agent would.
    pub async fn new(script: impl Fn(&RunSpec, &FakeGitHub) -> FakeOutcome + Send + Sync + 'static) -> Harness {
        let repo = TempRepo::new();
        fake_github_origin(&repo);
        let data_dir = tempfile::tempdir().unwrap();
        let data = DataDir { root: data_dir.path().join(".regie") };
        let gh = Arc::new(FakeGitHub::new());
        let gh_for_script = gh.clone();
        let claude = Arc::new(FakeClaude::new(move |spec| script(spec, &gh_for_script)));
        let now = Arc::new(AtomicI64::new(START));
        let probe_calls = Arc::new(AtomicUsize::new(0));
        let probe_five_hour = Arc::new(Mutex::new(0.10));
        let probe = {
            let (now, calls, used) = (now.clone(), probe_calls.clone(), probe_five_hour.clone());
            Arc::new(move || -> BoxFuture<'static, anyhow::Result<Reading>> {
                calls.fetch_add(1, Ordering::SeqCst);
                let at = now.load(Ordering::SeqCst);
                let used = *used.lock().unwrap();
                Box::pin(async move { Ok(reading(used, at)) })
            })
        };
        let clock = {
            let now = now.clone();
            Arc::new(move || Millis(now.load(Ordering::SeqCst)))
        };
        let deps = Deps { runner: claude.clone(), github: gh.clone(), probe, clock, caffeinate: false };
        let engine = engine::start(data.clone(), Settings::default(), deps).await.unwrap();
        Harness { repo, _data_dir: data_dir, data, claude, gh, now, probe_calls, probe_five_hour, engine }
    }

    pub fn repo_path(&self) -> String {
        self.repo.work.to_str().unwrap().to_string()
    }

    pub async fn command(&self, cmd: Command) -> CommandResult {
        self.engine.command(cmd, Actor::Frederic).await.unwrap()
    }

    /// A session on the temp repo, with these project facts.
    pub async fn create_session(&self, facts: ProjectFacts) -> SessionId {
        let cmd = Command::CreateSession { name: "s".into(), repos: vec![self.repo_path()] };
        let CommandResult::Session(s) = self.command(cmd).await else { panic!("no session id") };
        self.command(Command::SetProjectFacts { repo: self.repo_path(), facts }).await;
        s
    }

    pub async fn create_task(&self, session: SessionId, title: &str, blocked_by: Vec<TaskId>) -> TaskId {
        let cmd = Command::CreateTask {
            session,
            repo: self.repo_path(),
            title: title.into(),
            instruction: format!("Please do: {title}"),
            tickets: vec![],
            blocked_by,
            level: Level::Standard,
            paths: vec![],
            provider: None,
        };
        let CommandResult::Task(t) = self.command(cmd).await else { panic!("no task id") };
        t
    }

    pub async fn create_session_and_task(&self, title: &str) -> TaskId {
        let s = self.create_session(ProjectFacts::default()).await;
        self.create_task(s, title, vec![]).await
    }

    pub async fn message(&self, task: TaskId, text: &str) {
        self.command(Command::MessageAgent { task, text: text.into(), level: None }).await;
    }

    pub async fn tick(&self) {
        self.engine.tx.send(Input::Tick).await.unwrap();
    }

    pub fn advance_clock(&self, ms: i64) {
        self.now.fetch_add(ms, Ordering::SeqCst);
    }

    pub fn state(&self) -> State {
        self.engine.snapshot.read().unwrap().state.clone()
    }

    pub fn card(&self, task: TaskId) -> TaskCard {
        let session = self.state().tasks[&task].session;
        let board = self.engine.board(session).unwrap();
        board.cards.into_iter().find(|c| c.id == task).unwrap()
    }

    pub fn lane(&self, task: TaskId) -> Lane {
        self.card(task).lane
    }

    /// Polls (every 10 ms of real time) until `cond` holds; panics after `secs`.
    pub async fn wait_for(&self, what: &str, secs: u64, mut cond: impl FnMut(&Harness) -> bool) {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(secs);
        while !cond(self) {
            if tokio::time::Instant::now() > deadline {
                panic!("timed out waiting for {what}; events:\n{}", self.event_names().join("\n"));
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    }

    pub async fn wait_lane(&self, task: TaskId, lane: Lane, secs: u64) {
        self.wait_for(&format!("{task} in {lane:?}"), secs, |h| h.lane(task) == lane).await;
    }

    /// Moves the clock by `step` and ticks until `cond` holds.
    pub async fn tick_until(&self, what: &str, step: i64, secs: u64, mut cond: impl FnMut(&Harness) -> bool) {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(secs);
        while !cond(self) {
            if tokio::time::Instant::now() > deadline {
                panic!("timed out ticking for {what}; events:\n{}", self.event_names().join("\n"));
            }
            self.advance_clock(step);
            self.tick().await;
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    }

    pub fn events(&self) -> Vec<Event> {
        Store::open(&self.data.db()).unwrap().load_all().unwrap()
    }

    fn event_names(&self) -> Vec<String> {
        self.events().iter().map(|e| format!("{:?} {:?}", e.task, e.kind)).collect()
    }

    /// The task's lanes after each event, replayed from the log, repeats removed.
    pub fn lane_history(&self, task: TaskId) -> Vec<Lane> {
        let settings = Settings::default();
        let mut state = State::default();
        let mut lanes: Vec<Lane> = Vec::new();
        for e in self.events() {
            state.apply(&e);
            let Some(t) = state.tasks.get(&task) else { continue };
            let active = state.active_run(task).map(|r| r.role);
            let (l, _) = lane(t, active, &next_step(t, &state, &settings), false, None);
            if lanes.last() != Some(&l) {
                lanes.push(l);
            }
        }
        lanes
    }

    pub fn specs_for(&self, task: TaskId) -> Vec<RunSpec> {
        let tag = format!("T{}", task.0);
        self.claude
            .specs()
            .into_iter()
            .filter(|s| s.env.iter().any(|(k, v)| k == "REGIE_TASK" && *v == tag))
            .collect()
    }
}

/// Origin looks like `git@github.com:o/r.git`, but fetches come from the local bare repo.
fn fake_github_origin(repo: &TempRepo) {
    let origin = repo.origin.to_str().unwrap();
    git(&repo.work, &["remote", "set-url", "origin", "git@github.com:o/r.git"]);
    git(&repo.work, &["config", "ssh.variant", "simple"]);
    git(&repo.work, &["config", "core.sshCommand", &format!("sh -c 'exec git upload-pack {origin}' x")]);
}

pub fn reading(five_hour: f64, at: i64) -> Reading {
    Reading {
        five_hour: Window { used: five_hour, resets_at: Millis(at + 5 * HOUR) },
        seven_day: Window { used: 0.10, resets_at: Millis(at + 72 * HOUR) },
        read_at: Millis(at),
    }
}

/// The branch the run's worktree is on.
pub fn branch_of(spec: &RunSpec) -> String {
    git(&spec.cwd, &["rev-parse", "--abbrev-ref", "HEAD"])
}

pub fn pr(number: u64, sha: &str, checks: Checks, merge_state: MergeState) -> PrState {
    PrState {
        number,
        url: format!("https://github.com/o/r/pull/{number}"),
        lifecycle: PrLifecycle::Open,
        checks,
        conflicting: false,
        merge_state,
        review_decision: None,
        head_sha: sha.into(),
    }
}

/// A run that finishes at once with `msg`.
pub fn says(msg: &str) -> FakeOutcome {
    FakeOutcome { final_message: msg.into(), actions: vec![] }
}

/// A builder run that writes `file`, commits and reports done.
pub fn builds(file: &str, content: &str) -> FakeOutcome {
    FakeOutcome {
        final_message: "Done.\n\nStatus: done".into(),
        actions: vec![FakeAction::WriteFile(file.into(), content.into()), FakeAction::Commit(format!("write {file}"))],
    }
}

pub fn hangs() -> FakeOutcome {
    FakeOutcome { final_message: String::new(), actions: vec![FakeAction::HangUntilControl] }
}

pub const CLEAN_REVIEW: &str = "Looks good.\n```json\n{\"findings\":[]}\n```";

/// A review with one blocking finding backed by a command.
pub fn blocking_review(title: &str) -> String {
    format!(
        "One problem.\n```json\n{{\"findings\":[{{\"title\":\"{title}\",\"kind\":\"bug\",\"blocking\":true,\
         \"evidence\":{{\"type\":\"command\",\"detail\":\"cargo test fails\"}}}}]}}\n```"
    )
}

pub fn exists(p: &str) -> bool {
    Path::new(p).exists()
}
