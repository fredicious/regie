//! The `regie` command line: arguments, what each subcommand sends, and `serve`.

use std::future::Future;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use anyhow::Context;
use clap::{Args, Parser, Subcommand, ValueEnum};
use regie_core::{
    BoardView, Command, CommandResult, Flag, HumanReason, Lane, Level, Millis, ProjectFacts,
    QueuedReason, SessionId, Settings, TaskId,
};

use crate::agents::{probe, ClaudeCli};
use crate::client::Client;
use crate::engine::{self, BoxFuture, Deps};
use crate::github::GhCli;
use crate::paths::DataDir;
use crate::server::DEFAULT_PORT;

#[derive(Parser, Debug)]
#[command(name = "regie", version)]
pub struct Cli {
    /// Where Régie keeps its files (default `~/.regie`).
    #[arg(long, global = true, value_name = "PATH")]
    pub data_dir: Option<PathBuf>,
    #[command(subcommand)]
    pub command: Option<Sub>,
}

#[derive(Subcommand, Debug)]
pub enum Sub {
    /// Run the engine and the local HTTP API.
    Serve {
        #[arg(long, default_value_t = DEFAULT_PORT)]
        port: u16,
        /// Do not open the dashboard.
        #[arg(long)]
        no_open: bool,
    },
    Session {
        #[command(subcommand)]
        action: SessionSub,
    },
    Project {
        #[command(subcommand)]
        action: ProjectSub,
    },
    Task {
        #[command(subcommand)]
        action: TaskSub,
    },
    /// Pause one session, or everything with --all.
    Pause(Scope),
    /// Resume one session, or everything with --all.
    Resume(Scope),
    /// Print one line per task card.
    Board {
        #[arg(long, value_parser = parse_session)]
        session: Option<SessionId>,
    },
}

#[derive(Subcommand, Debug)]
pub enum SessionSub {
    /// Create a session on one or more repos.
    New {
        name: String,
        #[arg(required = true)]
        repos: Vec<String>,
    },
}

#[derive(Subcommand, Debug)]
pub enum ProjectSub {
    /// Set a repo's facts (setup, build, test, definition of done) from a JSON file.
    Set {
        repo: String,
        #[arg(long)]
        file: PathBuf,
    },
}

#[derive(Subcommand, Debug)]
pub enum TaskSub {
    New {
        #[arg(long, value_parser = parse_session)]
        session: SessionId,
        #[arg(long)]
        repo: String,
        #[arg(long)]
        title: String,
        /// The instruction, or `@file` to read it from a file.
        #[arg(long)]
        instruction: String,
        #[arg(long = "ticket")]
        tickets: Vec<String>,
        #[arg(long = "blocked-by", value_parser = parse_task)]
        blocked_by: Vec<TaskId>,
        #[arg(long, value_enum, default_value_t = LevelArg::Light)]
        level: LevelArg,
    },
    /// Send the task's agent a message.
    Message {
        #[arg(value_parser = parse_task)]
        task: TaskId,
        text: String,
    },
    Stop {
        #[arg(value_parser = parse_task)]
        task: TaskId,
    },
    Retry {
        #[arg(value_parser = parse_task)]
        task: TaskId,
    },
    Cancel {
        #[arg(value_parser = parse_task)]
        task: TaskId,
        #[arg(long)]
        close_pr: bool,
    },
}

#[derive(Args, Debug)]
pub struct Scope {
    #[arg(value_parser = parse_session, conflicts_with = "all")]
    pub session: Option<SessionId>,
    #[arg(long)]
    pub all: bool,
}

#[derive(ValueEnum, Clone, Copy, Debug, PartialEq, Eq)]
pub enum LevelArg {
    Light,
    Standard,
    Hard,
}

impl From<LevelArg> for Level {
    fn from(l: LevelArg) -> Level {
        match l {
            LevelArg::Light => Level::Light,
            LevelArg::Standard => Level::Standard,
            LevelArg::Hard => Level::Hard,
        }
    }
}

/// `T3` and `3` are both task 3.
pub fn parse_task(s: &str) -> Result<TaskId, String> {
    let digits = s.strip_prefix(['T', 't']).unwrap_or(s);
    digits
        .parse::<i64>()
        .map(TaskId)
        .map_err(|_| format!("`{s}` is not a task id (use T3 or 3)"))
}

