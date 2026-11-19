//! Every request must come from this machine's own page or CLI: the right Host (against
//! DNS rebinding), the right token, and no foreign Origin on anything that changes state.

use std::sync::Arc;

use axum::extract::{Request, State};
use axum::http::{header, HeaderMap, Method, StatusCode};
use axum::middleware::Next;
use axum::response::{IntoResponse, Redirect, Response};
use rand::RngCore;

pub const COOKIE: &str = "regie_token";

pub struct Guard {
    pub port: u16,
    pub token: String,
}

impl Guard {
    fn allowed(&self, value: &str, scheme: &str) -> bool {
        value == format!("{scheme}127.0.0.1:{}", self.port) || value == format!("{scheme}localhost:{}", self.port)
    }

    fn token_ok(&self, given: &str) -> bool {
        constant_time_eq(given.as_bytes(), self.token.as_bytes())
    }
}

pub fn new_token() -> String {
    let mut bytes = [0u8; 32];
    rand::rng().fill_bytes(&mut bytes);
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// Compares without stopping at the first difference.
fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

fn header_str(headers: &HeaderMap, name: header::HeaderName) -> Option<&str> {
    headers.get(name).and_then(|v| v.to_str().ok())
}

fn presented_token(headers: &HeaderMap) -> Option<String> {
    if let Some(t) = header_str(headers, header::AUTHORIZATION).and_then(|v| v.strip_prefix("Bearer ")) {
        return Some(t.trim().to_string());
    }
    header_str(headers, header::COOKIE)?
        .split(';')
        .find_map(|c| c.trim().strip_prefix(&format!("{COOKIE}=")).map(str::to_string))
}

fn query_token(req: &Request) -> Option<String> {
    req.uri().query()?.split('&').find_map(|kv| kv.strip_prefix("token=").map(str::to_string))
}

pub async fn guard(State(g): State<Arc<Guard>>, req: Request, next: Next) -> Response {
    let headers = req.headers();
    if !header_str(headers, header::HOST).is_some_and(|h| g.allowed(h, "")) {
        return (StatusCode::FORBIDDEN, "bad Host").into_response();
    }
    if req.method() == Method::GET && req.uri().path() == "/" {
        if let Some(t) = query_token(&req) {
            if g.token_ok(&t) {
                let cookie = format!("{COOKIE}={t}; HttpOnly; SameSite=Strict; Path=/");
                return ([(header::SET_COOKIE, cookie)], Redirect::to("/")).into_response();
            }
        }
    }
    if !presented_token(headers).is_some_and(|t| g.token_ok(&t)) {
        return (StatusCode::UNAUTHORIZED, "missing or wrong token").into_response();
    }
    if req.method() != Method::GET && req.method() != Method::HEAD {
        if let Some(origin) = headers.get(header::ORIGIN) {
            if !origin.to_str().is_ok_and(|o| g.allowed(o, "http://")) {
                return (StatusCode::FORBIDDEN, "bad Origin").into_response();
            }
        }
    }
    next.run(req).await
}
