use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use tokio::sync::OwnedMutexGuard;

/// One async mutex per repo path, so git operations on one repo run one at a time.
#[derive(Default)]
pub struct RepoLocks {
    map: Mutex<HashMap<PathBuf, Arc<tokio::sync::Mutex<()>>>>,
}

impl RepoLocks {
    pub async fn lock(&self, repo: &Path) -> OwnedMutexGuard<()> {
        let m = self.map.lock().unwrap().entry(repo.to_path_buf()).or_default().clone();
        m.lock_owned().await
    }
}
