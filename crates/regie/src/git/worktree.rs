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
    git_with(dir, args, crate::proc::DEFAULT_TIMEOUT).await
}

/// The environment every git call gets: English output whatever the user's
/// locale, and never a prompt. For network calls, ssh runs in batch mode
/// unless the caller already chose an ssh command (`GIT_SSH_COMMAND` in the
/// environment, or the repo's `core.sshCommand`, which `GIT_SSH_COMMAND` would override).
async fn git_env(dir: &Path, args: &[&str]) -> Vec<(&'static str, String)> {
    let mut env = vec![("LC_ALL", "C".to_string()), ("GIT_TERMINAL_PROMPT", "0".to_string())];
    let network = matches!(args.first(), Some(&"fetch" | &"ls-remote"));
    if network && std::env::var_os("GIT_SSH_COMMAND").is_none() {
        let mut cmd = Command::new("git");
        cmd.current_dir(dir).args(["config", "--get", "core.sshCommand"]).env("LC_ALL", "C");
        let configured = crate::proc::output(cmd, crate::proc::DEFAULT_TIMEOUT)
            .await
            .is_ok_and(|o| o.status.success() && !o.stdout.trim_ascii().is_empty());
        if !configured {
            env.push(("GIT_SSH_COMMAND", "ssh -o BatchMode=yes".to_string()));
        }
    }
    env
}

async fn git_with(dir: &Path, args: &[&str], limit: std::time::Duration) -> anyhow::Result<String> {
    let mut cmd = Command::new("git");
    cmd.current_dir(dir).args(args).envs(git_env(dir, args).await);
    let out = crate::proc::output(cmd, limit).await.with_context(|| format!("running git {args:?}"))?;
    if !out.status.success() {
        bail!("git {args:?} failed: {}", String::from_utf8_lossy(&out.stderr).trim());
    }
    Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

/// The remote's default branch: `origin/HEAD` when set, else what the remote
/// itself says (`ls-remote --symref`, whose output is not translated).
pub async fn default_branch(repo: &Path) -> anyhow::Result<String> {
    if let Ok(r) = git(repo, &["symbolic-ref", "--short", "refs/remotes/origin/HEAD"]).await {
        if let Some(b) = r.strip_prefix("origin/") {
            return Ok(b.to_string());
        }
    }
    let listed = git(repo, &["ls-remote", "--symref", "origin", "HEAD"]).await?;
    parse_symref(&listed).context("could not determine the default branch")
}

/// `ref: refs/heads/main<TAB>HEAD` gives `main`.
fn parse_symref(listed: &str) -> Option<String> {
    listed.lines().find_map(|l| {
        let (target, name) = l.strip_prefix("ref:")?.trim().split_once('\t')?;
        let branch = target.trim().strip_prefix("refs/heads/")?;
        (name.trim() == "HEAD" && !branch.is_empty()).then(|| branch.to_string())
    })
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

    /// A repo whose origin "ssh" hangs, as a fetch waiting on a prompt would.
    fn hanging_origin() -> tempfile::TempDir {
        let dir = tempfile::tempdir().unwrap();
        let run = |args: &[&str]| {
            let ok = std::process::Command::new("git").current_dir(dir.path()).args(args).status().unwrap();
            assert!(ok.success(), "{args:?}");
        };
        run(&["init", "-q"]);
        run(&["remote", "add", "origin", "git@github.com:o/r.git"]);
        run(&["config", "ssh.variant", "simple"]);
        run(&["config", "core.sshCommand", "sh -c 'sleep 30' x"]);
        dir
    }

    #[tokio::test]
    async fn hung_git_times_out() {
        let dir = hanging_origin();
        let t = std::time::Instant::now();
        let err = git_with(dir.path(), &["fetch", "origin"], std::time::Duration::from_millis(300)).await.unwrap_err();
        assert!(format!("{err:#}").contains("timed out"), "{err:#}");
        assert!(t.elapsed() < std::time::Duration::from_secs(5), "took {:?}", t.elapsed());
    }

    #[tokio::test]
    async fn git_never_prompts() {
        let dir = tempfile::tempdir().unwrap();
        std::process::Command::new("git").current_dir(dir.path()).args(["init", "-q"]).status().unwrap();
        let env = git_env(dir.path(), &["fetch", "origin"]).await;
        assert!(env.contains(&("LC_ALL", "C".into())));
        assert!(env.contains(&("GIT_TERMINAL_PROMPT", "0".into())));
        if std::env::var_os("GIT_SSH_COMMAND").is_none() {
            assert!(env.contains(&("GIT_SSH_COMMAND", "ssh -o BatchMode=yes".into())), "{env:?}");
        }
        // A repo's own core.sshCommand is kept (GIT_SSH_COMMAND would override it).
        let dir = hanging_origin();
        let env = git_env(dir.path(), &["fetch", "origin"]).await;
        assert!(!env.iter().any(|(k, _)| *k == "GIT_SSH_COMMAND"), "{env:?}");
    }

    #[test]
    fn symref_lines() {
        assert_eq!(parse_symref("ref: refs/heads/trunk\tHEAD\nabc123\tHEAD").as_deref(), Some("trunk"));
        assert_eq!(parse_symref("abc123\tHEAD"), None);
    }

    #[test]
    fn env_for_task() {
        let e = task_env(TaskId(7), 41020);
        assert!(e.contains(&("REGIE_PORT_BASE".into(), "41020".into())));
        assert!(e.contains(&("COMPOSE_PROJECT_NAME".into(), "regie-t7".into())));
        assert!(e.contains(&("REGIE_TASK".into(), "T7".into())));
    }
}
