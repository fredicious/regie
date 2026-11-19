//! Typed ids and timestamps. All export to TypeScript as `number`. Newtype structs
//! already serialize transparently in serde (ts-rs warns on an explicit attribute).

use serde::{Deserialize, Serialize};
use ts_rs::TS;

use std::fmt;

macro_rules! id_type {
    ($name:ident) => {
        #[derive(
            Serialize, Deserialize, TS, Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash,
        )]
        #[ts(export)]
        pub struct $name(#[ts(type = "number")] pub i64);
    };
}

id_type!(SessionId);
id_type!(TaskId);
id_type!(RunId);
id_type!(EventId);
id_type!(Millis);

impl fmt::Display for TaskId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "T{}", self.0)
    }
}

impl fmt::Display for RunId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "R{}", self.0)
    }
}

impl fmt::Display for SessionId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "S{}", self.0)
    }
}
