//! Running short external commands (`git`, `gh`) with a time limit.

use std::process::{Output, Stdio};
use std::time::Duration;

use tokio::process::Command;

/// How long a `git` or `gh` call may take before it counts as failed.
pub const DEFAULT_TIMEOUT: Duration = Duration::from_secs(60);

/// Runs `cmd` to completion and collects its output; past `limit` the command
/// and every process it started are killed and this fails with "timed out".
pub async fn output(mut cmd: Command, limit: Duration) -> anyhow::Result<Output> {
    cmd.stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0)
        .kill_on_drop(true);
    let child = cmd.spawn()?;
    let pid = child.id().and_then(|p| i32::try_from(p).ok());
    match tokio::time::timeout(limit, child.wait_with_output()).await {
        Ok(out) => Ok(out?),
        Err(_) => {
            if let Some(pid) = pid {
                // SAFETY: kill(2) has no memory effects. The group was created by
                // `process_group(0)` above and its leader is not reaped yet.
                unsafe {
                    libc::kill(-pid, libc::SIGKILL);
                }
            }
            anyhow::bail!("timed out after {} seconds", limit.as_secs_f32())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn a_hung_command_is_killed() {
        let mut cmd = Command::new("sh");
        cmd.args(["-c", "sleep 30 & wait"]);
        let t = std::time::Instant::now();
        let err = output(cmd, Duration::from_millis(200)).await.unwrap_err();
        assert!(err.to_string().contains("timed out"), "{err}");
        assert!(t.elapsed() < Duration::from_secs(5));
    }

    #[tokio::test]
    async fn output_is_collected() {
        let mut cmd = Command::new("sh");
        cmd.args(["-c", "echo out; echo err >&2; exit 3"]);
        let out = output(cmd, Duration::from_secs(5)).await.unwrap();
        assert_eq!(
            (out.stdout.as_slice(), out.stderr.as_slice()),
            (&b"out\n"[..], &b"err\n"[..])
        );
        assert_eq!(out.status.code(), Some(3));
    }
}
