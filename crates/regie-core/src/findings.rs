//! Review, verify and ruling types, and parsers for agents' final messages.

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

// ---- Parsing ----

/// A builder is done only if its last non-empty line is `Status: done`.
pub fn parse_builder_status(final_message: &str) -> BuilderStatus {
    let last = final_message.lines().map(str::trim).rfind(|l| !l.is_empty());
    match last.map(unwrap_emphasis) {
        Some(l) if l.eq_ignore_ascii_case("Status: done") => BuilderStatus::Done,
        _ => BuilderStatus::Question,
    }
}

/// Strips one layer of markdown emphasis or inline code around a whole line.
fn unwrap_emphasis(line: &str) -> &str {
    for w in ["**", "__", "*", "_", "`"] {
        if line.len() > 2 * w.len() && line.starts_with(w) && line.ends_with(w) {
            return line[w.len()..line.len() - w.len()].trim();
        }
    }
    line
}

/// Body of the last ```json fenced block, only if that block is closed; a
/// truncated final block yields `None` rather than an earlier block.
fn last_json_block(msg: &str) -> Option<String> {
    let mut best = None;
    let mut open: Option<Vec<&str>> = None;
    for line in msg.lines() {
        let t = line.trim();
        match open.as_mut() {
            Some(body) => {
                if t == "```" {
                    best = Some(body.join("\n"));
                    open = None;
                } else {
                    body.push(line);
                }
            }
            None => {
                if t.strip_prefix("```json").is_some_and(|rest| rest.trim().is_empty()) {
                    open = Some(Vec::new());
                }
            }
        }
    }
    if open.is_some() { None } else { best }
}

#[derive(Deserialize)]
struct RawReview {
    findings: Vec<RawFinding>,
}

