//! Live checks against the real `claude` CLI on Haiku, in a throwaway repo.
//! Each costs a few small calls. Run with:
//! `cargo test -p regie --test live_claude -- --ignored --nocapture`
mod support;

use std::path::PathBuf;
use std::time::{Duration, Instant};

use regie::agents::{AgentControl, AgentEvent, AgentRunner, ClaudeCli, RunHandle, RunSpec};
use regie_core::{Effort, Purpose, Role, RunId, RunOutcome};
use support::repo::TempRepo;

struct Live {
    repo: TempRepo,
    cli: ClaudeCli,
    _runs: tempfile::TempDir,
}

impl Live {
    fn new() -> Live {
        let runs = tempfile::tempdir().unwrap();
        let cli = ClaudeCli { bin: PathBuf::from("claude"), runs_dir: runs.path().to_path_buf() };
        Live { repo: TempRepo::new(), cli, _runs: runs }
    }

    fn spec(&self, run: i64, prompt: &str) -> RunSpec {
        RunSpec {
            run: RunId(run),
            role: Role::Builder,
            purpose: Purpose::Initial,
            model: "claude-haiku-5-5".into(),
            effort: Effort::Low,
            cwd: self.repo.work.clone(),
            prompt: prompt.into(),
            resume: None,
            env: vec![],
        }
    }
}

/// Collects events until `Finished`; `on` may send controls as events arrive.
async fn collect(h: &mut RunHandle, mut on: impl FnMut(&AgentEvent) -> Option<AgentControl>) -> Vec<AgentEvent> {
    let mut events = Vec::new();
    let deadline = tokio::time::Instant::now() + Duration::from_secs(240);
    loop {
        let ev = tokio::time::timeout_at(deadline, h.events.recv())
            .await
            .unwrap_or_else(|_| panic!("timed out: {events:#?}"))
            .unwrap_or_else(|| panic!("closed before Finished: {events:#?}"));
        println!("  event: {}", short(&ev));
        if let Some(c) = on(&ev) {
            println!("  -> sending {c:?}");
            h.control.send(c).await.unwrap();
        }
        let done = matches!(ev, AgentEvent::Finished { .. });
        events.push(ev);
        if done {
            return events;
        }
    }
}

fn short(e: &AgentEvent) -> String {
    let s = format!("{e:?}");
    if s.chars().count() > 200 { format!("{}…", s.chars().take(200).collect::<String>()) } else { s }
}

fn is_tool_use(e: &AgentEvent) -> bool {
    matches!(e, AgentEvent::Output(o) if o.starts_with("[Bash]"))
}

fn finished(events: &[AgentEvent]) -> (Option<String>, RunOutcome) {
    match events.last() {
        Some(AgentEvent::Finished { final_message, outcome, .. }) => (final_message.clone(), *outcome),
        other => panic!("expected Finished, got {other:?}"),
    }
}

fn session(events: &[AgentEvent]) -> String {
    events
        .iter()
        .find_map(|e| match e {
            AgentEvent::Attached { native_session, .. } => Some(native_session.clone()),
            _ => None,
        })
        .expect("an Attached event")
}

const SLEEP_PROMPT: &str = "Run the shell command `sleep 30` with the Bash tool (in the foreground, not in the background), then reply with the word DONE.";

#[tokio::test]
#[ignore]
async fn live_final_message_and_reading() {
    let live = Live::new();
    let mut h = live.cli.start(live.spec(1, "Reply with the word ok.")).await.unwrap();
    let events = collect(&mut h, |_| None).await;
    let (msg, outcome) = finished(&events);
    assert_eq!(outcome, RunOutcome::Finished);
    assert!(msg.as_deref().is_some_and(|m| m.to_lowercase().contains("ok")), "{msg:?}");
    assert!(events.iter().any(|e| matches!(e, AgentEvent::Reading(_))), "no reading");
    assert!(events.iter().any(|e| matches!(e, AgentEvent::Usage { .. })), "no usage");
    let lines = std::fs::read_to_string(live.cli.runs_dir.join("1.jsonl")).unwrap().lines().count();
    println!("final message {msg:?}; run file has {lines} lines");
}

#[tokio::test]
#[ignore]
async fn live_resume_after_kill() {
    let live = Live::new();
    let prompt = format!("Remember this code word: PELICAN-42. Do not write it to any file. {SLEEP_PROMPT}");
    let mut h = live.cli.start(live.spec(1, &prompt)).await.unwrap();
    let mut sent = false;
    let events = collect(&mut h, |e| {
        (is_tool_use(e) && !std::mem::replace(&mut sent, true)).then_some(AgentControl::Stop)
    })
    .await;
    assert_eq!(finished(&events).1, RunOutcome::Stopped);
    let native = session(&events);

    let mut spec = live.spec(2, "What was the code word I asked you to remember? Reply with just the word.");
    spec.resume = Some(native.clone());
    let mut h = live.cli.start(spec).await.unwrap();
    let events = collect(&mut h, |_| None).await;
    let (msg, outcome) = finished(&events);
    assert_eq!(outcome, RunOutcome::Finished);
    assert!(msg.as_deref().is_some_and(|m| m.contains("PELICAN-42")), "{msg:?}");
    println!("resumed {native} as {}: {msg:?}", session(&events));
}

#[tokio::test]
#[ignore]
async fn live_interrupt() {
    let live = Live::new();
    let mut h = live.cli.start(live.spec(1, SLEEP_PROMPT)).await.unwrap();
    let mut at = None;
    let events = collect(&mut h, |e| {
        (is_tool_use(e) && at.is_none()).then(|| {
            at = Some(Instant::now());
            AgentControl::Interrupt
        })
    })
    .await;
    let took = at.expect("a tool use").elapsed();
    println!("interrupt to Finished: {took:?}");
    assert_eq!(finished(&events).1, RunOutcome::Paused);
    assert!(took < Duration::from_secs(15), "took {took:?}");
}

#[tokio::test]
#[ignore]
async fn live_message_mid_turn() {
    let live = Live::new();
    let prompt = SLEEP_PROMPT.replace("sleep 30", "sleep 15");
    let mut h = live.cli.start(live.spec(1, &prompt)).await.unwrap();
    let mut sent = false;
    let events = collect(&mut h, |e| {
        (is_tool_use(e) && !std::mem::replace(&mut sent, true))
            .then(|| AgentControl::Message("Also reply with the word KIWI.".into()))
    })
    .await;
    let (msg, outcome) = finished(&events);
    let results = events.iter().filter(|e| matches!(e, AgentEvent::Usage { .. })).count();
    let kiwi = msg.as_deref().is_some_and(|m| m.contains("KIWI"));
    println!(
        "message delivery: {} ({results} result(s); final {msg:?})",
        if results == 1 && kiwi { "mid-turn, folded into the running turn" } else { "follow-up turn or lost" }
    );
    assert_eq!(outcome, RunOutcome::Finished);
    assert!(kiwi, "{msg:?}");
}
