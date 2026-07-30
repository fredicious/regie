import json

from regie.agents.base import AgentRequest, get_adapter
from regie.models import Binding, Budgets


def _req(tmp_path, schema=None):
    return AgentRequest(prompt="do the task", cwd=tmp_path,
                        binding=Binding(cli="claude", model="opus"),
                        budgets=Budgets(turns=7), output_schema=schema)


def _doc(**over):
    base = {"is_error": False, "subtype": "success", "result": "done the thing",
            "num_turns": 3, "usage": {"input_tokens": 10, "output_tokens": 5},
            "modelUsage": {}, "total_cost_usd": 0.12}
    base.update(over)
    return json.dumps(base)


def test_build_command_flags(tmp_path):
    cmd = get_adapter("claude").build_command(_req(tmp_path))
    assert cmd[:3] == ["claude", "-p", "do the task"]
    assert "--verbose" in cmd  # required by stream-json in -p mode
    for flag, val in (("--output-format", "stream-json"), ("--max-turns", "7"),
                      ("--model", "opus"), ("--permission-mode", "acceptEdits")):
        assert val == cmd[cmd.index(flag) + 1]
    assert "--json-schema" not in cmd and "--bare" not in cmd


def test_build_command_passes_schema_inline(tmp_path):
    # The real CLI (verified on 2.1.220) takes the schema as inline JSON, not
    # a file path — inline also means no scratch file can leak into a commit.
    cmd = get_adapter("claude").build_command(_req(tmp_path, schema={"type": "object"}))
    arg = cmd[cmd.index("--json-schema") + 1]
    assert json.loads(arg) == {"type": "object"}


def test_parse_done_with_usage_and_noise(tmp_path):
    out = "some log noise\n" + _doc()
    r = get_adapter("claude").parse(out, 0)
    assert r.outcome == "done" and r.turns == 3
    assert r.usage["total_cost_usd"] == 0.12 and r.text == "done the thing"


def test_parse_structured_result(tmp_path):
    doc = _doc(result=json.dumps({"findings": []}))
    r = get_adapter("claude").parse(doc, 0)
    assert r.structured == {"findings": []}


def test_parse_quota_from_api_error_status():
    r = get_adapter("claude").parse(_doc(is_error=True, api_error_status=429), 0)
    assert r.outcome == "quota"


def test_parse_quota_from_terminal_reason():
    r = get_adapter("claude").parse(_doc(terminal_reason="usage_limit_reached"), 0)
    assert r.outcome == "quota"


def test_parse_blocked_line():
    r = get_adapter("claude").parse(_doc(result="analysis...\nblocked: cache per user or global?"), 0)
    assert r.outcome == "blocked" and "cache per user" in r.blocked_question


def test_parse_error_on_garbage_and_nonzero_exit():
    assert get_adapter("claude").parse("not json at all", 0).outcome == "error"
    assert get_adapter("claude").parse(_doc(), 1).outcome == "error"


def test_parse_blocked_precedence_over_error():
    r = get_adapter("claude").parse(
        _doc(is_error=True, result="analysis...\nblocked: which cache?"), 1)
    assert r.outcome == "blocked" and "which cache" in r.blocked_question


def test_parse_result_as_native_dict():
    base = {"is_error": False, "subtype": "success",
            "result": {"findings": ["a", "b"]},
            "num_turns": 2, "usage": {}, "modelUsage": {}, "total_cost_usd": 0.05}
    r = get_adapter("claude").parse(json.dumps(base), 0)
    assert r.outcome == "done" and r.structured == {"findings": ["a", "b"]}
    assert json.loads(r.text) == {"findings": ["a", "b"]}


def test_parse_quota_from_error_text():
    r = get_adapter("claude").parse(
        _doc(is_error=True, result="You have hit your usage limit"), 0)
    assert r.outcome == "quota"


def test_parse_max_turns_death_names_budget():
    """A max-turns death must tell the retry exactly what happened, not look
    like a generic error (dogfood finding: two silent budget deaths burned a
    ladder with no explanation in the retry packet)."""
    doc = _doc(is_error=True, subtype="error_max_turns",
               terminal_reason="max_turns",
               errors=["Reached maximum number of turns (40)"])
    r = get_adapter("claude").parse(doc, 0)
    assert r.outcome == "error"
    assert "turn budget" in r.text.lower()
