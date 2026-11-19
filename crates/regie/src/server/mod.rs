//! The local HTTP server: one JSON API and one event stream, for the web page and
//! the CLI. It binds 127.0.0.1 only and every request passes `auth::guard`.

mod api;
mod auth;
mod sse;
mod web;

use std::fs::OpenOptions;
use std::io::Write;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::sync::Arc;

use anyhow::Context;
use axum::routing::{get, post};
use axum::{middleware, Router};
use serde::{Deserialize, Serialize};

use crate::engine::EngineHandle;
use crate::paths::DataDir;

pub const DEFAULT_PORT: u16 = 7317;

/// What the CLI reads from `server.json` to find and talk to the server.
#[derive(Clone, Serialize, Deserialize, PartialEq)]
pub struct ServerInfo {
    pub port: u16,
    pub token: String,
    pub pid: u32,
}

impl std::fmt::Debug for ServerInfo {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ServerInfo").field("port", &self.port).field("token", &"<redacted>").field("pid", &self.pid).finish()
    }
}

#[derive(Clone)]
pub(crate) struct AppState {
    pub engine: EngineHandle,
    pub data: DataDir,
}

pub fn router(engine: EngineHandle, data: DataDir, info: ServerInfo) -> Router {
    let guard = Arc::new(auth::Guard { port: info.port, token: info.token });
    Router::new()
        .route("/api/health", get(api::health))
        .route("/api/commands", post(api::command))
        .route("/api/sessions", get(api::sessions))
        .route("/api/board", get(api::board))
        .route("/api/tasks/{id}", get(api::task))
        .route("/api/runs/{id}/output", get(api::run_output))
        .route("/api/events", get(sse::events))
        .fallback(get(web::asset))
        .layer(middleware::from_fn_with_state(guard, auth::guard))
        .with_state(AppState { engine, data })
}

/// Binds 127.0.0.1:`port` (0 picks a free one), writes `server.json` and serves forever.
pub async fn serve(engine: EngineHandle, data: DataDir, port: u16) -> anyhow::Result<()> {
    let listener = tokio::net::TcpListener::bind(("127.0.0.1", port))
        .await
        .with_context(|| format!("binding 127.0.0.1:{port} (is Régie already running?)"))?;
    let port = listener.local_addr()?.port();
    let info = ServerInfo { port, token: auth::new_token(), pid: std::process::id() };
    write_server_json(&data, &info)?;
    axum::serve(listener, router(engine, data, info)).await?;
    Ok(())
}

fn write_server_json(data: &DataDir, info: &ServerInfo) -> anyhow::Result<()> {
    let path = data.server_json();
    let mut file = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(&path)
        .with_context(|| format!("writing {}", path.display()))?;
    // `mode` only applies when the file is created; an older file may be looser.
    file.set_permissions(std::fs::Permissions::from_mode(0o600))?;
    file.write_all(serde_json::to_string(info)?.as_bytes())?;
    Ok(())
}
