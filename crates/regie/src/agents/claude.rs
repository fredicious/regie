//! Claude Code run headless: `claude -p` with stream-json in and out.

use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use anyhow::Context;
use regie_core::{Effort, Millis, Reading, RunOutcome};
use serde_json::json;
use tokio::fs::File;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::mpsc;
use tokio::time::{sleep_until, Instant};

use super::stream::{parse_line, StreamItem};
use super::{AgentControl, AgentEvent, AgentRunner, RunHandle, RunSpec};

/// How long a stopped process gets between SIGTERM and SIGKILL.
const STOP_GRACE: Duration = Duration::from_secs(5);
/// How long to keep reading output after the process exits (a grandchild may
/// hold the pipe open).
const DRAIN_AFTER_EXIT: Duration = Duration::from_secs(2);
const PROBE_TIMEOUT: Duration = Duration::from_secs(120);

/// The output line a run reports when a message could not reach the agent.
pub const MESSAGE_NOT_DELIVERED: &str = "message not delivered: the run has already ended";

/// Runs `bin` (normally `claude` from `PATH`) and appends every stdout line to
/// `runs_dir/<run id>.jsonl`.
#[derive(Clone, Debug)]
pub struct ClaudeCli {
    pub bin: PathBuf,
    pub runs_dir: PathBuf,
}

impl ClaudeCli {
    pub fn args(spec: &RunSpec) -> Vec<String> {
        let mut args: Vec<String> = [
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "auto",
            "--model",
        ]
        .map(String::from)
        .into();
        args.push(spec.model.clone());
        args.push("--effort".into());
        args.push(effort_arg(spec.effort).into());
        match &spec.resume {
            Some(id) => args.extend(["--resume".into(), id.clone()]),
            None => args.extend(["--session-id".into(), uuid::Uuid::new_v4().to_string()]),
        }
        args
    }
}

pub fn effort_arg(e: Effort) -> &'static str {
    match e {
        Effort::Low => "low",
        Effort::Medium => "medium",
        Effort::High => "high",
        Effort::Xhigh => "xhigh",
        Effort::Max => "max",
    }
}

fn user_line(text: &str) -> String {
    format!("{}\n", json!({"type": "user", "message": {"role": "user", "content": text}}))
}

fn interrupt_line() -> String {
    let id = uuid::Uuid::new_v4().to_string();
    format!("{}\n", json!({"type": "control_request", "request_id": id, "request": {"subtype": "interrupt"}}))
}

fn now() -> Millis {
    let d = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default();
    Millis(d.as_millis() as i64)
}

/// Signals the whole process group the agent leads (its tool processes too).
fn signal_group(pid: Option<u32>, sig: libc::c_int) {
    if let Some(pid) = pid.and_then(|p| i32::try_from(p).ok()) {
        // SAFETY: kill(2) has no memory effects; the group exists until the leader is reaped.
        unsafe {
            libc::kill(-pid, sig);
        }
    }
}

/// A far-off instant for disabled timer branches.
fn or_never(t: Option<Instant>) -> Instant {
    t.unwrap_or_else(|| Instant::now() + Duration::from_secs(365 * 86_400))
}

#[async_trait::async_trait]
impl AgentRunner for ClaudeCli {
    async fn start(&self, spec: RunSpec) -> anyhow::Result<RunHandle> {
        tokio::fs::create_dir_all(&self.runs_dir)
            .await
            .with_context(|| format!("creating {}", self.runs_dir.display()))?;
        let path = self.runs_dir.join(format!("{}.jsonl", spec.run.0));
        let run_file = tokio::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&path)
            .await
            .with_context(|| format!("opening {}", path.display()))?;
        let child = Command::new(&self.bin)
            .args(Self::args(&spec))
            .current_dir(&spec.cwd)
            .envs(spec.env.iter().map(|(k, v)| (k, v)))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .process_group(0)
            .kill_on_drop(true)
            .spawn()
            .with_context(|| format!("starting {}", self.bin.display()))?;
        let (events_tx, events) = mpsc::channel(256);
        let (control, control_rx) = mpsc::channel(16);
        tokio::spawn(drive(child, spec.prompt, run_file, events_tx, control_rx));
        Ok(RunHandle { events, control })
    }
}

/// Writes one line to the agent's stdin; a closed pipe closes our side too.
async fn send_line(stdin: &mut Option<ChildStdin>, line: &str) -> bool {
    let Some(w) = stdin.as_mut() else { return false };
    let ok = w.write_all(line.as_bytes()).await.is_ok() && w.flush().await.is_ok();
    if !ok {
        *stdin = None;
    }
    ok
}

