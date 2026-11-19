//! GitHub through the `gh` CLI: PR state, CI checks and comments.

use std::path::Path;

use anyhow::Context;
use regie_core::{Checks, MergeState, Millis, PrComment, PrLifecycle, PrState, ReviewDecision};
use serde::Deserialize;

#[async_trait::async_trait]
pub trait GitHub: Send + Sync {
    async fn pr_for_branch(&self, repo: &Path, branch: &str) -> anyhow::Result<Option<PrState>>;
    async fn pr_activity(&self, repo: &Path, number: u64) -> anyhow::Result<Vec<PrComment>>;
    async fn close_pr(&self, repo: &Path, number: u64) -> anyhow::Result<()>;
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct RawPr {
    number: u64,
    url: String,
    state: String,
    #[serde(default)]
    mergeable: String,
    #[serde(default)]
    merge_state_status: String,
    #[serde(default)]
    review_decision: Option<String>,
    #[serde(default)]
    status_check_rollup: Vec<serde_json::Value>,
    #[serde(default)]
    head_ref_oid: String,
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
        checks: aggregate_checks(&r.status_check_rollup),
        conflicting: r.mergeable == "CONFLICTING",
        merge_state: match r.merge_state_status.as_str() {
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
        head_sha: r.head_ref_oid,
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
    let mut failed = Vec::new();
    let mut pending = false;
    for c in rollup {
        if c.get("__typename").and_then(|t| t.as_str()) == Some("StatusContext") {
            match text(c, "state").as_str() {
                "FAILURE" | "ERROR" => failed.push(text(c, "context")),
                "PENDING" | "EXPECTED" => pending = true,
                _ => {}
            }
        } else {
            if text(c, "status") != "COMPLETED" {
                pending = true;
            }
            if matches!(
                text(c, "conclusion").as_str(),
                "FAILURE" | "TIMED_OUT" | "CANCELLED" | "ACTION_REQUIRED"
            ) {
                failed.push(text(c, "name"));
            }
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

/// The real thing: shells out to `gh` in the repo folder.
pub struct GhCli;

impl GhCli {
    async fn gh(repo: &Path, args: &[&str]) -> anyhow::Result<String> {
        let out = tokio::process::Command::new("gh")
            .args(args)
            .current_dir(repo)
            .output()
            .await
            .context("running gh")?;
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
        let json = Self::gh(
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
        let json = Self::gh(
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
        Self::gh(repo, &["pr", "close", &number.to_string()]).await?;
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
    fn rfc3339_parses() {
        assert_eq!(parse_rfc3339("1970-01-01T00:00:00Z"), Some(0));
        assert_eq!(
            parse_rfc3339("2024-02-29T12:00:01Z"),
            Some(1_709_208_001_000)
        );
        assert_eq!(parse_rfc3339("garbage"), None);
    }
}