pub fn parse_session(s: &str) -> Result<SessionId, String> {
    let digits = s.strip_prefix(['S', 's']).unwrap_or(s);
    digits
        .parse::<i64>()
        .map(SessionId)
        .map_err(|_| format!("`{s}` is not a session id"))
}

fn canonical(repo: &str) -> anyhow::Result<String> {
    let path = std::fs::canonicalize(repo)
        .with_context(|| format!("{repo} is not a directory I can read"))?;
    Ok(path.to_string_lossy().into_owned())
}

impl Cli {
    pub fn data(&self) -> DataDir {
        self.data_dir
            .clone()
            .map(|root| DataDir { root })
            .unwrap_or_default()
    }

    /// The command a subcommand sends, if it sends one (`serve`, `board` and the
    /// bare `regie` do not). Reads `@file` instructions and facts files, and
    /// makes repo paths canonical.
    pub fn to_command(&self) -> anyhow::Result<Option<Command>> {
        let Some(sub) = &self.command else {
            return Ok(None);
        };
        let cmd = match sub {
            Sub::Serve { .. } | Sub::Board { .. } => return Ok(None),
            Sub::Session {
                action: SessionSub::New { name, repos },
            } => Command::CreateSession {
                name: name.clone(),
                repos: repos
                    .iter()
                    .map(|r| canonical(r))
                    .collect::<anyhow::Result<_>>()?,
            },
            Sub::Project {
                action: ProjectSub::Set { repo, file },
            } => {
                let text = std::fs::read_to_string(file)
                    .with_context(|| format!("reading {}", file.display()))?;
                let facts: ProjectFacts = serde_json::from_str(&text)
                    .with_context(|| format!("{} is not valid project facts", file.display()))?;
                Command::SetProjectFacts {
                    repo: canonical(repo)?,
                    facts,
                }
            }
            Sub::Task { action } => match action {
                TaskSub::New {
                    session,
                    repo,
                    title,
                    instruction,
                    tickets,
                    blocked_by,
                    level,
                } => Command::CreateTask {
                    session: *session,
                    repo: canonical(repo)?,
                    title: title.clone(),
                    instruction: instruction_text(instruction)?,
                    tickets: tickets.clone(),
                    blocked_by: blocked_by.clone(),
                    level: (*level).into(),
                    paths: vec![],
                    provider: None,
                },
                TaskSub::Message { task, text } => Command::MessageAgent {
                    task: *task,
                    text: text.clone(),
                    level: None,
                },
                TaskSub::Stop { task } => Command::StopAgent { task: *task },
                TaskSub::Retry { task } => Command::RetryTask { task: *task },
                TaskSub::Cancel { task, close_pr } => Command::CancelTask {
                    task: *task,
                    close_pr: *close_pr,
                },
            },
            Sub::Pause(scope) => match (scope.session, scope.all) {
                (Some(session), _) => Command::PauseSession { session },
                (None, true) => Command::PauseAll,
                (None, false) => anyhow::bail!("say which session to pause, or --all"),
            },
            Sub::Resume(scope) => match (scope.session, scope.all) {
                (Some(session), _) => Command::ResumeSession { session },
                (None, true) => Command::ResumeAll,
                (None, false) => anyhow::bail!("say which session to resume, or --all"),
            },
        };
        Ok(Some(cmd))
    }
}

fn instruction_text(arg: &str) -> anyhow::Result<String> {
    match arg.strip_prefix('@') {
        Some(file) => std::fs::read_to_string(file)
            .with_context(|| format!("reading the instruction from {file}")),
        None => Ok(arg.to_string()),
    }
}

/// Runs the parsed command line.
pub async fn run(cli: Cli) -> anyhow::Result<()> {
    let data = cli.data();
    match &cli.command {
        None => open_dashboard(&data).await,
        Some(Sub::Serve { port, no_open }) => serve_command(data, *port, *no_open).await,
        Some(Sub::Board { session }) => {
            let client = Client::from_data_dir(&data)?;
            let sessions: Vec<SessionId> = match session {
                Some(s) => vec![*s],
                None => client.sessions().await?.iter().map(|s| s.id).collect(),
            };
            for s in &sessions {
                let board = client.board(*s).await?;
                if session.is_none() {
                    println!("session {}  {}", board.session.id.0, board.session.name);
                }
                for line in board_lines(&board) {
                    println!("{line}");
                }
            }
            Ok(())
        }
        Some(_) => {
            let cmd = cli
                .to_command()?
                .expect("every other subcommand sends a command");
            let client = Client::from_data_dir(&data)?;
            match client.command(&cmd).await? {
                CommandResult::Session(s) => println!("session {}", s.0),
                CommandResult::Task(t) => println!("{t}"),
                CommandResult::Ok => {}
            }
            Ok(())
        }
    }
}

