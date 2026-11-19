//! `GET /api/events`: every stored event after an id, then what happens live.

use std::convert::Infallible;
use std::time::Duration;

use axum::extract::{Query, State};
use axum::http::HeaderMap;
use axum::response::sse::{Event as Frame, KeepAlive, KeepAliveStream, Sse};
use regie_core::{Event, EventId, RunId, TaskId};
use serde::Deserialize;
use serde::Serialize;
use tokio::sync::{broadcast, mpsc};
use tokio_stream::wrappers::ReceiverStream;

use super::AppState;
use crate::bus::BusMsg;
use crate::store::Store;

#[derive(Serialize)]
struct OutputLine {
    task: TaskId,
    run: RunId,
    line: String,
}

#[derive(Deserialize)]
pub struct EventsQuery {
    after: Option<i64>,
}

pub async fn events(
    State(s): State<AppState>,
    Query(q): Query<EventsQuery>,
    headers: HeaderMap,
) -> Sse<KeepAliveStream<ReceiverStream<Result<Frame, Infallible>>>> {
    let after = q
        .after
        .or_else(|| headers.get("last-event-id").and_then(|v| v.to_str().ok()).and_then(|v| v.parse().ok()))
        .unwrap_or(0);
    // Subscribe before reading the store, so nothing falls between the two.
    let mut live = s.engine.bus.subscribe();
    let db = s.data.db();
    let (tx, rx) = mpsc::channel(64);
    tokio::spawn(async move {
        let mut last = EventId(after);
        if send_stored(&db, &mut last, &tx).await.is_none() {
            return;
        }
        loop {
            match live.recv().await {
                Ok(BusMsg::Event(e)) => {
                    if e.id > last {
                        last = e.id;
                        if tx.send(Ok(event_frame(&e))).await.is_err() {
                            return;
                        }
                    }
                }
                Ok(BusMsg::Output { task, run, line }) => {
                    let data = serde_json::to_string(&OutputLine { task, run, line }).unwrap_or_default();
                    if tx.send(Ok(Frame::default().event("output").data(data))).await.is_err() {
                        return;
                    }
                }
                // Fell behind: the store has what was missed.
                Err(broadcast::error::RecvError::Lagged(_)) => {
                    if send_stored(&db, &mut last, &tx).await.is_none() {
                        return;
                    }
                }
                Err(broadcast::error::RecvError::Closed) => return,
            }
        }
    });
    Sse::new(ReceiverStream::new(rx)).keep_alive(KeepAlive::new().interval(Duration::from_secs(15)))
}

fn event_frame(e: &Event) -> Frame {
    Frame::default().event("event").id(e.id.0.to_string()).data(serde_json::to_string(e).unwrap_or_default())
}

/// Sends the stored events after `last`, moving `last` forward; `None` when the watcher left.
async fn send_stored(db: &std::path::Path, last: &mut EventId, tx: &mpsc::Sender<Result<Frame, Infallible>>) -> Option<()> {
    let stored = Store::open_reader(db).and_then(|s| s.load_after(*last)).unwrap_or_default();
    for e in stored {
        *last = e.id;
        tx.send(Ok(event_frame(&e))).await.ok()?;
    }
    Some(())
}
