import json
import time

from regie.agents.base import AgentRequest, classify_agent_failure
from regie.dispatch import _codex_line_is_progress, run_agent
from regie.models import Binding, Budgets
from regie.rundir import RunDir


def _req(cwd, budgets=None) -> AgentRequest:
    return AgentRequest(prompt="p", cwd=cwd, binding=Binding(cli="fake", model="m1"),
                        budgets=budgets or Budgets())


def test_intent_written_before_result_and_event_after(regie_home, tmp_path):
    rd = RunDir.create(regie_home, "r1")
    (tmp_path / ".fake_agent.json").write_text(json.dumps(
        {"result": {"outcome": "done", "text": "ok", "turns": 1}}))
    result = run_agent(rd, "T1", "build", 1, _req(tmp_path))
    assert result.outcome == "done"
    intents = rd.read_intents()
    assert intents[0]["task"] == "T1" and intents[0]["attempt"] == 1
    assert (rd.path / "tasks" / "T1" / "attempt-1.out").exists()
    event = json.loads((rd.path / "events.jsonl").read_text().splitlines()[-1])
    assert event["binding"] == {"cli": "fake", "model": "m1", "auth": "subscription"}
    assert event["duration_seconds"] >= 0


def test_wall_budget_kills_hung_agent(regie_home, tmp_path):
    rd = RunDir.create(regie_home, "r1")
    (tmp_path / ".fake_agent.json").write_text(json.dumps(
        {"sleep": 60, "result": {"outcome": "done"}}))
    budgets = Budgets(wall_minutes=1, stall_minutes=1)
    # shrink budgets to seconds for the test via the seconds override hook
    start = time.monotonic()
    result = run_agent(rd, "T1", "build", 1, _req(tmp_path, budgets),
                       _wall_seconds=2, _stall_seconds=2)
    assert result.outcome == "error" and "killed" in result.text
    assert time.monotonic() - start < 30


def test_wall_budget_uses_realtime_deadline_across_sleep(
        regie_home, tmp_path, monkeypatch):
    rd = RunDir.create(regie_home, "r1")
    (tmp_path / ".fake_agent.json").write_text(json.dumps(
        {"sleep": 60, "result": {"outcome": "done"}}))
    ticks = iter((100.0, 100.0, 103.0, 103.1))
    monkeypatch.setattr("regie.dispatch.time.time", lambda: next(ticks, 103.1))

    started = time.monotonic()
    result = run_agent(
        rd, "T1", "build", 1, _req(tmp_path),
        _wall_seconds=2, _stall_seconds=60,
    )

    assert result.failure_kind == "wall"
    assert time.monotonic() - started < 1
    event = json.loads((rd.path / "events.jsonl").read_text().splitlines()[-1])
    assert event["failure_kind"] == "wall"
    assert event["duration_seconds"] == 3.1


def test_codex_reconnect_chatter_is_not_progress():
    assert not _codex_line_is_progress(json.dumps({
        "type": "error",
        "message": "Reconnecting: stream disconnected before completion",
    }))
    assert not _codex_line_is_progress(json.dumps({
        "type": "item.completed",
        "item": {"type": "error", "message": "Falling back to HTTPS"},
    }))
    assert _codex_line_is_progress(json.dumps({
        "type": "item.completed",
        "item": {"type": "command_execution", "command": "git diff"},
    }))


def test_network_stream_failures_are_infrastructure():
    assert classify_agent_failure(
        "stream disconnected before completion: Connection reset by peer"
    ) == "infrastructure"
