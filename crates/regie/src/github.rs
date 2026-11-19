//! GitHub through the `gh` CLI: PR state, CI checks and comments.

use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::Context;
use regie_core::{Checks, MergeState, Millis, PrComment, PrLifecycle, PrState, ReviewDecision};
use serde::Deserialize;

#[async_trait::async_trait]
pub trait GitHub: Send + Sync {
    async fn pr_for_branch(&self, repo: &Path, branch: &str) -> anyhow::Result<Option<PrState>>;
    async fn pr_activity(&self, repo: &Path, number: u64) -> anyhow::Result<Vec<PrComment>>;
    async fn close_pr(&self, repo: &Path, number: u64) -> anyhow::Result<()>;
}

/// `gh` gives `null` for fields GitHub has not computed yet; every such field is optional.
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct RawPr {
    number: u64,
    url: String,
    state: String,
    #[serde(default)]
    mergeable: Option<String>,
    #[serde(default)]
    merge_state_status: Option<String>,
    #[serde(default)]
    review_decision: Option<String>,
    #[serde(default)]
    status_check_rollup: Option<Vec<serde_json::Value>>,
    #[serde(default)]
    head_ref_oid: Option<String>,
}

pub fn parse_pr_list(json: &str) -> anyhow::Result<Option<PrState>> {
    let raw: Vec<RawPr> = serde_json::from_str(json).context("parsing `gh pr list` output")?;
    Ok(raw.into_iter().next().map(|r| PrState {
        number: r.number,
        url: r.url,
        lifecycle: match r.state.as_str() {
            "MERGED" => PrLifecycle::Merged,
            "CLOSED" => PrLifecycle::Closed,
            _ => PrLifecycle::Open,
        },
        checks: aggregate_checks(r.status_check_rollup.as_deref().unwrap_or_default()),
        conflicting: r.mergeable.as_deref() == Some("CONFLICTING"),
        merge_state: match r.merge_state_status.as_deref().unwrap_or_default() {
            "CLEAN" => MergeState::Clean,
            "BLOCKED" => MergeState::Blocked,
            "BEHIND" => MergeState::Behind,
            "UNSTABLE" => MergeState::Unstable,
            "DIRTY" => MergeState::Dirty,
            _ => MergeState::Unknown,
        },
        review_decision: match r.review_decision.as_deref() {
            Some("APPROVED") => Some(ReviewDecision::Approved),
            Some("CHANGES_REQUESTED") => Some(ReviewDecision::ChangesRequested),
            Some("REVIEW_REQUIRED") => Some(ReviewDecision::ReviewRequired),
            _ => None,
        },
        head_sha: r.head_ref_oid.unwrap_or_default(),
    }))
}

#[derive(Deserialize)]
struct RawActivity {
    #[serde(default)]
    comments: Vec<RawEntry>,
    #[serde(default)]
    reviews: Vec<RawEntry>,
}

#[derive(Deserialize)]
struct RawEntry {
    #[serde(default)]
    id: String,
    author: Option<RawAuthor>,
    #[serde(default)]
    body: String,
    #[serde(default)]
    state: String,
    #[serde(default, rename = "createdAt")]
    created_at: Option<String>,
    #[serde(default, rename = "submittedAt")]
    submitted_at: Option<String>,
}

#[derive(Deserialize)]
struct RawAuthor {
    #[serde(default)]
    login: String,
}

pub fn parse_activity(json: &str) -> anyhow::Result<Vec<PrComment>> {
    let raw: RawActivity = serde_json::from_str(json).context("parsing `gh pr view` output")?;
    let mut out = Vec::new();
    for (entries, is_review) in [(raw.comments, false), (raw.reviews, true)] {
        for e in entries {
            let when = e
                .created_at
                .as_deref()
                .or(e.submitted_at.as_deref())
                .unwrap_or("");
            out.push(PrComment {
                id: e.id,
                author: e.author.map(|a| a.login).unwrap_or_default(),
                body: e.body,
                changes_requested: is_review && e.state == "CHANGES_REQUESTED",
                at: Millis(parse_rfc3339(when).unwrap_or(0)),
            });
        }
    }
    out.sort_by_key(|c| c.at);
    Ok(out)
}

