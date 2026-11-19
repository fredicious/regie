//! The HTTP server driven in-process: no socket, the router gets requests directly.

mod support;

use std::time::Duration;

use axum::body::{Body, Bytes};
use axum::http::{header, Request, Response, StatusCode};
use axum::Router;
use regie::server::{router, ServerInfo};
use regie_core::{Command, CommandResult, ProjectFacts, TaskId};
use support::harness::{says, Harness};
use tower::ServiceExt;

const TOKEN: &str = "secret-token";
const PORT: u16 = 7317;
const HOST: &str = "127.0.0.1:7317";

async fn app() -> (Harness, Router) {
    let h = Harness::new(|_, _| says("ok")).await;
    let info = ServerInfo {
        port: PORT,
        token: TOKEN.into(),
        pid: 1,
    };
    let r = router(h.engine.clone(), h.data.clone(), info);
    (h, r)
}

fn get(path: &str) -> axum::http::request::Builder {
    Request::builder()
        .uri(path)
        .header(header::HOST, HOST)
        .header(header::AUTHORIZATION, format!("Bearer {TOKEN}"))
}

fn post_command(cmd: &Command) -> Request<Body> {
    Request::builder()
        .method("POST")
        .uri("/api/commands")
        .header(header::HOST, HOST)
        .header(header::AUTHORIZATION, format!("Bearer {TOKEN}"))
        .header(header::CONTENT_TYPE, "application/json")
        .body(Body::from(serde_json::to_vec(cmd).unwrap()))
        .unwrap()
}

async fn body(r: Response<Body>) -> Bytes {
    axum::body::to_bytes(r.into_body(), 1 << 20).await.unwrap()
}

async fn json(r: Response<Body>) -> serde_json::Value {
    serde_json::from_slice(&body(r).await).unwrap()
}