fn reason_label(r: HumanReason) -> String {
    match r {
        HumanReason::BuilderQuestion => "builder question".into(),
        HumanReason::SetupFailed => "setup failed".into(),
        HumanReason::ReviewUnparsed => "review unreadable".into(),
        HumanReason::VerifyFailed => "verify failed".into(),
        HumanReason::CapReached(kind) => format!("cap reached ({kind:?})"),
        HumanReason::ChangesRequested => "changes requested".into(),
        HumanReason::PrNotFound => "PR not found".into(),
        HumanReason::PrClosed => "PR closed".into(),
        HumanReason::FixNotPushed(kind) => format!("fix not pushed ({kind:?})"),
        HumanReason::RunCrashed => "run crashed".into(),
        HumanReason::AgentStopped => "agent stopped".into(),
    }
}

fn lane_label(l: Lane) -> &'static str {
    match l {
        Lane::Queued => "Queued",
        Lane::Working => "Working",
        Lane::InReview => "In review",
        Lane::Ci => "CI",
        Lane::ReadyToMerge => "Ready to merge",
        Lane::Merged => "Merged",
        Lane::Cancelled => "Cancelled",
    }
}

fn flag_label(f: &Flag) -> String {
    match f {
        Flag::NeedsMe(r) => format!("[needs me: {}]", reason_label(*r)),
        Flag::Paused => "[paused]".into(),
        Flag::Waiting(q) => format!(
            "[waiting: {}]",
            match q {
                QueuedReason::Slot => "slot",
                QueuedReason::Quota => "quota",
                QueuedReason::Dependency => "dependency",
                QueuedReason::Paused => "paused",
            }
        ),
    }
}

/// `T3  Working  [needs me: builder question]  Add CSV export`
pub fn board_lines(board: &BoardView) -> Vec<String> {
    board
        .cards
        .iter()
        .map(|c| {
            let mut parts = vec![c.id.to_string(), lane_label(c.lane).to_string()];
            parts.extend(c.flags.iter().map(flag_label));
            parts.push(c.title.clone());
            parts.join("  ")
        })
        .collect()
}

fn browser_url(client: &Client) -> String {
    format!("{}/?token={}", client.base, client.token)
}

fn open_browser(url: &str) {
    if let Err(e) = std::process::Command::new("open").arg(url).status() {
        eprintln!("regie: could not open the browser: {e}; open {url}");
    }
}

/// A client for a server that answers health, if one does.
async fn live_client(data: &DataDir) -> Option<Client> {
    let client = Client::from_data_dir(data).ok()?;
    client.health().await.then_some(client)
}