#[derive(Default)]
struct Progress {
    attached: bool,
    /// The outcome asked for by `Interrupt` or `Stop` before any result.
    requested: Option<RunOutcome>,
    result: Option<Option<String>>,
    rejected: Option<Millis>,
}

impl Progress {
    fn outcome(&self) -> RunOutcome {
        match (self.requested, self.rejected, &self.result) {
            (Some(o), _, _) => o,
            (None, Some(_), _) => RunOutcome::LimitHit,
            (None, None, Some(_)) => RunOutcome::Finished,
            (None, None, None) => RunOutcome::Crashed,
        }
    }
}

async fn drive(
    mut child: Child,
    prompt: String,
    mut run_file: File,
    events: mpsc::Sender<AgentEvent>,
    mut control: mpsc::Receiver<AgentControl>,
) {
    let pid = child.id();
    let mut stdin = child.stdin.take();
    let mut out = BufReader::new(child.stdout.take().expect("stdout is piped"));
    let stderr = child.stderr.take().map(|e| {
        let events = events.clone();
        tokio::spawn(async move {
            let mut lines = BufReader::new(e).lines();
            while let Ok(Some(l)) = lines.next_line().await {
                let _ = events.send(AgentEvent::Output(format!("stderr: {l}"))).await;
            }
        })
    });
    send_line(&mut stdin, &user_line(&prompt)).await;

    let mut p = Progress::default();
    let mut buf = Vec::new();
    let mut control_open = true;
    let mut kill_at: Option<Instant> = None;
    let mut drain_until: Option<Instant> = None;
    let mut status = None;
    loop {
        tokio::select! {
            // read_until keeps partial input in `buf` if another branch wins.
            n = out.read_until(b'\n', &mut buf) => {
                if !matches!(n, Ok(n) if n > 0) {
                    break;
                }
                if !buf.ends_with(b"\n") {
                    buf.push(b'\n');
                }
                let _ = run_file.write_all(&buf).await;
                let line = String::from_utf8_lossy(&buf).into_owned();
                buf.clear();
                for item in parse_line(line.trim_end(), now()) {
                    handle_item(item, &mut p, &mut stdin, pid, &events).await;
                }
            }
            c = control.recv(), if control_open => match c {
                None => control_open = false,
                Some(AgentControl::Message(text)) => {
                    if !send_line(&mut stdin, &user_line(&text)).await {
                        let _ = events.send(AgentEvent::Output(MESSAGE_NOT_DELIVERED.to_string())).await;
                    }
                }
                Some(AgentControl::Interrupt) => {
                    if p.result.is_none() {
                        p.requested.get_or_insert(RunOutcome::Paused);
                    }
                    send_line(&mut stdin, &interrupt_line()).await;
                }
                Some(AgentControl::Stop) => {
                    if p.result.is_none() {
                        p.requested = Some(RunOutcome::Stopped);
                    }
                    // Once reaped, the pid may belong to someone else.
                    if status.is_none() {
                        signal_group(pid, libc::SIGTERM);
                        kill_at.get_or_insert(Instant::now() + STOP_GRACE);
                    }
                }
            },
            _ = sleep_until(or_never(kill_at)), if kill_at.is_some() && status.is_none() => {
                signal_group(pid, libc::SIGKILL);
                kill_at = None;
            }
            s = child.wait(), if status.is_none() => {
                status = Some(s);
                drain_until = Some(Instant::now() + DRAIN_AFTER_EXIT);
            }
            _ = sleep_until(or_never(drain_until)), if drain_until.is_some() => break,
        }
    }
    drop(stdin);
    let status = match status {
        Some(s) => s,
        None => loop {
            tokio::select! {
                s = child.wait() => break s,
                _ = sleep_until(or_never(kill_at)), if kill_at.is_some() => {
                    signal_group(pid, libc::SIGKILL);
                    kill_at = None;
                }
            }
        },
    };
    if let Some(mut h) = stderr {
        if tokio::time::timeout(Duration::from_secs(1), &mut h).await.is_err() {
            h.abort();
        }
    }
    let outcome = p.outcome();
    if outcome == RunOutcome::Crashed {
        let msg = match status {
            Ok(s) => format!("claude exited without a result ({s})"),
            Err(e) => format!("claude exited without a result ({e})"),
        };
        let _ = events.send(AgentEvent::Output(msg)).await;
    }
    let _ = events
        .send(AgentEvent::Finished {
            final_message: p.result.flatten(),
            outcome,
            limit_resets_at: p.rejected,
        })
        .await;
}

