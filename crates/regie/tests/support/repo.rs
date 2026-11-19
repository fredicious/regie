//! A throwaway git repo with a bare origin, shared by the I/O tests.
use std::path::{Path, PathBuf};
use std::process::Command;

pub struct TempRepo {
    _dir: tempfile::TempDir,
    pub origin: PathBuf,
    pub work: PathBuf,
    pub root: PathBuf,
}

pub fn git(dir: &Path, args: &[&str]) -> String {
    let out = Command::new("git")
        .current_dir(dir)
        .args([
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
        ])
        .args(args)
        .output()
        .expect("run git");
    assert!(
        out.status.success(),
        "git {args:?} failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8_lossy(&out.stdout).trim().to_string()
}

impl TempRepo {
    pub fn new() -> TempRepo {
        let dir = tempfile::tempdir().unwrap();
        let base = dir.path().canonicalize().unwrap();
        let origin = base.join("origin.git");
        let work = base.join("work");
        let root = base.join("worktrees");
        std::fs::create_dir_all(&root).unwrap();
        git(&base, &["init", "--bare", "-b", "main", "origin.git"]);
        git(&base, &["clone", origin.to_str().unwrap(), "work"]);
        git(&work, &["checkout", "-B", "main"]);
        std::fs::write(work.join("README.md"), "hello\n").unwrap();
        git(&work, &["add", "."]);
        git(&work, &["commit", "-m", "initial"]);
        git(&work, &["push", "-u", "origin", "main"]);
        git(&work, &["remote", "set-head", "origin", "main"]);
        std::fs::write(work.join(".env"), "SECRET=1\n").unwrap();
        TempRepo {
            _dir: dir,
            origin,
            work,
            root,
        }
    }

    /// Commit to origin from a separate clone; returns the new sha.
    pub fn push_new_commit_to_origin(&self) -> String {
        let other = self.root.parent().unwrap().join("other");
        if !other.exists() {
            git(
                self.root.parent().unwrap(),
                &["clone", self.origin.to_str().unwrap(), "other"],
            );
        }
        git(&other, &["pull", "origin", "main"]);
        let n = git(&other, &["rev-list", "--count", "HEAD"]);
        std::fs::write(other.join(format!("f{n}.txt")), "x\n").unwrap();
        git(&other, &["add", "."]);
        git(&other, &["commit", "-m", "more"]);
        git(&other, &["push", "origin", "main"]);
        git(&other, &["rev-parse", "HEAD"])
    }

    /// Branch names on the bare origin.
    pub fn origin_branches(&self) -> String {
        git(&self.origin, &["branch", "--list"])
    }
}
