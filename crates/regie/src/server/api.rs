//! The JSON routes.

use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Json, Response};
use regie_core::{Actor, Command, RunId, SessionId, TaskId};
use serde::Deserialize;
use serde_json::json;

use super::AppState;

pub async fn health() -> Json<serde_json::Value> {
    Json(json!({"ok": true, "version": crate::VERSION}))
}

pub async fn command(State(s): State<AppState>, Json(cmd): Json<Command>) -> Response {
    match s.engine.command(cmd, Actor::Frederic).await {
        Ok(result) => Json(json!({"result": result})).into_response(),
        Err(error) => (StatusCode::BAD_REQUEST, Json(json!({"error": error}))).into_response(),
    }
}

pub async fn sessions(State(s): State<AppState>) -> Response {
    let snap = s.engine.snapshot.read().unwrap();
    Json(snap.state.sessions.values().cloned().collect::<Vec<_>>()).into_response()
}

#[derive(Deserialize)]
pub struct BoardQuery {
    session: i64,
}

fn not_found(what: &str) -> Response {
    (StatusCode::NOT_FOUND, Json(json!({"error": format!("{what} not found")}))).into_response()
}

pub async fn board(State(s): State<AppState>, Query(q): Query<BoardQuery>) -> Response {
    match s.engine.board(SessionId(q.session)) {
        Some(b) => Json(b).into_response(),
        None => not_found(&format!("session {}", q.session)),
    }
}

pub async fn task(State(s): State<AppState>, Path(id): Path<i64>) -> Response {
    match s.engine.task(TaskId(id)) {
        Some(t) => Json(t).into_response(),
        None => not_found(&format!("task {id}")),
    }
}

#[derive(Deserialize)]
pub struct OutputQuery {
    lines: Option<usize>,
}

/// The last `lines` (default 200) lines of the run's file.
pub async fn run_output(State(s): State<AppState>, Path(id): Path<i64>, Query(q): Query<OutputQuery>) -> Response {
    let Ok(text) = tokio::fs::read_to_string(s.data.run_file(RunId(id))).await else {
        return not_found(&format!("output of run {id}"));
    };
    let n = q.lines.unwrap_or(200);
    let all: Vec<&str> = text.lines().collect();
    let mut out = all[all.len().saturating_sub(n)..].join("\n");
    if !out.is_empty() {
        out.push('\n');
    }
    ([(axum::http::header::CONTENT_TYPE, "text/plain; charset=utf-8")], out).into_response()
}