/// Parse the `YYYY-MM-DDTHH:MM:SSZ` form gh emits into unix milliseconds.
fn parse_rfc3339(s: &str) -> Option<i64> {
    let b = s.as_bytes();
    if b.len() != 20
        || b[4] != b'-'
        || b[7] != b'-'
        || b[10] != b'T'
        || b[13] != b':'
        || b[16] != b':'
        || b[19] != b'Z'
    {
        return None;
    }
    let n = |r: std::ops::Range<usize>| s.get(r)?.parse::<i64>().ok();
    let (y, m, d) = (n(0..4)?, n(5..7)?, n(8..10)?);
    let (hh, mm, ss) = (n(11..13)?, n(14..16)?, n(17..19)?);
    if !(1..=12).contains(&m) || !(1..=31).contains(&d) || hh > 23 || mm > 59 || ss > 60 {
        return None;
    }
    // Days from civil (Howard Hinnant).
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let doy = (153 * (if m > 2 { m - 3 } else { m + 9 }) + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let days = era * 146_097 + doe - 719_468;
    Some(((days * 24 + hh) * 60 + mm) * 60_000 + ss * 1000)
}

pub fn aggregate_checks(rollup: &[serde_json::Value]) -> Checks {
    if rollup.is_empty() {
        return Checks::None;
    }
    let text = |v: &serde_json::Value, k: &str| {
        v.get(k).and_then(|x| x.as_str()).unwrap_or("").to_string()
    };
    // Only known-good answers pass: a completed check run with any other
    // conclusion failed, and a status context in an unknown state is pending.
    let mut failed = Vec::new();
    let mut pending = false;
    for c in rollup {
        if c.get("__typename").and_then(|t| t.as_str()) == Some("StatusContext") {
            match text(c, "state").as_str() {
                "SUCCESS" => {}
                "FAILURE" | "ERROR" => failed.push(text(c, "context")),
                _ => pending = true,
            }
        } else if text(c, "status") != "COMPLETED" {
            pending = true;
        } else if !matches!(text(c, "conclusion").as_str(), "SUCCESS" | "NEUTRAL" | "SKIPPED") {
            failed.push(text(c, "name"));
        }
    }
    if !failed.is_empty() {
        Checks::Failure { failed }
    } else if pending {
        Checks::Pending
    } else {
        Checks::Success
    }
}

/// The real thing: shells out to `gh` (normally from `PATH`) in the repo folder.
pub struct GhCli {
    pub bin: PathBuf,
    /// How long one `gh` call may take; a call that hangs counts as failed.
    pub timeout: Duration,
}

impl Default for GhCli {
    fn default() -> Self {
        GhCli { bin: PathBuf::from("gh"), timeout: crate::proc::DEFAULT_TIMEOUT }
    }
}

impl GhCli {
    async fn gh(&self, repo: &Path, args: &[&str]) -> anyhow::Result<String> {
        let mut cmd = tokio::process::Command::new(&self.bin);
        cmd.args(args).current_dir(repo).env("GH_PROMPT_DISABLED", "1");
        let out = crate::proc::output(cmd, self.timeout)
            .await
            .with_context(|| format!("running gh {}", args.join(" ")))?;
        if !out.status.success() {
            anyhow::bail!(
                "gh {} failed: {}",
                args.join(" "),
                String::from_utf8_lossy(&out.stderr).trim()
            );
        }
        Ok(String::from_utf8(out.stdout)?)
    }
}

#[async_trait::async_trait]
impl GitHub for GhCli {
    async fn pr_for_branch(&self, repo: &Path, branch: &str) -> anyhow::Result<Option<PrState>> {
        let json = self.gh(
            repo,
            &[
                "pr", "list", "--head", branch, "--state", "all", "--limit", "1", "--json",
                "number,url,state,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup,headRefOid",
            ],
        )
        .await?;
        parse_pr_list(&json)
    }

    async fn pr_activity(&self, repo: &Path, number: u64) -> anyhow::Result<Vec<PrComment>> {
        let json = self.gh(
            repo,
            &[
                "pr",
                "view",
                &number.to_string(),
                "--json",
                "comments,reviews",
            ],
        )
        .await?;
        parse_activity(&json)
    }

    async fn close_pr(&self, repo: &Path, number: u64) -> anyhow::Result<()> {
        self.gh(repo, &["pr", "close", &number.to_string()]).await?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const PENDING: &str = include_str!("../tests/fixtures/gh/pr_list_pending.json");
    const FAILED: &str = include_str!("../tests/fixtures/gh/pr_list_failed.json");
    const CLEAN: &str = include_str!("../tests/fixtures/gh/pr_list_clean.json");
    const CONFLICT: &str = include_str!("../tests/fixtures/gh/pr_list_conflict.json");
    const MERGED: &str = include_str!("../tests/fixtures/gh/pr_list_merged.json");
    const ACTIVITY: &str = include_str!("../tests/fixtures/gh/pr_view_activity.json");

    fn pr(json: &str) -> PrState {
        parse_pr_list(json).unwrap().unwrap()
    }

    #[test]
    fn pending_checks() {
        let p = pr(PENDING);
        assert_eq!(p.checks, Checks::Pending);
        assert_eq!(p.number, 11);
        assert_eq!(p.head_sha, "abc123def456");
        assert_eq!(p.merge_state, MergeState::Blocked);
        assert_eq!(p.review_decision, None);
    }

    #[test]
    fn failed_checks_named() {
        assert_eq!(
            pr(FAILED).checks,
            Checks::Failure {
                failed: vec!["build".into(), "ci/lint".into()]
            }
        );
    }

    #[test]
    fn clean_pr() {
        let p = pr(CLEAN);
        assert_eq!(p.checks, Checks::Success);
        assert_eq!(p.merge_state, MergeState::Clean);
        assert!(!p.conflicting);
        assert_eq!(p.lifecycle, PrLifecycle::Open);
        assert_eq!(p.review_decision, Some(ReviewDecision::Approved));
    }

    #[test]
    fn conflict_pr() {
        let p = pr(CONFLICT);
        assert!(p.conflicting);
        assert_eq!(p.merge_state, MergeState::Dirty);
        assert_eq!(p.checks, Checks::None);
        assert_eq!(p.review_decision, Some(ReviewDecision::ReviewRequired));
    }

    #[test]
    fn merged_pr() {
        let p = pr(MERGED);
        assert_eq!(p.lifecycle, PrLifecycle::Merged);
        assert_eq!(p.merge_state, MergeState::Unknown);
    }

    #[test]
    fn no_pr_is_none() {
        assert_eq!(parse_pr_list("[]").unwrap(), None);
    }

    #[test]
    fn activity_comments_and_change_requests() {
        let a = parse_activity(ACTIVITY).unwrap();
        let ids: Vec<_> = a.iter().map(|c| c.id.as_str()).collect();
        assert_eq!(ids, ["PRR_1", "IC_2", "PRR_3"]);
        assert_eq!(a[0].author, "alice");
        assert!(!a[0].changes_requested);
        assert_eq!(a[1].body, "Looks odd here.");
        assert!(a[2].changes_requested);
        assert_eq!(a[2].body, "");
        // 2026-11-19T10:00:00Z
        assert_eq!(a[0].at, Millis(1_795_082_400_000));
    }

    #[test]
    fn null_fields_are_accepted() {
        let json = r#"[{"number":4,"url":"u","state":"OPEN","mergeable":null,"mergeStateStatus":null,
            "reviewDecision":null,"statusCheckRollup":null,"headRefOid":null}]"#;
        let p = pr(json);
        assert_eq!(p.checks, Checks::None);
        assert_eq!(p.merge_state, MergeState::Unknown);
        assert!(!p.conflicting);
        assert_eq!(p.head_sha, "");
    }

    #[test]
    fn check_conclusions_table() {
        use serde_json::json;
        let run = |status: &str, conclusion: &str| {
            json!({"__typename":"CheckRun","name":conclusion.to_lowercase(),"status":status,"conclusion":conclusion})
        };
        let ctx = |state: &str| json!({"__typename":"StatusContext","context":state.to_lowercase(),"state":state});
        let failed = |n: &str| Checks::Failure { failed: vec![n.into()] };
        let rows: Vec<(&str, Vec<serde_json::Value>, Checks)> = vec![
            ("success", vec![run("COMPLETED", "SUCCESS")], Checks::Success),
            ("neutral", vec![run("COMPLETED", "NEUTRAL")], Checks::Success),
            ("skipped", vec![run("COMPLETED", "SKIPPED")], Checks::Success),
            ("timed out", vec![run("COMPLETED", "TIMED_OUT")], failed("timed_out")),
            ("cancelled", vec![run("COMPLETED", "CANCELLED")], failed("cancelled")),
            ("action required", vec![run("COMPLETED", "ACTION_REQUIRED")], failed("action_required")),
            ("startup failure", vec![run("COMPLETED", "STARTUP_FAILURE")], failed("startup_failure")),
            ("stale", vec![run("COMPLETED", "STALE")], failed("stale")),
            ("in progress", vec![run("IN_PROGRESS", "")], Checks::Pending),
            ("status success", vec![ctx("SUCCESS")], Checks::Success),
            ("status error", vec![ctx("ERROR")], failed("error")),
            ("status failure", vec![ctx("FAILURE")], failed("failure")),
            ("status pending", vec![ctx("PENDING")], Checks::Pending),
            ("status expected", vec![ctx("EXPECTED")], Checks::Pending),
            ("status unknown", vec![ctx("SOMETHING_NEW")], Checks::Pending),
            (
                "failure beats pending",
                vec![run("IN_PROGRESS", ""), ctx("PENDING"), run("COMPLETED", "STARTUP_FAILURE")],
                failed("startup_failure"),
            ),
            ("empty", vec![], Checks::None),
        ];
        for (name, rollup, want) in rows {
            assert_eq!(aggregate_checks(&rollup), want, "{name}");
        }
    }

    /// A `gh` stand-in: a shell script in a temp folder.
    fn fake_gh(dir: &tempfile::TempDir, body: &str, timeout: std::time::Duration) -> GhCli {
        use std::os::unix::fs::PermissionsExt;
        let bin = dir.path().join("gh");
        std::fs::write(&bin, format!("#!/bin/sh\n{body}\n")).unwrap();
        std::fs::set_permissions(&bin, std::fs::Permissions::from_mode(0o755)).unwrap();
        GhCli { bin, timeout }
    }

    #[tokio::test]
    async fn hung_gh_times_out() {
        let dir = tempfile::tempdir().unwrap();
        let gh = fake_gh(&dir, "sleep 30", std::time::Duration::from_millis(300));
        let t = std::time::Instant::now();
        let err = gh.pr_activity(dir.path(), 3).await.unwrap_err();
        assert!(format!("{err:#}").contains("timed out"), "{err:#}");
        assert!(t.elapsed() < std::time::Duration::from_secs(5), "took {:?}", t.elapsed());
    }

    #[test]
    fn rfc3339_parses() {
        assert_eq!(parse_rfc3339("1970-01-01T00:00:00Z"), Some(0));
        assert_eq!(
            parse_rfc3339("2024-02-29T12:00:01Z"),
            Some(1_709_208_001_000)
        );
        assert_eq!(parse_rfc3339("garbage"), None);
    }
}
