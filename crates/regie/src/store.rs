//! The append-only event store (SQLite, WAL). The engine owns the only writer;
//! state is rebuilt by replaying `load_all` into `regie_core::State`.

use std::path::Path;

use anyhow::{anyhow, Context};
use regie_core::{Actor, Event, EventId, EventKind, Millis, NewEvent, SessionId, TaskId};
use rusqlite::{params, Connection, Row};

const SCHEMA: &str = "CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at INTEGER NOT NULL,
    session INTEGER,
    task INTEGER,
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL
)";

const COLUMNS: &str = "id, at, session, task, actor, payload";

pub struct Store {
    conn: Connection,
}

impl Store {
    /// Opens (creating if needed) the database at `path` in WAL mode.
    pub fn open(path: &Path) -> anyhow::Result<Store> {
        let conn = Connection::open(path).with_context(|| format!("opening {}", path.display()))?;
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.pragma_update(None, "synchronous", "NORMAL")?;
        conn.execute_batch(SCHEMA)?;
        Ok(Store { conn })
    }

    /// Opens an existing database for reading only (the server's second connection);
    /// waits up to 5 seconds when the engine is writing.
    pub fn open_reader(path: &Path) -> anyhow::Result<Store> {
        let flags = rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX;
        let conn = Connection::open_with_flags(path, flags).with_context(|| format!("opening {}", path.display()))?;
        conn.busy_timeout(std::time::Duration::from_secs(5))?;
        Ok(Store { conn })
    }

    /// Appends an event and returns it with its assigned id.
    pub fn append(&mut self, e: NewEvent) -> anyhow::Result<Event> {
        let payload = serde_json::to_value(&e.kind)?;
        let kind = payload
            .get("type")
            .and_then(|t| t.as_str())
            .ok_or_else(|| anyhow!("event kind has no type tag"))?
            .to_owned();
        self.conn.execute(
            "INSERT INTO events (at, session, task, actor, kind, payload) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![
                e.at.0,
                e.session.map(|s| s.0),
                e.task.map(|t| t.0),
                serde_json::to_string(&e.actor)?,
                kind,
                payload.to_string()
            ],
        )?;
        Ok(Event {
            id: EventId(self.conn.last_insert_rowid()),
            at: e.at,
            session: e.session,
            task: e.task,
            actor: e.actor,
            kind: e.kind,
        })
    }

    pub fn load_all(&self) -> anyhow::Result<Vec<Event>> {
        self.load_after(EventId(0))
    }

    /// Events with an id greater than `after`, in order.
    pub fn load_after(&self, after: EventId) -> anyhow::Result<Vec<Event>> {
        let mut stmt = self.conn.prepare(&format!("SELECT {COLUMNS} FROM events WHERE id > ?1 ORDER BY id"))?;
        let rows = stmt.query_map([after.0], read_row)?;
        rows.map(|r| r?).collect()
    }

    /// The task's events, in order.
    pub fn load_task(&self, task: TaskId) -> anyhow::Result<Vec<Event>> {
        let mut stmt = self.conn.prepare(&format!("SELECT {COLUMNS} FROM events WHERE task = ?1 ORDER BY id"))?;
        let rows = stmt.query_map([task.0], read_row)?;
        rows.map(|r| r?).collect()
    }
}

fn read_row(row: &Row) -> rusqlite::Result<anyhow::Result<Event>> {
    let id: i64 = row.get(0)?;
    let at: i64 = row.get(1)?;
    let session: Option<i64> = row.get(2)?;
    let task: Option<i64> = row.get(3)?;
    let actor: String = row.get(4)?;
    let payload: String = row.get(5)?;
    Ok((|| {
        let actor: Actor = serde_json::from_str(&actor).with_context(|| format!("event {id}: actor"))?;
        let kind: EventKind = serde_json::from_str(&payload).with_context(|| format!("event {id}: payload"))?;
        Ok(Event { id: EventId(id), at: Millis(at), session: session.map(SessionId), task: task.map(TaskId), actor, kind })
    })())
}

#[cfg(test)]
mod tests {
    use regie_core::*;

    use super::*;

    fn ev(kind: EventKind, session: Option<SessionId>, task: Option<TaskId>, at: i64) -> NewEvent {
        NewEvent { at: Millis(at), session, task, actor: Actor::Regie, kind }
    }

    fn task_created() -> EventKind {
        EventKind::TaskCreated {
            repo: "r".into(),
            title: "t".into(),
            instruction: "do it".into(),
            tickets: vec![],
            blocked_by: vec![],
            level: Level::Standard,
            paths: vec![],
            provider: None,
        }
    }

    #[test]
    fn append_assigns_increasing_ids() {
        let dir = tempfile::tempdir().unwrap();
        let mut store = Store::open(&dir.path().join("regie.db")).unwrap();
        let a = store.append(ev(EventKind::AllPaused, None, None, 1)).unwrap();
        let b = store.append(ev(EventKind::AllResumed, None, None, 2)).unwrap();
        assert_eq!(a.id, EventId(1));
        assert_eq!(b.id, EventId(2));
        assert_eq!(b.kind, EventKind::AllResumed);
        assert_eq!(b.at, Millis(2));
    }

    #[test]
    fn reopen_and_replay_gives_same_state() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("regie.db");
        let s = Some(SessionId(1));
        let mut incremental = State::default();
        {
            let mut store = Store::open(&path).unwrap();
            let kinds = [
                (EventKind::SessionCreated { name: "s".into(), repos: vec![] }, None),
                (task_created(), Some(TaskId(1))),
                (EventKind::SessionPaused, None),
            ];
            for (i, (k, t)) in kinds.into_iter().enumerate() {
                let e = store.append(ev(k, s, t, i as i64)).unwrap();
                incremental.apply(&e);
            }
        }
        let store = Store::open(&path).unwrap();
        let events = store.load_all().unwrap();
        assert_eq!(events.len(), 3);
        assert_eq!(events[1].task, Some(TaskId(1)));
        assert_eq!(State::replay(&events), incremental);
    }

    #[test]
    fn load_after_returns_only_newer() {
        let dir = tempfile::tempdir().unwrap();
        let mut store = Store::open(&dir.path().join("regie.db")).unwrap();
        for i in 0..4 {
            store.append(ev(EventKind::AllPaused, None, None, i)).unwrap();
        }
        let newer = store.load_after(EventId(2)).unwrap();
        assert_eq!(newer.iter().map(|e| e.id).collect::<Vec<_>>(), vec![EventId(3), EventId(4)]);
        assert!(store.load_after(EventId(4)).unwrap().is_empty());
    }

    #[test]
    fn load_task_returns_only_that_task() {
        let dir = tempfile::tempdir().unwrap();
        let mut store = Store::open(&dir.path().join("regie.db")).unwrap();
        for (i, t) in [Some(1), None, Some(2), Some(1)].into_iter().enumerate() {
            store.append(ev(EventKind::PrMissing, None, t.map(TaskId), i as i64)).unwrap();
        }
        let ids: Vec<EventId> = store.load_task(TaskId(1)).unwrap().iter().map(|e| e.id).collect();
        assert_eq!(ids, vec![EventId(1), EventId(4)]);
    }

    #[test]
    fn kind_column_holds_the_variant_name() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("regie.db");
        let mut store = Store::open(&path).unwrap();
        store.append(ev(EventKind::PrMissing, None, None, 1)).unwrap();
        let conn = rusqlite::Connection::open(&path).unwrap();
        let kind: String = conn.query_row("SELECT kind FROM events", [], |r| r.get(0)).unwrap();
        assert_eq!(kind, "PrMissing");
    }
}