async fn handle_item(
    item: StreamItem,
    p: &mut Progress,
    stdin: &mut Option<ChildStdin>,
    pid: Option<u32>,
    events: &mpsc::Sender<AgentEvent>,
) {
    let event = match item {
        StreamItem::Init { session_id } => {
            if p.attached {
                return;
            }
            p.attached = true;
            AgentEvent::Attached { native_session: session_id, pid }
        }
        StreamItem::Text(t) => AgentEvent::Output(t),
        StreamItem::ToolUse { name, summary } => AgentEvent::Output(format!("[{name}] {summary}")),
        StreamItem::Reading(r) => AgentEvent::Reading(r),
        StreamItem::Rejected { resets_at } => {
            p.rejected = Some(resets_at);
            return;
        }
        StreamItem::Result { text, is_error: _, usage, duration_ms } => {
            // The turn is over: closing stdin lets the process exit.
            *stdin = None;
            p.result = Some(text);
            AgentEvent::Usage {
                input_tokens: usage.0,
                output_tokens: usage.1,
                cache_read_tokens: usage.2,
                duration_ms,
            }
        }
    };
    let _ = events.send(event).await;
}

const PROBE_ARGS: [&str; 14] = [
    "-p",
    "ok",
    "--model",
    "claude-haiku-5-5",
    "--effort",
    "low",
    "--output-format",
    "stream-json",
    "--verbose",
    "--strict-mcp-config",
    "--tools",
    "",
    "--max-turns",
    "1",
];

/// One tiny Haiku call with no tools, for its usage reading.
pub async fn probe(bin: &Path) -> anyhow::Result<Reading> {
    let mut child = Command::new(bin)
        .args(PROBE_ARGS)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true)
        .spawn()
        .with_context(|| format!("starting {}", bin.display()))?;
    let mut lines = BufReader::new(child.stdout.take().expect("stdout is piped")).lines();
    let mut last_result = None;
    let read = async {
        while let Some(line) = lines.next_line().await? {
            for item in parse_line(&line, now()) {
                match item {
                    StreamItem::Reading(r) => return Ok(Some(r)),
                    StreamItem::Result { text, .. } => last_result = text,
                    _ => {}
                }
            }
        }
        Ok::<_, std::io::Error>(None)
    };
    match tokio::time::timeout(PROBE_TIMEOUT, read).await {
        Err(_) => anyhow::bail!("usage probe timed out"),
        Ok(Err(e)) => Err(e).context("reading the usage probe's output"),
        Ok(Ok(Some(r))) => Ok(r),
        Ok(Ok(None)) => {
            let status = child.wait().await?;
            let said = last_result.map(|t| format!(": {t}")).unwrap_or_default();
            anyhow::bail!("usage probe gave no reading ({status}){said}")
        }
    }
}

#[cfg(test)]
mod tests {
    use std::os::unix::fs::PermissionsExt;
    use std::time::{Duration, Instant};

    use regie_core::{Effort, Millis, Purpose, Role, RunId, RunOutcome};

    use super::*;
    use crate::agents::{AgentControl, AgentEvent};

    fn fixture(name: &str) -> String {
        format!("{}/tests/fixtures/claude/{name}", env!("CARGO_MANIFEST_DIR"))
    }

    struct Env {
        dir: tempfile::TempDir,
    }

    impl Env {
        fn new() -> Env {
            Env { dir: tempfile::tempdir().unwrap() }
        }

        fn path(&self, name: &str) -> PathBuf {
            self.dir.path().join(name)
        }

        /// A `claude` stand-in: a shell script run in the temp folder.
        fn cli(&self, body: &str) -> ClaudeCli {
            let bin = self.path("claude.sh");
            std::fs::write(&bin, format!("#!/bin/sh\ncd '{}'\n{body}\n", self.dir.path().display())).unwrap();
            std::fs::set_permissions(&bin, std::fs::Permissions::from_mode(0o755)).unwrap();
            ClaudeCli { bin, runs_dir: self.path("runs") }
        }

        fn spec(&self) -> RunSpec {
            RunSpec {
                run: RunId(3),
                role: Role::Builder,
                purpose: Purpose::Initial,
                model: "claude-opus-5-5".into(),
                effort: Effort::Medium,
                cwd: self.dir.path().to_path_buf(),
                prompt: "Do the \"thing\".\nThen stop.".into(),
                resume: None,
                env: vec![("REGIE_TASK".into(), "T7".into())],
            }
        }
    }

