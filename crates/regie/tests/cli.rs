//! The CLI: argument parsing, the client against an in-process server, and shutdown.

mod support;

use std::time::Duration;

use clap::Parser;
use regie::cli::{board_lines, serve_until, Cli};
use regie::client::Client;
use regie_core::{
    Command, CommandResult, EventKind, Lane, Level, RunId, RunOutcome, SessionId, TaskId,
};
use support::harness::{hangs, says, Harness};

fn parse(args: &[&str]) -> Cli {
    Cli::try_parse_from(std::iter::once("regie").chain(args.iter().copied())).unwrap()
}

#[test]
fn parses_task_new() {
    let repo = tempfile::tempdir().unwrap();
    let repo_arg = repo.path().to_str().unwrap();
    let cli = parse(&[
        "task",
        "new",
        "--session",
        "1",
        "--repo",
        repo_arg,
        "--title",
        "Add CSV export",
        "--instruction",
        "Export it",
        "--ticket",
        "ABC-1",
        "--ticket",
        "ABC-2",
        "--blocked-by",
        "T2",
    ]);
    let cmd = cli.to_command().unwrap().unwrap();
    assert_eq!(
        cmd,
        Command::CreateTask {
            session: SessionId(1),
            repo: std::fs::canonicalize(repo.path())
                .unwrap()
                .to_str()
                .unwrap()
                .into(),
            title: "Add CSV export".into(),
            instruction: "Export it".into(),
            tickets: vec!["ABC-1".into(), "ABC-2".into()],
            blocked_by: vec![TaskId(2)],
            level: Level::Light,
            paths: vec![],
            provider: None,
        }
    );
}

#[test]
fn task_new_reads_instruction_from_a_file_and_takes_a_level() {
    let dir = tempfile::tempdir().unwrap();
    let file = dir.path().join("i.md");
    std::fs::write(&file, "From a file").unwrap();
    let at_file = format!("@{}", file.display());
    let cli = parse(&[
        "task",
        "new",
        "--session",
        "2",
        "--repo",
        dir.path().to_str().unwrap(),
        "--title",
        "t",
        "--instruction",
        &at_file,
        "--level",
        "hard",
    ]);
    let Command::CreateTask {
        instruction,
        level,
        session,
        ..
    } = cli.to_command().unwrap().unwrap()
    else {
        panic!()
    };
    assert_eq!(
        (instruction.as_str(), level, session),
        ("From a file", Level::Hard, SessionId(2))
    );
}

#[test]
fn task_ids_accept_t_prefix() {
    for arg in ["T3", "3"] {
        assert_eq!(
            parse(&["task", "stop", arg]).to_command().unwrap(),
            Some(Command::StopAgent { task: TaskId(3) }),
            "{arg}"
        );
    }
    assert_eq!(
        parse(&["task", "cancel", "T4", "--close-pr"])
            .to_command()
            .unwrap(),
        Some(Command::CancelTask {
            task: TaskId(4),
            close_pr: true
        })
    );
    assert_eq!(
        parse(&["task", "message", "5", "use sqlite"])
            .to_command()
            .unwrap(),
        Some(Command::MessageAgent {
            task: TaskId(5),
            text: "use sqlite".into(),
            level: None
        })
    );
    assert!(Cli::try_parse_from(["regie", "task", "stop", "Tx"]).is_err());
}

#[test]
fn pause_and_resume_scopes() {
    assert_eq!(
        parse(&["pause", "--all"]).to_command().unwrap(),
        Some(Command::PauseAll)
    );
    assert_eq!(
        parse(&["resume", "--all"]).to_command().unwrap(),
        Some(Command::ResumeAll)
    );
    assert_eq!(
        parse(&["pause", "2"]).to_command().unwrap(),
        Some(Command::PauseSession {
            session: SessionId(2)
        })
    );
    assert!(parse(&["pause"]).to_command().is_err());
    assert!(Cli::try_parse_from(["regie", "pause", "2", "--all"]).is_err());
}

