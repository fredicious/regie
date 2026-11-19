//! In-memory `GitHub` for integration tests.

use std::collections::BTreeMap;
use std::path::Path;
use std::sync::Mutex;

use regie::github::GitHub;
use regie_core::{PrComment, PrLifecycle, PrState};

#[derive(Default)]
pub struct FakeGitHub {
    prs: Mutex<BTreeMap<String, PrState>>,
    comments: Mutex<BTreeMap<u64, Vec<PrComment>>>,
    closed: Mutex<Vec<u64>>,
}

impl FakeGitHub {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn set(&self, branch: &str, pr: PrState) {
        self.prs.lock().unwrap().insert(branch.to_string(), pr);
    }

    /// Mark the branch's PR merged.
    pub fn merge(&self, branch: &str) {
        if let Some(pr) = self.prs.lock().unwrap().get_mut(branch) {
            pr.lifecycle = PrLifecycle::Merged;
        }
    }

    pub fn add_comment(&self, number: u64, comment: PrComment) {
        self.comments
            .lock()
            .unwrap()
            .entry(number)
            .or_default()
            .push(comment);
    }

    /// PR numbers passed to `close_pr`, in call order.
    pub fn closed(&self) -> Vec<u64> {
        self.closed.lock().unwrap().clone()
    }
}

#[async_trait::async_trait]
impl GitHub for FakeGitHub {
    async fn pr_for_branch(&self, _repo: &Path, branch: &str) -> anyhow::Result<Option<PrState>> {
        Ok(self.prs.lock().unwrap().get(branch).cloned())
    }

    async fn pr_activity(&self, _repo: &Path, number: u64) -> anyhow::Result<Vec<PrComment>> {
        Ok(self
            .comments
            .lock()
            .unwrap()
            .get(&number)
            .cloned()
            .unwrap_or_default())
    }

    async fn close_pr(&self, _repo: &Path, number: u64) -> anyhow::Result<()> {
        self.closed.lock().unwrap().push(number);
        for pr in self.prs.lock().unwrap().values_mut() {
            if pr.number == number {
                pr.lifecycle = PrLifecycle::Closed;
            }
        }
        Ok(())
    }
}
