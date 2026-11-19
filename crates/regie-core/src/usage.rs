//! Provider usage readings (functions arrive with the usage task).

use serde::{Deserialize, Serialize};
use ts_rs::TS;

use crate::ids::Millis;

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub struct Window {
    pub used: f64,
    pub resets_at: Millis,
}

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub struct Reading {
    pub five_hour: Window,
    pub seven_day: Window,
    pub read_at: Millis,
}
