//! What the engine tells its watchers: every event, and agents' output lines.

use regie_core::{Event, RunId, TaskId};
use tokio::sync::broadcast;

#[derive(Clone, Debug, PartialEq)]
pub enum BusMsg {
    Event(Event),
    /// A line for people to read; not stored (the run file has the raw stream).
    Output {
        task: TaskId,
        run: RunId,
        line: String,
    },
}

/// A broadcast channel; a watcher that falls more than 1024 messages behind misses some.
#[derive(Clone, Debug)]
pub struct Bus {
    tx: broadcast::Sender<BusMsg>,
}

impl Default for Bus {
    fn default() -> Self {
        Bus {
            tx: broadcast::channel(1024).0,
        }
    }
}

impl Bus {
    pub fn subscribe(&self) -> broadcast::Receiver<BusMsg> {
        self.tx.subscribe()
    }

    /// Sends to every current watcher; nobody watching is fine.
    pub fn publish(&self, msg: BusMsg) {
        let _ = self.tx.send(msg);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn watchers_get_what_is_published_after_they_subscribe() {
        let bus = Bus::default();
        bus.publish(BusMsg::Output {
            task: TaskId(1),
            run: RunId(1),
            line: "lost".into(),
        });
        let mut rx = bus.subscribe();
        let msg = BusMsg::Output {
            task: TaskId(1),
            run: RunId(2),
            line: "hi".into(),
        };
        bus.publish(msg.clone());
        assert_eq!(rx.recv().await.unwrap(), msg);
    }
}
