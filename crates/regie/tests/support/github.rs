//! In-memory `GitHub` for integration tests.

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicI64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use regie::github::{BranchPr, GhRepo, GitHub};
use regie_core::{Millis, PrComment, PrLifecycle, PrState};

#[derive(Default)]
pub struct FakeGitHub {
    /// Each branch's PRs, oldest first.
    prs: Mutex<BTreeMap<String, Vec<BranchPr>>>,
    comments: Mutex<BTreeMap<u64, Vec<PrComment>>>,
    closed: Mutex<Vec<u64>>,
    /// The clock a new PR's creation time is read from.
    clock: Option<Arc<AtomicI64>>,
    /// How many of the next calls fail.
    failing: AtomicUsize,
    /// The `owner/name` of every call, in order.
    repos: Mutex<Vec<Option<String>>>,
}

impl FakeGitHub {
    pub fn new() -> Self {
        Self::default()
    }

    /// New PRs are created at the clock's time.
    pub fn with_clock(clock: Arc<AtomicI64>) -> Self {
        FakeGitHub { clock: Some(clock), ..Self::default() }
    }

    /// Opens or updates the branch's PR with this number (created now, if new).
    pub fn set(&self, branch: &str, pr: PrState) {
        let now = Millis(self.clock.as_ref().map_or(0, |c| c.load(Ordering::SeqCst)));
        self.set_created(branch, pr, now);
    }

    /// Like `set`, with the creation time of a new PR given.
    pub fn set_created(&self, branch: &str, pr: PrState, created_at: Millis) {
        let mut prs = self.prs.lock().unwrap();
        let list = prs.entry(branch.to_string()).or_default();
        match list.iter_mut().find(|p| p.pr.number == pr.number) {
            Some(existing) => existing.pr = pr,
            None => list.push(BranchPr { pr, created_at }),
        }
    }

    /// Mark the branch's newest PR merged.
    pub fn merge(&self, branch: &str) {
        if let Some(p) = self.prs.lock().unwrap().get_mut(branch).and_then(|l| l.last_mut()) {
            p.pr.lifecycle = PrLifecycle::Merged;
        }
    }

    pub fn add_comment(&self, number: u64, comment: PrComment) {
        self.comments.lock().unwrap().entry(number).or_default().push(comment);
    }

    /// PR numbers passed to `close_pr`, in call order.
    pub fn closed(&self) -> Vec<u64> {
        self.closed.lock().unwrap().clone()
    }

    /// The next `n` calls fail, as a `gh` that timed out would.
    pub fn fail_next(&self, n: usize) {
        self.failing.store(n, Ordering::SeqCst);
    }

    /// The `owner/name` each call was pointed at.
    pub fn repos(&self) -> Vec<Option<String>> {
        self.repos.lock().unwrap().clone()
    }

    fn call(&self, repo: &GhRepo) -> anyhow::Result<()> {
        self.repos.lock().unwrap().push(repo.github.clone());
        let failing = self.failing.fetch_update(Ordering::SeqCst, Ordering::SeqCst, |n| n.checked_sub(1));
        if failing.is_ok() {
            anyhow::bail!("running gh pr list: timed out after 60 seconds");
        }
        Ok(())
    }
}

#[async_trait::async_trait]
impl GitHub for FakeGitHub {
    async fn prs_for_branch(&self, repo: &GhRepo, branch: &str) -> anyhow::Result<Vec<BranchPr>> {
        self.call(repo)?;
        let mut list = self.prs.lock().unwrap().get(branch).cloned().unwrap_or_default();
        list.reverse();
        Ok(list)
    }

    async fn pr_activity(&self, repo: &GhRepo, number: u64) -> anyhow::Result<Vec<PrComment>> {
        self.call(repo)?;
        Ok(self.comments.lock().unwrap().get(&number).cloned().unwrap_or_default())
    }

    async fn close_pr(&self, repo: &GhRepo, number: u64) -> anyhow::Result<()> {
        self.call(repo)?;
        self.closed.lock().unwrap().push(number);
        for p in self.prs.lock().unwrap().values_mut().flatten() {
            if p.pr.number == number {
                p.pr.lifecycle = PrLifecycle::Closed;
            }
        }
        Ok(())
    }
}
