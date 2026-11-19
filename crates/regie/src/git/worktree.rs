use super::locks::RepoLocks;
use anyhow::{bail, Context};
use regie_core::{TaskId, Worktree};
use std::path::{Component, Path};
use tokio::process::Command;

/// Lowercase ASCII, other characters to `-`, runs collapsed, trimmed, at most 40 characters.
pub fn slug(title: &str) -> String {
    let mut out = String::new();
    for c in title.chars() {
        let c = if c.is_ascii_alphanumeric() { c.to_ascii_lowercase() } else { '-' };
        if c == '-' && (out.is_empty() || out.ends_with('-')) {
            continue;
        }
        out.push(c);
    }
    out.truncate(40);
    out.trim_end_matches('-').to_string()
}

pub fn branch_name(task: TaskId, title: &str) -> String {
    format!("regie/t{}-{}", task.0, slug(title))
}

pub fn task_env(task: TaskId, port_base: u16) -> Vec<(String, String)> {
    vec![
        ("REGIE_TASK".into(), format!("T{}", task.0)),
        ("REGIE_PORT_BASE".into(), port_base.to_string()),
        ("COMPOSE_PROJECT_NAME".into(), format!("regie-t{}", task.0)),
    ]
}

async fn git(dir: &Path, args: &[&str]) -> anyhow::Result<String> {
    let out = Command::new("git")
        .current_dir(dir)
        .args(args)
        .output()
        .await
        .with_context(|| format!("running git {args:?}"))?;
    if !out.status.success() {
        bail!("git {args:?} failed: {}", String::from_utf8_lossy(&out.stderr).trim());
    }
    Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

pub async fn default_branch(repo: &Path) -> anyhow::Result<String> {
    if let Ok(r) = git(repo, &["symbolic-ref", "--short", "refs/remotes/origin/HEAD"]).await {
        if let Some(b) = r.strip_prefix("origin/") {
            return Ok(b.to_string());
        }
    }
    let shown = git(repo, &["remote", "show", "origin"]).await?;
    shown
        .lines()
        .find_map(|l| l.trim().strip_prefix("HEAD branch:"))
        .map(|b| b.trim().to_string())
        .filter(|b| !b.is_empty() && b != "(unknown)")
        .context("could not determine the default branch")
}

/// `owner/name` when origin is on github.com (SSH or HTTPS form).
pub async fn github_remote(repo: &Path) -> Option<String> {
    let url = git(repo, &["remote", "get-url", "origin"]).await.ok()?;
    let rest = url
        .strip_prefix("git@github.com:")
        .or_else(|| url.strip_prefix("ssh://git@github.com/"))
        .or_else(|| url.strip_prefix("https://github.com/"))
        .or_else(|| url.strip_prefix("http://github.com/"))?;
    let rest = rest.trim_end_matches('/');
    let rest = rest.strip_suffix(".git").unwrap_or(rest);
    let (owner, name) = rest.split_once('/')?;
    if owner.is_empty() || name.is_empty() || name.contains('/') {
        return None;
    }
    Some(format!("{owner}/{name}"))
}

pub async fn create_worktree(
    locks: &RepoLocks,
    repo: &Path,
    root: &Path,
    task: TaskId,
    title: &str,
    copy_files: &[String],
    port_base: u16,
) -> anyhow::Result<Worktree> {
    for f in copy_files {
        let p = Path::new(f);
        if p.is_absolute()
            || p.components().any(|c| matches!(c, Component::ParentDir | Component::Prefix(_)))
        {
            bail!("copy file {f:?} must be a relative path inside the repo");
        }
    }
    let _guard = locks.lock(repo).await;
    let branch = branch_name(task, title);
    let path = root.join(format!("t{}-{}", task.0, slug(title)));
    git(repo, &["fetch", "origin"]).await?;
    let base = format!("origin/{}", default_branch(repo).await?);
    tokio::fs::create_dir_all(root).await?;
    let path_str = path.to_str().context("worktree path is not UTF-8")?;
    git(repo, &["worktree", "add", "--no-track", "-b", &branch, path_str, &base]).await?;
    for f in copy_files {
        let src = repo.join(f);
        if tokio::fs::metadata(&src).await.map(|m| m.is_file()).unwrap_or(false) {
            let dst = path.join(f);
            if let Some(dir) = dst.parent() {
                tokio::fs::create_dir_all(dir).await?;
            }
            tokio::fs::copy(&src, &dst).await.with_context(|| format!("copying {f}"))?;
        }
    }
    Ok(Worktree { path: path_str.to_string(), branch, port_base })
}

/// Runs `sh -c cmd` in the worktree; returns success and the last 4,000 characters of output.
pub async fn run_setup(path: &Path, cmd: &str, env: &[(String, String)]) -> (bool, String) {
    let out = Command::new("sh")
        .arg("-c")
        .arg(cmd)
        .current_dir(path)
        .envs(env.iter().map(|(k, v)| (k, v)))
        .output()
        .await;
    match out {
        Ok(o) => {
            let mut text = String::from_utf8_lossy(&o.stdout).into_owned();
            text.push_str(&String::from_utf8_lossy(&o.stderr));
            let n = text.chars().count();
            let tail = if n > 4000 { text.chars().skip(n - 4000).collect() } else { text };
            (o.status.success(), tail)
        }
        Err(e) => (false, format!("could not run setup: {e}")),
    }
}

pub async fn remove_worktree(locks: &RepoLocks, repo: &Path, wt: &Worktree) -> anyhow::Result<()> {
    let _guard = locks.lock(repo).await;
    git(repo, &["worktree", "remove", "--force", &wt.path]).await?;
    git(repo, &["branch", "-D", &wt.branch]).await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn slug_and_branch() {
        assert_eq!(branch_name(TaskId(7), "Add CSV export: v2!"), "regie/t7-add-csv-export-v2");
        assert_eq!(slug(&"a".repeat(60)).len(), 40);
        assert_eq!(slug("  --Hi--  "), "hi");
    }

    #[test]
    fn env_for_task() {
        let e = task_env(TaskId(7), 41020);
        assert!(e.contains(&("REGIE_PORT_BASE".into(), "41020".into())));
        assert!(e.contains(&("COMPOSE_PROJECT_NAME".into(), "regie-t7".into())));
        assert!(e.contains(&("REGIE_TASK".into(), "T7".into())));
    }
}