#[derive(Deserialize)]
struct RawFinding {
    title: String,
    kind: RawKind,
    blocking: bool,
    #[serde(default)]
    evidence: Option<RawEvidence>,
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum RawKind {
    Bug,
    Security,
    MissingTest,
    UnmetCriterion,
    Design,
    Other,
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum RawEvidenceType {
    Command,
    Test,
    Criterion,
}

#[derive(Deserialize)]
struct RawEvidence {
    r#type: RawEvidenceType,
    #[serde(default)]
    detail: String,
    #[serde(default)]
    file: Option<String>,
    #[serde(default)]
    line: Option<u32>,
}

impl From<RawKind> for FindingKind {
    fn from(k: RawKind) -> Self {
        match k {
            RawKind::Bug => Self::Bug,
            RawKind::Security => Self::Security,
            RawKind::MissingTest => Self::MissingTest,
            RawKind::UnmetCriterion => Self::UnmetCriterion,
            RawKind::Design => Self::Design,
            RawKind::Other => Self::Other,
        }
    }
}

impl From<RawEvidenceType> for EvidenceType {
    fn from(t: RawEvidenceType) -> Self {
        match t {
            RawEvidenceType::Command => Self::Command,
            RawEvidenceType::Test => Self::Test,
            RawEvidenceType::Criterion => Self::Criterion,
        }
    }
}

/// Whether the evidence is concrete enough to justify blocking.
fn evidence_supports_blocking(e: &Evidence) -> bool {
    if e.detail.trim().is_empty() {
        return false;
    }
    if e.r#type == EvidenceType::Criterion {
        let has_file = e.file.as_deref().is_some_and(|f| !f.trim().is_empty());
        return has_file && e.line.is_some();
    }
    true
}

pub fn parse_review(final_message: &str) -> ReviewOutcome {
    let unparsed = || ReviewOutcome::Unparsed { raw: final_message.to_string() };
    let Some(block) = last_json_block(final_message) else { return unparsed() };
    let Ok(raw) = serde_json::from_str::<RawReview>(&block) else { return unparsed() };
    let findings = raw
        .findings
        .into_iter()
        .enumerate()
        .map(|(i, f)| {
            let evidence = f.evidence.map(|e| Evidence {
                r#type: e.r#type.into(),
                detail: e.detail,
                file: e.file,
                line: e.line,
            });
            let blocking = f.blocking && evidence.as_ref().is_some_and(evidence_supports_blocking);
            Finding {
                id: i as u32 + 1,
                title: f.title,
                kind: f.kind.into(),
                blocking,
                evidence,
            }
        })
        .collect();
    ReviewOutcome::Findings(findings)
}

#[derive(Deserialize)]
struct RawVerify {
    results: Vec<RawVerifyResult>,
}

#[derive(Deserialize)]
struct RawVerifyResult {
    finding: u32,
    status: RawVerifyStatus,
    #[serde(default)]
    note: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum RawVerifyStatus {
    Fixed,
    NotFixed,
    Disputed,
}

pub fn parse_verify(final_message: &str) -> VerifyOutcome {
    let unparsed = || VerifyOutcome::Unparsed { raw: final_message.to_string() };
    let Some(block) = last_json_block(final_message) else { return unparsed() };
    let Ok(raw) = serde_json::from_str::<RawVerify>(&block) else { return unparsed() };
    VerifyOutcome::Results(
        raw.results
            .into_iter()
            .map(|r| VerifyResult {
                finding: r.finding,
                status: match r.status {
                    RawVerifyStatus::Fixed => VerifyStatus::Fixed,
                    RawVerifyStatus::NotFixed => VerifyStatus::NotFixed,
                    RawVerifyStatus::Disputed => VerifyStatus::Disputed,
                },
                note: r.note,
            })
            .collect(),
    )
}

/// The rule used while there is no lead: blocking findings get fixed, the rest are advisory.
pub fn auto_rule(findings: &[Finding]) -> Vec<Ruling> {
    findings
        .iter()
        .map(|f| Ruling {
            finding: f.id,
            decision: if f.blocking { Decision::Fix } else { Decision::Advisory },
            note: String::new(),
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn block(json: &str) -> String {
        format!("Review done.\n```json\n{json}\n```\n")
    }

    fn findings(o: ReviewOutcome) -> Vec<Finding> {
        match o {
            ReviewOutcome::Findings(f) => f,
            other => panic!("expected findings, got {other:?}"),
        }
    }

    #[test]
    fn done_status() {
        assert_eq!(parse_builder_status("Did X.\n\nStatus: done\n"), BuilderStatus::Done);
        assert_eq!(parse_builder_status("x\n  STATUS: Done  \n\n"), BuilderStatus::Done);
    }

    #[test]
    fn status_line_must_be_last() {
        assert_eq!(parse_builder_status("Status: done\nThen one more thing?"), BuilderStatus::Question);
        assert_eq!(parse_builder_status("No status line at all"), BuilderStatus::Question);
        assert_eq!(parse_builder_status("Status: question"), BuilderStatus::Question);
        assert_eq!(parse_builder_status(""), BuilderStatus::Question);
        assert_eq!(parse_builder_status("Status: done soon"), BuilderStatus::Question);
        assert_eq!(parse_builder_status("**Status: done**\nmore"), BuilderStatus::Question);
        assert_eq!(parse_builder_status("**Status: question**"), BuilderStatus::Question);
        assert_eq!(parse_builder_status("**Status: done"), BuilderStatus::Question);
    }

    #[test]
    fn status_line_may_be_wrapped_in_emphasis_or_code() {
        for l in ["**Status: done**", "*Status: done*", "_Status: done_", "`Status: done`", "  **STATUS: Done**  "] {
            assert_eq!(parse_builder_status(&format!("Work.\n\n{l}\n\n")), BuilderStatus::Done, "{l}");
        }
    }

    #[test]
    fn review_findings_parsed_and_numbered() {
        let msg = block(
            r#"{"findings":[
              {"title":"Panics on empty input","kind":"bug","blocking":true,
               "evidence":{"type":"command","detail":"cargo test fails"}},
              {"title":"Naming","kind":"design","blocking":false}
            ]}"#,
        );
        let f = findings(parse_review(&msg));
        assert_eq!(f.len(), 2);
        assert_eq!((f[0].id, f[1].id), (1, 2));
        assert_eq!((f[0].kind, f[1].kind), (FindingKind::Bug, FindingKind::Design));
        assert!(f[0].blocking);
        assert!(!f[1].blocking);
        assert_eq!(f[0].evidence.as_ref().unwrap().r#type, EvidenceType::Command);
        assert_eq!(f[1].evidence, None);
    }

    #[test]
    fn blocking_without_evidence_is_advisory() {
        let msg = block(r#"{"findings":[
            {"title":"a","kind":"bug","blocking":true},
            {"title":"b","kind":"bug","blocking":true,"evidence":null},
            {"title":"c","kind":"bug","blocking":true,"evidence":{"type":"test","detail":"  "}}
        ]}"#);
        let f = findings(parse_review(&msg));
        assert!(f.iter().all(|x| !x.blocking));
    }

    #[test]
    fn criterion_needs_file_and_line() {
        let msg = block(r#"{"findings":[
            {"title":"a","kind":"unmet_criterion","blocking":true,
             "evidence":{"type":"criterion","detail":"d","file":"src/a.rs"}},
            {"title":"b","kind":"unmet_criterion","blocking":true,
             "evidence":{"type":"criterion","detail":"d","line":3}},
            {"title":"c","kind":"unmet_criterion","blocking":true,
             "evidence":{"type":"criterion","detail":"d","file":"src/a.rs","line":3}}
        ]}"#);
        let f = findings(parse_review(&msg));
        assert_eq!(f.iter().map(|x| x.blocking).collect::<Vec<_>>(), [false, false, true]);
        assert_eq!(f[2].evidence.as_ref().unwrap().line, Some(3));
    }

