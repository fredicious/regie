//! Pull request state as Régie observes it.

use serde::{Deserialize, Serialize};
use ts_rs::TS;

use crate::ids::Millis;

#[derive(Serialize, Deserialize, TS, Clone, Copy, Debug, PartialEq, Eq)]
#[ts(export)]
pub enum PrLifecycle {
    Open,
    Merged,
    Closed,
}

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub enum Checks {
    None,
    Pending,
    Success,
    Failure { failed: Vec<String> },
}

#[derive(Serialize, Deserialize, TS, Clone, Copy, Debug, PartialEq, Eq)]
#[ts(export)]
pub enum MergeState {
    Clean,
    Blocked,
    Behind,
    Unstable,
    Dirty,
    Unknown,
}

#[derive(Serialize, Deserialize, TS, Clone, Copy, Debug, PartialEq, Eq)]
#[ts(export)]
pub enum ReviewDecision {
    Approved,
    ChangesRequested,
    ReviewRequired,
}

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub struct PrState {
    #[ts(type = "number")]
    pub number: u64,
    pub url: String,
    pub lifecycle: PrLifecycle,
    pub checks: Checks,
    pub conflicting: bool,
    pub merge_state: MergeState,
    pub review_decision: Option<ReviewDecision>,
    pub head_sha: String,
}

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub struct PrComment {
    pub id: String,
    pub author: String,
    pub body: String,
    pub changes_requested: bool,
    pub at: Millis,
}
