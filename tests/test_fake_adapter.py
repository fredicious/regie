import json
import subprocess
from pathlib import Path

from regie.agents.base import AgentRequest, get_adapter
from regie.models import Binding, Budgets


def _req(cwd: Path) -> AgentRequest:
    return AgentRequest(prompt="do it", cwd=cwd,
                        binding=Binding(cli="fake", model="m1"), budgets=Budgets())


def _run(cwd: Path):
    adapter = get_adapter("fake")
    proc = subprocess.run(adapter.build_command(_req(cwd)), cwd=cwd,
                          capture_output=True, text=True, check=False)
    return adapter.parse(proc.stdout, proc.returncode)


def test_fake_agent_returns_scripted_result_and_writes_files(tmp_path):
    (tmp_path / ".fake_agent.json").write_text(json.dumps({
        "result": {"outcome": "done", "text": "built it", "turns": 2},
        "writes": {"src/new.py": "x = 1\n"},
    }))
    result = _run(tmp_path)
    assert result.outcome == "done" and result.turns == 2
    assert (tmp_path / "src" / "new.py").read_text() == "x = 1\n"


def test_fake_agent_blocked_outcome(tmp_path):
    (tmp_path / ".fake_agent.json").write_text(json.dumps({
        "result": {"outcome": "blocked", "blocked_question": "which cache?"}}))
    assert _run(tmp_path).blocked_question == "which cache?"


def test_unparseable_output_is_error(tmp_path):
    adapter = get_adapter("fake")
    assert adapter.parse("garbage", 0).outcome == "error"


def test_fake_agent_queue_consumes_in_order(tmp_path):
    q = tmp_path / ".fake_agent_queue"
    q.mkdir()
    (q / "0.json").write_text(json.dumps({"result": {"outcome": "done", "text": "first"}}))
    (q / "1.json").write_text(json.dumps({"result": {"outcome": "done", "text": "second"}}))
    assert _run(tmp_path).text == "first"
    assert _run(tmp_path).text == "second"