#[test]
fn session_new_and_project_set_canonicalise_repos() {
    let repo = tempfile::tempdir().unwrap();
    let real = std::fs::canonicalize(repo.path())
        .unwrap()
        .to_str()
        .unwrap()
        .to_string();
    let linked = repo.path().join(".");
    let cmd = parse(&["session", "new", "work", linked.to_str().unwrap()])
        .to_command()
        .unwrap()
        .unwrap();
    assert_eq!(
        cmd,
        Command::CreateSession {
            name: "work".into(),
            repos: vec![real.clone()]
        }
    );

    let facts = repo.path().join("facts.json");
    std::fs::write(&facts, r#"{"setup":null,"build":null,"test":"cargo test","lint":null,"copy_files":[],"definition_of_done":"green"}"#)
        .unwrap();
    let cmd = parse(&[
        "project",
        "set",
        linked.to_str().unwrap(),
        "--file",
        facts.to_str().unwrap(),
    ])
    .to_command()
    .unwrap()
    .unwrap();
    let Command::SetProjectFacts { repo, facts } = cmd else {
        panic!()
    };
    assert_eq!(
        (
            repo,
            facts.test.as_deref(),
            facts.definition_of_done.as_str()
        ),
        (real, Some("cargo test"), "green")
    );
}

#[test]
fn serve_and_board_send_no_command() {
    assert_eq!(
        parse(&["serve", "--no-open", "--port", "0"])
            .to_command()
            .unwrap(),
        None
    );
    assert_eq!(parse(&["board"]).to_command().unwrap(), None);
    assert_eq!(parse(&[]).to_command().unwrap(), None);
}

/// A server on a free port with the harness engine; returns a client for it.
async fn in_process_server(h: &Harness) -> Client {
    tokio::spawn(regie::server::serve(h.engine.clone(), h.data.clone(), 0));
    for _ in 0..200 {
        if let Ok(client) = Client::from_data_dir(&h.data) {
            if client.health().await {
                return client;
            }
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    panic!("the server did not come up");
}

#[tokio::test]
async fn client_round_trip_against_server() {
    let h = Harness::new(|_, _| says("ok")).await;
    let client = in_process_server(&h).await;
    let repo = h.repo_path();

    let r = client
        .command(&Command::CreateSession {
            name: "s".into(),
            repos: vec![repo],
        })
        .await
        .unwrap();
    assert_eq!(r, CommandResult::Session(SessionId(1)));
    let sessions = client.sessions().await.unwrap();
    assert_eq!(sessions.len(), 1);
    assert_eq!(sessions[0].name, "s");
    assert!(client.board(SessionId(1)).await.unwrap().cards.is_empty());
}

#[tokio::test]
async fn client_prints_the_servers_error_verbatim() {
    let h = Harness::new(|_, _| says("ok")).await;
    let client = in_process_server(&h).await;
    let err = client
        .command(&Command::StopAgent { task: TaskId(9) })
        .await
        .unwrap_err();
    assert_eq!(err.to_string(), "task T9 not found");
    let err = client.board(SessionId(5)).await.unwrap_err();
    assert_eq!(err.to_string(), "session 5 not found");
}

#[tokio::test]
async fn client_needs_a_server_json() {
    let tmp = tempfile::tempdir().unwrap();
    let data = regie::paths::DataDir {
        root: tmp.path().to_path_buf(),
    };
    assert!(Client::from_data_dir(&data).is_err());
}

#[tokio::test]
async fn board_lines_show_lane_flags_and_title() {
    let h = Harness::new(|_, _| says("Status: question\n\nWhich database?")).await;
    let t = h.create_session_and_task("Add CSV export").await;
    h.wait_lane(t, Lane::Working, 10).await;
    h.wait_for("a question", 10, |h| {
        h.card(t)
            .flags
            .iter()
            .any(|f| matches!(f, regie_core::Flag::NeedsMe(_)))
    })
    .await;
    let session = h.state().tasks[&t].session;
    let lines = board_lines(&h.engine.board(session).unwrap());
    assert_eq!(
        lines,
        vec![format!(
            "T{}  Working  [needs me: builder question]  Add CSV export",
            t.0
        )]
    );
}

#[tokio::test]
async fn shutdown_ends_runs_interrupted_and_removes_server_json() {
    let h = Harness::new(|_, _| hangs()).await;
    let t = h.create_session_and_task("Long job").await;
    h.wait_for("a run", 10, |h| h.state().active_run(t).is_some())
        .await;

    let (stop, stopped) = tokio::sync::oneshot::channel::<()>();
    let serving = tokio::spawn(serve_until(h.engine.clone(), h.data.clone(), 0, async {
        let _ = stopped.await;
    }));
    let json = h.data.server_json();
    for _ in 0..200 {
        if json.exists() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    assert!(json.exists());

    stop.send(()).unwrap();
    tokio::time::timeout(Duration::from_secs(10), serving)
        .await
        .unwrap()
        .unwrap()
        .unwrap();

    assert!(!json.exists());
    let finishes: Vec<_> = h
        .events()
        .into_iter()
        .filter_map(|e| match e.kind {
            EventKind::RunFinished { run, outcome, .. } => Some((run, outcome)),
            _ => None,
        })
        .collect();
    assert_eq!(finishes, vec![(RunId(1), RunOutcome::Interrupted)]);
}

#[tokio::test]
async fn a_second_serve_on_a_live_data_dir_fails_and_touches_nothing() {
    let h = Harness::new(|_, _| hangs()).await;
    let t = h.create_session_and_task("Long job").await;
    h.wait_for("a run", 10, |h| h.state().active_run(t).is_some()).await;
    // The live server: it holds the lock and has written its server.json.
    h.data.ensure().unwrap();
    let _live = regie::cli::lock_data_dir(&h.data).unwrap();
    let info = r#"{"port":7317,"token":"live","pid":1}"#;
    std::fs::write(h.data.server_json(), info).unwrap();
    let events_before = h.events().len();

    let cli = parse(&["serve", "--no-open", "--port", "0", "--data-dir", h.data.root.to_str().unwrap()]);
    let err = regie::cli::run(cli).await.unwrap_err();
    assert!(err.to_string().contains("already running"), "{err}");

    assert_eq!(std::fs::read_to_string(h.data.server_json()).unwrap(), info);
    assert_eq!(h.events().len(), events_before);
    assert!(h.state().active_run(t).is_some());
}