    /// Reads events until `Finished`, sending each control after `Attached`.
    async fn run(cli: &ClaudeCli, spec: RunSpec, controls: Vec<AgentControl>) -> Vec<AgentEvent> {
        let mut h = cli.start(spec).await.unwrap();
        let mut controls = Some(controls);
        let mut events = Vec::new();
        let deadline = tokio::time::Instant::now() + Duration::from_secs(15);
        loop {
            let ev = tokio::time::timeout_at(deadline, h.events.recv())
                .await
                .unwrap_or_else(|_| panic!("timed out; events so far: {events:?}"))
                .unwrap_or_else(|| panic!("channel closed before Finished: {events:?}"));
            if matches!(ev, AgentEvent::Attached { .. }) {
                for c in controls.take().unwrap_or_default() {
                    h.control.send(c).await.unwrap();
                }
            }
            let done = matches!(ev, AgentEvent::Finished { .. });
            events.push(ev);
            if done {
                assert!(h.events.recv().await.is_none(), "events after Finished");
                return events;
            }
        }
    }

    fn finished(events: &[AgentEvent]) -> (Option<String>, RunOutcome, Option<Millis>) {
        match events.last() {
            Some(AgentEvent::Finished { final_message, outcome, limit_resets_at }) => {
                (final_message.clone(), *outcome, *limit_resets_at)
            }
            other => panic!("expected Finished last, got {other:?}"),
        }
    }

    /// Answers control requests and user lines mentioning KIWI with a result.
    const RESPONDER: &str = r#"while IFS= read -r l; do
  case "$l" in
    *control_request*) printf '%s\n' "$l" > control.txt
      echo '{"type":"result","subtype":"error_during_execution","is_error":true,"duration_ms":5,"usage":{"input_tokens":1,"output_tokens":1,"cache_read_input_tokens":0}}' ;;
    *KIWI*) printf '%s\n' "$l" > message.txt
      echo '{"type":"result","subtype":"success","is_error":false,"result":"KIWI","duration_ms":5,"usage":{"input_tokens":1,"output_tokens":1,"cache_read_input_tokens":0}}' ;;
  esac
