mod support;

use regie::github::{GhRepo, GitHub};
use regie_core::{Checks, MergeState, PrLifecycle, PrState};
use support::github::FakeGitHub;

fn pr(number: u64) -> PrState {
    PrState {
        number,
        url: format!("https://example.test/pull/{number}"),
        lifecycle: PrLifecycle::Open,
        checks: Checks::Pending,
        conflicting: false,
        merge_state: MergeState::Blocked,
        review_decision: None,
        head_sha: "abc".into(),
    }
}

#[tokio::test]
async fn fake_tracks_merge_and_close() {
    let gh = FakeGitHub::new();
    let repo = &GhRepo { path: ".".into(), github: Some("o/r".into()) };
    assert_eq!(gh.prs_for_branch(repo, "b").await.unwrap(), vec![]);
    gh.set("b", pr(1));
    gh.set("c", pr(2));
    gh.merge("b");
    assert_eq!(
        gh.prs_for_branch(repo, "b").await.unwrap()[0].pr.lifecycle,
        PrLifecycle::Merged
    );
    gh.close_pr(repo, 2).await.unwrap();
    assert_eq!(gh.closed(), vec![2]);
    assert_eq!(
        gh.prs_for_branch(repo, "c").await.unwrap()[0].pr.lifecycle,
        PrLifecycle::Closed
    );
    assert!(gh.pr_activity(repo, 1).await.unwrap().is_empty());
    assert!(gh.repos().iter().all(|r| r.as_deref() == Some("o/r")));
}
