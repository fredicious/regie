//! Per-repo project facts the lead sets once.

use serde::{Deserialize, Serialize};
use ts_rs::TS;

#[derive(Serialize, Deserialize, TS, Clone, Debug, Default, PartialEq)]
#[ts(export)]
pub struct ProjectFacts {
    pub setup: Option<String>,
    pub build: Option<String>,
    pub test: Option<String>,
    pub lint: Option<String>,
    pub copy_files: Vec<String>,
    pub definition_of_done: String,
}
