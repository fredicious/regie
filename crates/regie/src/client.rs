//! The CLI's side of the local HTTP API.

use anyhow::Context;
use regie_core::{BoardView, Command, CommandResult, Session, SessionId};

use crate::paths::DataDir;
use crate::server::ServerInfo;

pub struct Client {
    pub base: String,
    pub token: String,
}

impl Client {
    /// Reads `server.json`; fails when no server has written one.
    pub fn from_data_dir(data: &DataDir) -> anyhow::Result<Client> {
        let path = data.server_json();
        let text = std::fs::read_to_string(&path).with_context(|| {
            format!(
                "Régie is not running (no {}); start it with `regie`",
                path.display()
            )
        })?;
        let info: ServerInfo =
            serde_json::from_str(&text).with_context(|| format!("reading {}", path.display()))?;
        Ok(Client {
            base: format!("http://127.0.0.1:{}", info.port),
            token: info.token,
        })
    }

    fn http() -> reqwest::Client {
        reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(30))
            .build()
            .expect("http client")
    }

    pub async fn health(&self) -> bool {
        let request = Self::http()
            .get(format!("{}/api/health", self.base))
            .bearer_auth(&self.token);
        matches!(request.send().await, Ok(r) if r.status().is_success())
    }

    /// Runs a command; the server's error message comes back verbatim.
    pub async fn command(&self, cmd: &Command) -> anyhow::Result<CommandResult> {
        let response = Self::http()
            .post(format!("{}/api/commands", self.base))
            .bearer_auth(&self.token)
            .json(cmd)
            .send()
            .await
            .context("Régie did not answer (is it running?)")?;
        let body = ok_body(response).await?;
        let result = body
            .get("result")
            .cloned()
            .context("the server sent no result")?;
        serde_json::from_value(result).context("the server sent an unreadable result")
    }

    pub async fn board(&self, s: SessionId) -> anyhow::Result<BoardView> {
        self.get(&format!("/api/board?session={}", s.0)).await
    }

    pub async fn sessions(&self) -> anyhow::Result<Vec<Session>> {
        self.get("/api/sessions").await
    }

    async fn get<T: serde::de::DeserializeOwned>(&self, path: &str) -> anyhow::Result<T> {
        let response = Self::http()
            .get(format!("{}{path}", self.base))
            .bearer_auth(&self.token)
            .send()
            .await
            .context("Régie did not answer (is it running?)")?;
        let body = ok_body(response).await?;
        serde_json::from_value(body).context("the server sent an unreadable answer")
    }
}

/// The JSON body of a successful response; on failure, the server's `error` text.
async fn ok_body(response: reqwest::Response) -> anyhow::Result<serde_json::Value> {
    let status = response.status();
    let body: serde_json::Value = response.json().await.unwrap_or_default();
    if status.is_success() {
        return Ok(body);
    }
    match body.get("error").and_then(|e| e.as_str()) {
        Some(message) => anyhow::bail!("{message}"),
        None => anyhow::bail!("the server answered {status}"),
    }
}
