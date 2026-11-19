//! Review, verify and ruling types (parsing lives in later tasks).

use serde::{Deserialize, Serialize};
use ts_rs::TS;

#[derive(Serialize, Deserialize, TS, Clone, Copy, Debug, PartialEq, Eq)]
#[ts(export)]
pub enum FindingKind {
    Bug,
    Security,
    MissingTest,
    UnmetCriterion,
    Design,
    Other,
}

#[derive(Serialize, Deserialize, TS, Clone, Copy, Debug, PartialEq, Eq)]
#[ts(export)]
pub enum EvidenceType {
    Command,
    Test,
    Criterion,
}

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub struct Evidence {
    pub r#type: EvidenceType,
    pub detail: String,
    pub file: Option<String>,
    pub line: Option<u32>,
}

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub struct Finding {
    pub id: u32,
    pub title: String,
    pub kind: FindingKind,
    pub blocking: bool,
    pub evidence: Option<Evidence>,
}

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub enum ReviewOutcome {
    Findings(Vec<Finding>),
    Unparsed { raw: String },
}

#[derive(Serialize, Deserialize, TS, Clone, Copy, Debug, PartialEq, Eq)]
#[ts(export)]
pub enum VerifyStatus {
    Fixed,
    NotFixed,
    Disputed,
}

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub struct VerifyResult {
    pub finding: u32,
    pub status: VerifyStatus,
    pub note: String,
}

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub enum VerifyOutcome {
    Results(Vec<VerifyResult>),
    Unparsed { raw: String },
}

#[derive(Serialize, Deserialize, TS, Clone, Copy, Debug, PartialEq, Eq)]
#[ts(export)]
pub enum BuilderStatus {
    Done,
    Question,
}

#[derive(Serialize, Deserialize, TS, Clone, Copy, Debug, PartialEq, Eq)]
#[ts(export)]
pub enum Decision {
    Fix,
    OutOfScope,
    Advisory,
}

#[derive(Serialize, Deserialize, TS, Clone, Debug, PartialEq)]
#[ts(export)]
pub struct Ruling {
    pub finding: u32,
    pub decision: Decision,
    pub note: String,
}