    #[test]
    fn review_without_json_is_unparsed() {
        assert!(matches!(parse_review("Looks fine to me."), ReviewOutcome::Unparsed { .. }));
        assert!(matches!(parse_review("```json\n{not json\n```"), ReviewOutcome::Unparsed { .. }));
        // unterminated fence, unknown kind, missing blocking: never read as clean
        assert!(matches!(parse_review("```json\n{\"findings\":[]}"), ReviewOutcome::Unparsed { .. }));
        assert!(matches!(
            parse_review(&block(r#"{"findings":[{"title":"a","kind":"weird","blocking":false}]}"#)),
            ReviewOutcome::Unparsed { .. }
        ));
        assert!(matches!(
            parse_review(&block(r#"{"findings":[{"title":"a","kind":"bug"}]}"#)),
            ReviewOutcome::Unparsed { .. }
        ));
        let raw = "Looks fine to me.";
        assert_eq!(parse_review(raw), ReviewOutcome::Unparsed { raw: raw.into() });
    }

    #[test]
    fn empty_findings_is_a_clean_review() {
        assert_eq!(parse_review("```json\n{\"findings\":[]}\n```"), ReviewOutcome::Findings(vec![]));
    }

    #[test]
    fn last_json_block_wins() {
        let msg = "Format:\n```json\n{\"findings\":[{\"title\":\"example\",\"kind\":\"bug\",\"blocking\":false}]}\n```\nReal:\n```json\n{\"findings\":[]}\n```\n";
        assert_eq!(parse_review(msg), ReviewOutcome::Findings(vec![]));
        // a bad last block is not papered over by an earlier good one
        let msg = "```json\n{\"findings\":[]}\n```\n```json\n{oops\n```";
        assert!(matches!(parse_review(msg), ReviewOutcome::Unparsed { .. }));
        // other fence languages are ignored
        let msg = "```json\n{\"findings\":[]}\n```\n```text\nhi\n```";
        assert_eq!(parse_review(msg), ReviewOutcome::Findings(vec![]));
    }

    #[test]
    fn unterminated_last_fence_is_unparsed() {
        let rev = "```json\n{\"findings\":[]}\n```\nMore:\n```json\n{\"findings\":[{\"title\":\"x\"";
        assert!(matches!(parse_review(rev), ReviewOutcome::Unparsed { .. }));
        let ver = "```json\n{\"results\":[]}\n```\nMore:\n```json\n{\"results\":[{\"finding\":1";
        assert!(matches!(parse_verify(ver), VerifyOutcome::Unparsed { .. }));
    }

    #[test]
    fn verify_results_parsed() {
        let msg = block(r#"{"results":[
            {"finding":1,"status":"fixed","note":"ok"},
            {"finding":2,"status":"not_fixed"},
            {"finding":3,"status":"disputed","note":"not a bug"}
        ]}"#);
        let VerifyOutcome::Results(r) = parse_verify(&msg) else { panic!("unparsed") };
        assert_eq!(
            r.iter().map(|x| (x.finding, x.status)).collect::<Vec<_>>(),
            [(1, VerifyStatus::Fixed), (2, VerifyStatus::NotFixed), (3, VerifyStatus::Disputed)]
        );
        assert_eq!(r[0].note, "ok");
        assert_eq!(r[1].note, "");
        assert!(matches!(parse_verify("nothing"), VerifyOutcome::Unparsed { .. }));
        assert!(matches!(
            parse_verify(&block(r#"{"results":[{"finding":1,"status":"maybe"}]}"#)),
            VerifyOutcome::Unparsed { .. }
        ));
    }

    #[test]
    fn auto_rule_fixes_blocking_only() {
        let mk = |id, blocking| Finding {
            id,
            title: "t".into(),
            kind: FindingKind::Bug,
            blocking,
            evidence: None,
        };
        let r = auto_rule(&[mk(1, true), mk(2, false)]);
        assert_eq!(
            r.iter().map(|x| (x.finding, x.decision)).collect::<Vec<_>>(),
            [(1, Decision::Fix), (2, Decision::Advisory)]
        );
    }
}
