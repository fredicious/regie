mod support;
use regie::git::{create_worktree, default_branch, github_remote, remove_worktree, run_setup, RepoLocks};
use regie_core::TaskId;
use std::path::Path;
use support::repo::{git, TempRepo};

#[tokio::test]
async fn creates_worktree_from_fresh_origin() {
    let r = TempRepo::new();
    let sha = r.push_new_commit_to_origin();
    let locks = RepoLocks::default();
    let wt = create_worktree(&locks, &r.work, &r.root, TaskId(7), "Add CSV export: v2!", &[".env".into(), "missing.txt".into()], 41020)
        .await
        .unwrap();
    assert_eq!(wt.branch, "regie/t7-add-csv-export-v2");
    assert_eq!(wt.port_base, 41020);
    assert_eq!(wt.base, "main");
    assert_eq!(Path::new(&wt.path), r.root.join("t7-add-csv-export-v2"));
    assert_eq!(git(Path::new(&wt.path), &["rev-parse", "HEAD"]), sha);
    assert_eq!(std::fs::read_to_string(Path::new(&wt.path).join(".env")).unwrap(), "SECRET=1\n");
    assert!(!Path::new(&wt.path).join("missing.txt").exists());
    // the user's own working copy is untouched
    assert_ne!(git(&r.work, &["rev-parse", "HEAD"]), sha);
}

#[tokio::test]
async fn default_branch_without_origin_head() {
    let r = TempRepo::new();
    assert_eq!(default_branch(&r.work).await.unwrap(), "main");
    git(&r.work, &["symbolic-ref", "--delete", "refs/remotes/origin/HEAD"]);
    assert_eq!(default_branch(&r.work).await.unwrap(), "main");
}

#[tokio::test]
async fn setup_gets_env_and_tail() {
    let dir = tempfile::tempdir().unwrap();
    let env = regie::git::task_env(TaskId(7), 41020);
    let (ok, out) = run_setup(dir.path(), "echo $REGIE_PORT_BASE; exit 3", &env).await;
    assert!(!ok);
    assert_eq!(out, "41020\n");
    let (ok, out) = run_setup(dir.path(), "echo err >&2; echo out", &env).await;
    assert!(ok);
    assert!(out.contains("err") && out.contains("out"));
    let (_, out) = run_setup(dir.path(), "yes x | head -c 9000", &env).await;
    assert_eq!(out.chars().count(), 4000);
}

#[tokio::test]
async fn remove_cleans_worktree_and_branch() {
    let r = TempRepo::new();
    let locks = RepoLocks::default();
    let before = r.origin_branches();
    let wt = create_worktree(&locks, &r.work, &r.root, TaskId(1), "thing", &[], 41000).await.unwrap();
    remove_worktree(&locks, &r.work, &wt).await.unwrap();
    assert!(!Path::new(&wt.path).exists());
    assert!(!git(&r.work, &["branch", "--list"]).contains("regie/"));
    assert_eq!(r.origin_branches(), before);
}

#[tokio::test]
async fn parallel_worktrees_same_repo() {
    let r = TempRepo::new();
    let locks = RepoLocks::default();
    let (a, b) = tokio::join!(
        create_worktree(&locks, &r.work, &r.root, TaskId(1), "one", &[], 41000),
        create_worktree(&locks, &r.work, &r.root, TaskId(2), "two", &[], 41010),
    );
    let (a, b) = (a.unwrap(), b.unwrap());
    assert!(Path::new(&a.path).is_dir() && Path::new(&b.path).is_dir());
}

#[tokio::test]
async fn github_remote_parsing() {
    let r = TempRepo::new();
    for (url, want) in [
        ("git@github.com:o/n.git", Some("o/n")),
        ("https://github.com/o/n", Some("o/n")),
        ("https://github.com/o/n.git", Some("o/n")),
        ("git@gitlab.com:o/n.git", None),
    ] {
        git(&r.work, &["remote", "set-url", "origin", url]);
        assert_eq!(github_remote(&r.work).await.as_deref(), want, "{url}");
    }
}

#[tokio::test]
async fn rejects_copy_paths_outside_the_repo() {
    let r = TempRepo::new();
    let locks = RepoLocks::default();
    let abs = r.work.join(".env").to_str().unwrap().to_string();
    let outside = r.root.parent().unwrap().join("x");
    std::fs::write(&outside, "keep\n").unwrap();
    for bad in ["../x".to_string(), "../../x".to_string(), abs] {
        let res = create_worktree(&locks, &r.work, &r.root, TaskId(3), "bad", std::slice::from_ref(&bad), 41000).await;
        assert!(res.is_err(), "{bad}");
        assert!(!git(&r.work, &["branch", "--list"]).contains("regie/"), "no git call before validation");
    }
    assert_eq!(std::fs::read_to_string(r.work.join(".env")).unwrap(), "SECRET=1\n");
    assert_eq!(std::fs::read_to_string(&outside).unwrap(), "keep\n");
    assert!(!r.root.join("x").exists());
}

#[tokio::test]
async fn task_branch_has_no_upstream() {
    let r = TempRepo::new();
    let locks = RepoLocks::default();
    let wt = create_worktree(&locks, &r.work, &r.root, TaskId(4), "solo", &[], 41000).await.unwrap();
    let out = std::process::Command::new("git")
        .current_dir(&r.work)
        .args(["rev-parse", "--abbrev-ref", &format!("{}@{{upstream}}", wt.branch)])
        .output()
        .unwrap();
    assert!(!out.status.success());
}
