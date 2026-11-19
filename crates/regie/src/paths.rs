//! Where Régie keeps its files.

use std::path::PathBuf;

use anyhow::Context;
use regie_core::RunId;

/// The data directory (`~/.regie` by default).
#[derive(Clone, Debug)]
pub struct DataDir {
    pub root: PathBuf,
}

impl Default for DataDir {
    fn default() -> Self {
        let home = std::env::var_os("HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("."));
        DataDir {
            root: home.join(".regie"),
        }
    }
}

impl DataDir {
    pub fn db(&self) -> PathBuf {
        self.root.join("regie.db")
    }

    pub fn server_json(&self) -> PathBuf {
        self.root.join("server.json")
    }

    pub fn runs(&self) -> PathBuf {
        self.root.join("runs")
    }

    pub fn run_file(&self, run: RunId) -> PathBuf {
        self.runs().join(format!("{}.jsonl", run.0))
    }

    pub fn worktrees(&self) -> PathBuf {
        self.root.join("worktrees")
    }

    /// Creates the root, `runs/` and `worktrees/` if missing.
    pub fn ensure(&self) -> anyhow::Result<()> {
        for dir in [self.root.clone(), self.runs(), self.worktrees()] {
            std::fs::create_dir_all(&dir).with_context(|| format!("creating {}", dir.display()))?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use regie_core::RunId;

    #[test]
    fn layout_and_ensure() {
        let tmp = tempfile::tempdir().unwrap();
        let d = DataDir {
            root: tmp.path().join(".regie"),
        };
        d.ensure().unwrap();
        d.ensure().unwrap();
        assert!(d.runs().is_dir() && d.worktrees().is_dir());
        assert_eq!(d.db(), d.root.join("regie.db"));
        assert_eq!(d.server_json(), d.root.join("server.json"));
        assert_eq!(d.run_file(RunId(7)), d.root.join("runs").join("7.jsonl"));
    }
}