#[tokio::test]
async fn health_with_a_valid_token() {
    let (_h, app) = app().await;
    let r = app
        .oneshot(get("/api/health").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(r.status(), StatusCode::OK);
    let v = json(r).await;
    assert_eq!(v["ok"], true);
    assert_eq!(v["version"], regie::VERSION);
}

#[tokio::test]
async fn cookie_is_accepted_as_well() {
    let (_h, app) = app().await;
    let req = Request::builder()
        .uri("/api/health")
        .header(header::HOST, "localhost:7317")
        .header(header::COOKIE, format!("a=b; regie_token={TOKEN}"))
        .body(Body::empty())
        .unwrap();
    assert_eq!(app.oneshot(req).await.unwrap().status(), StatusCode::OK);
}

#[tokio::test]
async fn rejects_missing_token() {
    let (_h, app) = app().await;
    let req = Request::builder()
        .uri("/api/health")
        .header(header::HOST, HOST)
        .body(Body::empty())
        .unwrap();
    assert_eq!(
        app.oneshot(req).await.unwrap().status(),
        StatusCode::UNAUTHORIZED
    );
}

#[tokio::test]
async fn rejects_wrong_token() {
    let (_h, app) = app().await;
    let req = Request::builder()
        .uri("/api/health")
        .header(header::HOST, HOST)
        .header(header::AUTHORIZATION, "Bearer secret-tokeX")
        .body(Body::empty())
        .unwrap();
    assert_eq!(
        app.oneshot(req).await.unwrap().status(),
        StatusCode::UNAUTHORIZED
    );
}

#[tokio::test]
async fn rejects_wrong_host() {
    let (_h, app) = app().await;
    for host in ["evil.com", "evil.com:7317", "127.0.0.1:9999"] {
        let req = Request::builder()
            .uri("/api/health")
            .header(header::HOST, host)
            .header(header::AUTHORIZATION, format!("Bearer {TOKEN}"))
            .body(Body::empty())
            .unwrap();
        assert_eq!(
            app.clone().oneshot(req).await.unwrap().status(),
            StatusCode::FORBIDDEN,
            "{host}"
        );
    }
}

#[tokio::test]
async fn rejects_foreign_origin_on_post() {
    let (_h, app) = app().await;
    let mut req = post_command(&Command::PauseAll);
    req.headers_mut()
        .insert(header::ORIGIN, "https://evil.com".parse().unwrap());
    assert_eq!(
        app.clone().oneshot(req).await.unwrap().status(),
        StatusCode::FORBIDDEN
    );
    let mut req = post_command(&Command::PauseAll);
    req.headers_mut()
        .insert(header::ORIGIN, "http://localhost:7317".parse().unwrap());
    assert_eq!(
        app.clone().oneshot(req).await.unwrap().status(),
        StatusCode::OK
    );
    // No Origin at all is the CLI.
    assert_eq!(
        app.oneshot(post_command(&Command::ResumeAll))
            .await
            .unwrap()
            .status(),
        StatusCode::OK
    );
}

#[tokio::test]
async fn token_query_sets_cookie() {
    let (_h, app) = app().await;
    let req = Request::builder()
        .uri(format!("/?token={TOKEN}"))
        .header(header::HOST, HOST)
        .body(Body::empty())
        .unwrap();
    let r = app.clone().oneshot(req).await.unwrap();
    assert_eq!(r.status(), StatusCode::SEE_OTHER);
    assert_eq!(r.headers()[header::LOCATION], "/");
    assert_eq!(
        r.headers()[header::SET_COOKIE],
        format!("regie_token={TOKEN}; HttpOnly; SameSite=Strict; Path=/")
    );
    // A wrong token in the query is just a 401.
    let req = Request::builder()
        .uri("/?token=nope")
        .header(header::HOST, HOST)
        .body(Body::empty())
        .unwrap();
    let r = app.oneshot(req).await.unwrap();
    assert_eq!(r.status(), StatusCode::UNAUTHORIZED);
    assert!(r.headers().get(header::SET_COOKIE).is_none());
}

fn web_is_built() -> bool {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../web/dist/index.html")
        .exists()
}

#[tokio::test]
async fn root_says_how_to_build_the_web_page_when_it_is_missing() {
    if web_is_built() {
        eprintln!("skipped: web/dist is built");
        return;
    }
    let (_h, app) = app().await;
    let r = app
        .oneshot(get("/").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(r.status(), StatusCode::OK);
    assert!(String::from_utf8(body(r).await.to_vec())
        .unwrap()
        .contains("make web"));
}

#[tokio::test]
async fn serves_embedded_index_when_built() {
    if !web_is_built() {
        eprintln!("skipped: web/dist is not built (run `make web`)");
        return;
    }
    let (_h, app) = app().await;
    let r = app
        .clone()
        .oneshot(get("/").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(r.status(), StatusCode::OK);
    assert_eq!(
        r.headers()[header::CONTENT_TYPE],
        "text/html; charset=utf-8"
    );
    assert_eq!(r.headers()[header::X_FRAME_OPTIONS], "DENY");
    assert_eq!(
        r.headers()[header::CONTENT_SECURITY_POLICY],
        "frame-ancestors 'none'"
    );
    assert_eq!(r.headers()[header::REFERRER_POLICY], "no-referrer");
    let html = String::from_utf8(body(r).await.to_vec()).unwrap();
    assert!(html.contains("<title>Régie</title>"), "{html}");

    // The page itself ("RÉGIE" is in the bundle): fetch the script the index points at.
    let src = html
        .split("src=\"")
        .nth(1)
        .and_then(|t| t.split('"').next())
        .expect("a script tag")
        .to_string();
    let r = app
        .oneshot(get(&src).body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(r.status(), StatusCode::OK);
    assert_eq!(
        r.headers()[header::CONTENT_TYPE],
        "text/javascript; charset=utf-8"
    );
    assert_eq!(r.headers()[header::X_FRAME_OPTIONS], "DENY");
    assert_eq!(
        r.headers()[header::CONTENT_SECURITY_POLICY],
        "frame-ancestors 'none'"
    );
    assert_eq!(r.headers()[header::REFERRER_POLICY], "no-referrer");
    assert!(String::from_utf8(body(r).await.to_vec())
        .unwrap()
        .contains("RÉGIE"));
}

#[tokio::test]
async fn create_session_and_task_by_command() {
    let (h, app) = app().await;
    let cmd = Command::CreateSession {
        name: "s".into(),
        repos: vec![h.repo_path()],
    };
    let r = app.clone().oneshot(post_command(&cmd)).await.unwrap();
    assert_eq!(r.status(), StatusCode::OK);
    let CommandResult::Session(s) =
        serde_json::from_value(json(r).await["result"].clone()).unwrap()
    else {
        panic!("not a session")
    };
    h.command(Command::SetProjectFacts {
        repo: h.repo_path(),
        facts: ProjectFacts::default(),
    })
    .await;
    let cmd = Command::CreateTask {
        session: s,
        repo: h.repo_path(),
        title: "First".into(),
        instruction: "Do it".into(),
        tickets: vec![],
        blocked_by: vec![],
        level: regie_core::Level::Standard,
        paths: vec![],
        provider: None,
    };
    let r = app.clone().oneshot(post_command(&cmd)).await.unwrap();
    assert_eq!(r.status(), StatusCode::OK);
    let result: CommandResult = serde_json::from_value(json(r).await["result"].clone()).unwrap();
    assert_eq!(result, CommandResult::Task(TaskId(1)));

    let r = app
        .clone()
        .oneshot(
            get(&format!("/api/board?session={}", s.0))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(r.status(), StatusCode::OK);
    let board = json(r).await;
    assert!(
        board["cards"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["title"] == "First"),
        "{board}"
    );

    let r = app
        .clone()
        .oneshot(get("/api/sessions").body(Body::empty()).unwrap())
        .await
        .unwrap();
    let sessions = json(r).await;
    assert_eq!(sessions.as_array().unwrap().len(), 1);
    assert_eq!(sessions[0]["name"], "s");

    let r = app
        .clone()
        .oneshot(get("/api/tasks/1").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(r.status(), StatusCode::OK);
    let r = app
        .clone()
        .oneshot(get("/api/tasks/99").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(r.status(), StatusCode::NOT_FOUND);
    let r = app
        .oneshot(get("/api/board?session=99").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(r.status(), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn invalid_command_is_400_with_message() {
    let (h, app) = app().await;
    let cmd = Command::CreateSession {
        name: "s".into(),
        repos: vec![h.repo_path()],
    };
    let CommandResult::Session(s) = h.command(cmd).await else {
        panic!()
    };
    let cmd = Command::CreateTask {
        session: s,
        repo: h.repo_path(),
        title: "t".into(),
        instruction: "i".into(),
        tickets: vec![],
        blocked_by: vec![],
        level: regie_core::Level::Standard,
        paths: vec![],
        provider: None,
    };
    let r = app.oneshot(post_command(&cmd)).await.unwrap();
    assert_eq!(r.status(), StatusCode::BAD_REQUEST);
    let v = json(r).await;
    assert!(
        v["error"].as_str().unwrap().contains("no project facts"),
        "{v}"
    );
}

#[tokio::test]
async fn run_output_returns_the_last_lines() {
    let (h, app) = app().await;
    std::fs::write(h.data.run_file(regie_core::RunId(5)), "one\ntwo\nthree\n").unwrap();
    let r = app
        .clone()
        .oneshot(
            get("/api/runs/5/output?lines=2")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(r.status(), StatusCode::OK);
    assert_eq!(body(r).await.as_ref(), b"two\nthree\n");
    let r = app
        .oneshot(get("/api/runs/6/output").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(r.status(), StatusCode::NOT_FOUND);
}

/// Reads frames until `n` blank-line-terminated ones arrived, or fails after 5 seconds.
async fn frames(r: Response<Body>, n: usize) -> Vec<String> {
    use tokio_stream::StreamExt;
    let mut stream = r.into_body().into_data_stream();
    let mut text = String::new();
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    while text.matches("\n\n").count() < n {
        let next = tokio::time::timeout_at(deadline, stream.next())
            .await
            .expect("timed out waiting for frames");
        text.push_str(&String::from_utf8_lossy(
            &next.expect("stream ended").unwrap(),
        ));
    }
    text.split("\n\n")
        .filter(|f| !f.is_empty())
        .map(str::to_string)
        .collect()
}

#[tokio::test]
async fn sse_replays_after_id() {
    let (h, app) = app().await;
    let s = h.create_session(ProjectFacts::default()).await;
    h.create_task(s, "a", vec![]).await;
    let all = h.events();
    assert!(all.len() >= 3, "{} events", all.len());
    let r = app
        .oneshot(get("/api/events?after=1").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(r.status(), StatusCode::OK);
    assert!(r.headers()[header::CONTENT_TYPE]
        .to_str()
        .unwrap()
        .starts_with("text/event-stream"));
    let got = frames(r, all.len() - 1).await;
    for (frame, e) in got.iter().zip(&all[1..]) {
        assert!(frame.contains("event: event"), "{frame}");
        assert!(frame.contains(&format!("id: {}", e.id.0)), "{frame}");
        let data = frame
            .lines()
            .find_map(|l| l.strip_prefix("data: "))
            .unwrap();
        assert_eq!(serde_json::from_str::<regie_core::Event>(data).unwrap(), *e);
    }
    assert!(!got
        .iter()
        .any(|f| f.contains("id: 1\n") || f.ends_with("id: 1")));
}

#[tokio::test]
async fn sse_last_event_id_header_and_live_messages() {
    let (h, app) = app().await;
    let s = h.create_session(ProjectFacts::default()).await;
    let n = h.events().len() as i64;
    let req = get("/api/events")
        .header("Last-Event-ID", n.to_string())
        .body(Body::empty())
        .unwrap();
    let r = app.oneshot(req).await.unwrap();
    // Nothing stored after n; a live event and an output line follow.
    let engine = h.engine.clone();
    let publish = tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(100)).await;
        engine.bus.publish(regie::bus::BusMsg::Output {
            task: TaskId(1),
            run: regie_core::RunId(2),
            line: "hello".into(),
        });
        engine
            .command(
                Command::PauseSession { session: s },
                regie_core::Actor::Frederic,
            )
            .await
            .unwrap();
    });
    let got = frames(r, 2).await;
    publish.await.unwrap();
    assert!(got[0].contains("event: output"), "{got:?}");
    assert!(
        got[0].contains(r#"{"task":1,"run":2,"line":"hello"}"#),
        "{got:?}"
    );
    assert!(!got[0].contains("id:"));
    assert!(
        got[1].contains("event: event") && got[1].contains(&format!("id: {}", n + 1)),
        "{got:?}"
    );
}

#[tokio::test]
async fn serve_writes_server_json_with_mode_0600() {
    use std::os::unix::fs::PermissionsExt;
    let h = Harness::new(|_, _| says("ok")).await;
    let path = h.data.server_json();
    std::fs::write(&path, "old").unwrap();
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o644)).unwrap();
    tokio::spawn(regie::server::serve(h.engine.clone(), h.data.clone(), 0));
    let info: ServerInfo = loop {
        if let Ok(i) = serde_json::from_slice(&std::fs::read(&path).unwrap_or_default()) {
            break i;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    };
    assert_ne!(info.port, 0);
    assert_eq!(info.token.len(), 64);
    assert_eq!(info.pid, std::process::id());
    assert_eq!(
        std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
        0o600
    );
}

#[tokio::test]
async fn sse_uses_the_larger_of_after_and_last_event_id() {
    let (h, app) = app().await;
    let s = h.create_session(ProjectFacts::default()).await;
    h.create_task(s, "a", vec![]).await;
    let all = h.events();
    assert!(all.len() >= 4, "{} events", all.len());
    let req = get("/api/events?after=1")
        .header("Last-Event-ID", "3")
        .body(Body::empty())
        .unwrap();
    let got = frames(app.oneshot(req).await.unwrap(), all.len() - 3).await;
    assert_eq!(got.len(), all.len() - 3);
    for (frame, e) in got.iter().zip(&all[3..]) {
        assert!(frame.contains(&format!("id: {}", e.id.0)), "{frame}");
    }
}

#[tokio::test]
async fn sse_ends_when_the_store_cannot_be_read() {
    use tokio_stream::StreamExt;
    let (h, app) = app().await;
    h.create_session(ProjectFacts::default()).await;
    for ext in ["", "-wal", "-shm"] {
        let _ = std::fs::remove_file(format!("{}{ext}", h.data.db().display()));
    }
    let r = app
        .oneshot(get("/api/events?after=0").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(r.status(), StatusCode::OK);
    let mut stream = r.into_body().into_data_stream();
    let next = tokio::time::timeout(Duration::from_secs(5), stream.next())
        .await
        .expect("stream should end, not hang");
    assert!(
        next.is_none(),
        "a failed replay must not look like an empty one: {next:?}"
    );
}

#[tokio::test]
async fn task_read_error_is_an_error_not_an_empty_timeline() {
    let (h, app) = app().await;
    let t = h.create_session_and_task("Readable").await;
    // the engine has written the task's events: the timeline is not empty
    let r = app
        .clone()
        .oneshot(
            get(&format!("/api/tasks/{}", t.0))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(r.status(), StatusCode::OK);
    assert!(!json(r).await["timeline"].as_array().unwrap().is_empty());
    for ext in ["", "-wal", "-shm"] {
        let _ = std::fs::remove_file(format!("{}{ext}", h.data.db().display()));
    }
    let r = app
        .oneshot(
            get(&format!("/api/tasks/{}", t.0))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(r.status(), StatusCode::INTERNAL_SERVER_ERROR);
    assert!(json(r).await["error"]
        .as_str()
        .unwrap()
        .contains("timeline"));
    // no stray empty database was made in its place
    assert!(!h.data.db().exists());
}
