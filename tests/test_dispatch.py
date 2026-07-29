import json
import time

from regie.agents.base import AgentRequest
from regie.dispatch import run_agent
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
