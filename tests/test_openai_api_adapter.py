import json
from pathlib import Path

import pytest

from regie.agents import openai_api_runner
from regie.agents.base import AgentRequest, get_adapter
from regie.models import Binding, Budgets, TokenPolicy


def _request(tmp_path, *, sandbox="workspace-write"):
    return AgentRequest(
        prompt="inspect it", instructions="You are a reviewer.", cwd=tmp_path,
        binding=Binding(cli="openai-api", model="gpt-5.6-terra", auth="api"),
        budgets=Budgets(turns=4),
        token_policy=TokenPolicy(
            effort="low", tools=["list", "read", "search", "patch"], sandbox=sandbox),
        output_schema={"type": "object", "additionalProperties": False,
                       "properties": {"findings": {"type": "array"}},
                       "required": ["findings"]})


def test_adapter_serializes_request_outside_repo(tmp_path):
    command = get_adapter("openai-api").build_command(_request(tmp_path))
    assert command[1:3] == ["-m", "regie.agents.openai_api_runner"]
    request_path = Path(command[-1])
    payload = json.loads(request_path.read_text())
    assert payload["instructions"] == "You are a reviewer."
    assert payload["token_policy"]["effort"] == "low"
    assert not request_path.is_relative_to(tmp_path)
    request_path.unlink()


def test_adapter_parses_runner_result(tmp_path):
    stdout = "heartbeat\n" + json.dumps({
        "regie_result": True, "outcome": "done", "text": '{"findings":[]}',
        "structured": {"findings": []}, "turns": 2,
        "metrics": {"new_input_tokens": 10, "output_tokens": 3}})
    result = get_adapter("openai-api").parse(stdout, 0)
    assert result.outcome == "done" and result.structured == {"findings": []}
    assert result.metrics.new_input_tokens == 10


def test_runner_tool_loop_and_usage(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("A = 1\n")
    responses = iter([
        {"id": "r1", "output": [{"type": "function_call", "name": "read_file",
                                    "call_id": "c1",
                                    "arguments": json.dumps({"path": "a.py",
                                                             "start_line": 1,
                                                             "end_line": 20})}],
         "usage": {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 80},
                   "output_tokens": 10}},
        {"id": "r2", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": '{"findings": []}'}]}],
         "usage": {"input_tokens": 50, "input_tokens_details": {"cached_tokens": 40},
                   "output_tokens": 5,
                   "output_tokens_details": {"reasoning_tokens": 2}}},
    ])
    sent = []

    def fake_post(payload, _timeout):
        sent.append(payload)
        return next(responses)

    monkeypatch.setattr(openai_api_runner, "_post", fake_post)
    cfg = _request(tmp_path).model_dump(mode="json")
    result = openai_api_runner.run(cfg)

    assert result["outcome"] == "done" and result["structured"] == {"findings": []}
    assert result["metrics"]["cached_input_tokens"] == 120
    assert result["metrics"]["new_input_tokens"] == 30
    assert result["metrics"]["tool_output_bytes"] > 0
    assert sent[1]["previous_response_id"] == "r1"
    assert sent[1]["input"][0]["type"] == "function_call_output"


def test_read_only_api_policy_removes_patch_tool(tmp_path):
    cfg = _request(tmp_path, sandbox="read-only").model_dump(mode="json")
    names = {tool["name"] for tool in openai_api_runner._tools(cfg)}
    assert "apply_patch" not in names


def test_api_tool_rejects_path_escape(tmp_path):
    with pytest.raises(ValueError, match="escapes"):
        openai_api_runner._inside(tmp_path.resolve(), "../secret")

