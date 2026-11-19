//! The web page, embedded in the binary from `web/dist` (built by `make web`).

use axum::http::{header, HeaderValue, StatusCode, Uri};
use axum::response::{IntoResponse, Response};
use rust_embed::RustEmbed;

#[derive(RustEmbed)]
#[folder = "$CARGO_MANIFEST_DIR/../../web/dist"]
#[allow_missing = true]
struct Assets;

const MISSING: &str = "Régie's web page is not built into this binary. Run `make web`, then rebuild.\n";

pub async fn asset(uri: Uri) -> Response {
    let path = uri.path().trim_start_matches('/');
    let path = if path.is_empty() { "index.html" } else { path };
    match Assets::get(path) {
        Some(file) => with_security_headers(
            ([(header::CONTENT_TYPE, content_type(path))], file.data.into_owned()).into_response(),
        ),
        None if path == "index.html" => with_security_headers(
            ([(header::CONTENT_TYPE, "text/plain; charset=utf-8")], MISSING).into_response(),
        ),
        None => StatusCode::NOT_FOUND.into_response(),
    }
}

/// The page must not be framed, and must not leak its URL (which can carry the token) as a referrer.
fn with_security_headers(mut r: Response) -> Response {
    let h = r.headers_mut();
    h.insert(header::X_FRAME_OPTIONS, HeaderValue::from_static("DENY"));
    h.insert(header::CONTENT_SECURITY_POLICY, HeaderValue::from_static("frame-ancestors 'none'"));
    h.insert(header::REFERRER_POLICY, HeaderValue::from_static("no-referrer"));
    r
}

fn content_type(path: &str) -> &'static str {
    match path.rsplit('.').next().unwrap_or("") {
        "html" => "text/html; charset=utf-8",
        "js" | "mjs" => "text/javascript; charset=utf-8",
        "css" => "text/css; charset=utf-8",
        "json" => "application/json",
        "svg" => "image/svg+xml",
        "png" => "image/png",
        "ico" => "image/x-icon",
        "woff2" => "font/woff2",
        _ => "application/octet-stream",
    }
}