/// Opens the dashboard, starting `regie serve --no-open` first when nothing answers.
async fn open_dashboard(data: &DataDir) -> anyhow::Result<()> {
    if let Some(client) = live_client(data).await {
        open_browser(&browser_url(&client));
        return Ok(());
    }
    data.ensure()?;
    let log = data.root.join("server.log");
    let out = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log)
        .with_context(|| format!("opening {}", log.display()))?;
    let err = out.try_clone()?;
    let exe = std::env::current_exe().context("finding my own executable")?;
    {
        use std::os::unix::process::CommandExt;
        std::process::Command::new(exe)
            .args(["serve", "--no-open", "--data-dir"])
            .arg(&data.root)
            .stdin(std::process::Stdio::null())
            .stdout(out)
            .stderr(err)
            .process_group(0)
            .spawn()
            .context("starting `regie serve`")?;
    }
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    while tokio::time::Instant::now() < deadline {
        if let Some(client) = live_client(data).await {
            open_browser(&browser_url(&client));
            return Ok(());
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    anyhow::bail!(
        "Régie did not start within 5 seconds; see {}",
        log.display()
    )
}

/// Whether `REGIE_RUN_ANYWAY` asks to start work past the usage thresholds: only `1` does.
pub fn run_anyway(var: Option<std::ffi::OsString>) -> bool {
    var.is_some_and(|v| v == "1")
}

/// Starts the engine with the real Claude and gh, serves the API, and stops
/// cleanly on SIGINT or SIGTERM.
pub async fn serve_command(data: DataDir, port: u16, no_open: bool) -> anyhow::Result<()> {
    data.ensure()?;
    // Held until the process ends; the engine must not start (and recover) under a live server.
    let _lock = lock_data_dir(&data)?;
    let claude = PathBuf::from("claude");
    let probe_dir = data.root.clone();
    let deps = Deps {
        runner: Arc::new(ClaudeCli {
            bin: claude.clone(),
            runs_dir: data.runs(),
        }),
        github: Arc::new(GhCli::default()),
        probe: Arc::new(
            move || -> BoxFuture<'static, anyhow::Result<regie_core::Reading>> {
                let (bin, dir) = (claude.clone(), probe_dir.clone());
                Box::pin(async move { probe(&bin, &dir).await })
            },
        ),
        clock: Arc::new(|| {
            let ms = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map_or(0, |d| d.as_millis() as i64);
            Millis(ms)
        }),
        caffeinate: true,
        run_anyway: run_anyway(std::env::var_os("REGIE_RUN_ANYWAY")),
    };
    if deps.run_anyway {
        eprintln!("regie: REGIE_RUN_ANYWAY=1: usage thresholds and limit blocks are ignored; slot caps still hold");
    }
    let engine = engine::start(data.clone(), Settings::default(), deps).await?;
    if !no_open {
        let data = data.clone();
        tokio::spawn(async move {
            for _ in 0..50 {
                if let Some(client) = live_client(&data).await {
                    open_browser(&browser_url(&client));
                    return;
                }
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
        });
    }
    let signals = async {
        use tokio::signal::unix::{signal, SignalKind};
        let (mut term, mut int) = match (
            signal(SignalKind::terminate()),
            signal(SignalKind::interrupt()),
        ) {
            (Ok(t), Ok(i)) => (t, i),
            _ => return std::future::pending().await,
        };
        tokio::select! { _ = term.recv() => {}, _ = int.recv() => {} }
    };
    serve_until(engine, data, port, signals).await
}

/// Serves until `shutdown` completes (or the server fails), then stops every
/// agent as `Interrupted` through the engine and removes `server.json`.
pub async fn serve_until(
    engine: engine::EngineHandle,
    data: DataDir,
    port: u16,
    shutdown: impl Future<Output = ()>,
) -> anyhow::Result<()> {
    let server = tokio::spawn(crate::server::serve(engine.clone(), data.clone(), port));
    let result = tokio::select! {
        r = server => Some(r.map_err(anyhow::Error::from).and_then(|r| r)),
        _ = shutdown => None,
    };
    engine.stop_all_and_shutdown().await;
    remove_server_json(&data.server_json());
    result.unwrap_or(Ok(()))
}

/// Takes the exclusive lock on `<data>/regie.lock`, or says that Régie already runs there.
pub fn lock_data_dir(data: &DataDir) -> anyhow::Result<std::fs::File> {
    use std::os::fd::AsRawFd;
    let path = data.root.join("regie.lock");
    let file = std::fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(&path)
        .with_context(|| format!("opening {}", path.display()))?;
    // SAFETY: flock on a file descriptor we own.
    if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        anyhow::bail!(
            "Régie is already running on {} (it holds {})",
            data.root.display(),
            path.display()
        );
    }
    Ok(file)
}

/// Removes `server.json` only when it is ours (a live server's must stay).
fn remove_server_json(path: &Path) {
    let ours = std::fs::read_to_string(path)
        .ok()
        .and_then(|t| serde_json::from_str::<crate::server::ServerInfo>(&t).ok())
        .is_some_and(|i| i.pid == std::process::id());
    if !ours {
        return;
    }
    if let Err(e) = std::fs::remove_file(path) {
        if e.kind() != std::io::ErrorKind::NotFound {
            eprintln!("regie: could not remove {}: {e}", path.display());
        }
    }
}