done"#;

    #[tokio::test]
    async fn exit_without_result_is_crash() {
        let env = Env::new();
        let cli = env.cli(&format!("cat '{}'\nexit 1", fixture("error_no_result.jsonl")));
        let events = run(&cli, env.spec(), vec![]).await;
        assert!(matches!(
            &events[0],
            AgentEvent::Attached { native_session, pid: Some(_) } if native_session == "4f6c2f8e-1d2a-4b7c-9e0a-6a1b2c3d4e5f"
        ));
        assert!(events.iter().any(|e| matches!(e, AgentEvent::Output(o) if o.contains("cargo test --workspace"))));
        assert_eq!(finished(&events), (None, RunOutcome::Crashed, None));
    }

    #[tokio::test]
    async fn result_closes_stdin_and_finishes() {
        let env = Env::new();
        let cli = env.cli(&format!("cat '{}'\ncat > /dev/null", fixture("basic.jsonl")));
        let events = run(&cli, env.spec(), vec![]).await;
        assert_eq!(finished(&events), (Some("ok".into()), RunOutcome::Finished, None));
        assert!(events.contains(&AgentEvent::Usage {
            input_tokens: 2,
            output_tokens: 116,
            cache_read_tokens: 25185,
            duration_ms: 2855
        }));
        assert!(events.iter().any(|e| matches!(e, AgentEvent::Reading(r) if r.seven_day.used == 0.79)));
        assert!(events.contains(&AgentEvent::Output("ok".into())));
        // Every stdout line lands in the run file, raw.
        let run_file = std::fs::read_to_string(env.path("runs/3.jsonl")).unwrap();
        assert_eq!(run_file, std::fs::read_to_string(fixture("basic.jsonl")).unwrap());
    }

    #[tokio::test]
    async fn arguments_prompt_and_env() {
        let env = Env::new();
        let body = format!(
            "printf '%s\\n' \"$@\" > args.txt\nprintf '%s' \"$REGIE_TASK\" > env.txt\nIFS= read -r l\nprintf '%s\\n' \"$l\" > stdin.txt\ncat '{}'\ncat > /dev/null",
            fixture("basic.jsonl")
        );
        let cli = env.cli(&body);
        run(&cli, env.spec(), vec![]).await;
        let args = std::fs::read_to_string(env.path("args.txt")).unwrap();
        let args: Vec<&str> = args.lines().collect();
        assert_eq!(
            args[..12],
            [
                "-p",
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
                "--verbose",
                "--permission-mode",
                "auto",
                "--model",
                "claude-opus-5-5",
                "--effort",
                "medium"
            ]
        );
        assert_eq!(args[12], "--session-id");
        assert_eq!(args[13].len(), 36, "a uuid: {}", args[13]);
        assert_eq!(args.len(), 14);
        assert_eq!(std::fs::read_to_string(env.path("env.txt")).unwrap(), "T7");
        let line: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(env.path("stdin.txt")).unwrap()).unwrap();
        assert_eq!(
            line,
            serde_json::json!({"type":"user","message":{"role":"user","content":"Do the \"thing\".\nThen stop."}})
        );

        let mut spec = env.spec();
        spec.resume = Some("abc-123".into());
        spec.effort = Effort::Xhigh;
        run(&cli, spec, vec![]).await;
        let args = std::fs::read_to_string(env.path("args.txt")).unwrap();
        let args: Vec<&str> = args.lines().collect();
        assert_eq!(args[10..], ["--effort", "xhigh", "--resume", "abc-123"]);
    }

    #[tokio::test]
    async fn interrupt_sends_control_request_and_pauses() {
        let env = Env::new();
        let cli = env.cli(&format!("cat '{}'\n{RESPONDER}", fixture("error_no_result.jsonl")));
        let events = run(&cli, env.spec(), vec![AgentControl::Interrupt]).await;
        assert_eq!(finished(&events).1, RunOutcome::Paused);
        let line: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(env.path("control.txt")).unwrap()).unwrap();
        assert_eq!(line["type"], "control_request");
        assert_eq!(line["request"], serde_json::json!({"subtype":"interrupt"}));
        assert_eq!(line["request_id"].as_str().unwrap().len(), 36);
    }

    #[tokio::test]
    async fn message_writes_a_user_line() {
        let env = Env::new();
        let cli = env.cli(&format!("cat '{}'\n{RESPONDER}", fixture("error_no_result.jsonl")));
        let events = run(&cli, env.spec(), vec![AgentControl::Message("Also say KIWI.".into())]).await;
        assert_eq!(finished(&events), (Some("KIWI".into()), RunOutcome::Finished, None));
        let line: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(env.path("message.txt")).unwrap()).unwrap();
        assert_eq!(line, serde_json::json!({"type":"user","message":{"role":"user","content":"Also say KIWI."}}));
    }

    #[tokio::test]
    async fn stop_terminates_the_process() {
        let env = Env::new();
        let cli = env.cli(&format!("cat '{}'\nsleep 30", fixture("error_no_result.jsonl")));
        let t = Instant::now();
        let events = run(&cli, env.spec(), vec![AgentControl::Stop]).await;
        assert_eq!(finished(&events).1, RunOutcome::Stopped);
        assert!(t.elapsed() < Duration::from_secs(4), "took {:?}", t.elapsed());
    }

    #[tokio::test]
    async fn stop_kills_a_process_that_ignores_sigterm() {
        let env = Env::new();
        let cli = env.cli(&format!("trap '' TERM\ncat '{}'\nwhile :; do sleep 1; done", fixture("error_no_result.jsonl")));
        let t = Instant::now();
        let events = run(&cli, env.spec(), vec![AgentControl::Stop]).await;
        assert_eq!(finished(&events).1, RunOutcome::Stopped);
        assert!(t.elapsed() >= Duration::from_secs(5), "killed too early: {:?}", t.elapsed());
    }

    #[tokio::test]
    async fn rejected_limit_is_limit_hit() {
        let env = Env::new();
        let cli = env.cli(&format!("cat '{}'\ncat > /dev/null", fixture("limit_rejected.jsonl")));
        let events = run(&cli, env.spec(), vec![]).await;
        let (_, outcome, resets) = finished(&events);
        assert_eq!((outcome, resets), (RunOutcome::LimitHit, Some(Millis(1_795_129_200_000))));
    }

    #[tokio::test]
    async fn probe_returns_the_first_reading() {
        let env = Env::new();
        let cli = env.cli(&format!("printf '%s\\n' \"$@\" > args.txt\ncat '{}'", fixture("basic.jsonl")));
        let r = probe(&cli.bin).await.unwrap();
        assert_eq!((r.five_hour.used, r.seven_day.used), (0.12, 0.79));
        assert_eq!(r.seven_day.resets_at, Millis(1_795_273_200_000));
        let args = std::fs::read_to_string(env.path("args.txt")).unwrap();
        assert_eq!(
            args.lines().collect::<Vec<_>>(),
            [
                "-p",
                "ok",
                "--model",
                "claude-haiku-5-5",
                "--effort",
                "low",
                "--output-format",
                "stream-json",
                "--verbose",
                "--strict-mcp-config",
                "--tools",
                "",
                "--max-turns",
                "1"
            ]
        );

        let cli = env.cli(&format!("cat '{}'", fixture("error_no_result.jsonl")));
        assert!(probe(&cli.bin).await.is_err());
    }
}
