import json

from regie.agents.base import AgentRequest, get_adapter
from regie.models import Binding, Budgets


def _req(tmp_path):
    return AgentRequest(prompt="build it", cwd=tmp_path,
                        binding=Binding(cli="codex", model="gpt-5.x"),
                        budgets=Budgets())


def _lines(*events) -> str:
    return "\n".join(json.dumps(e) for e in events)


def test_build_command_flags(tmp_path):
    cmd = get_adapter("codex").build_command(_req(tmp_path))
    assert cmd[:3] == ["codex", "exec", "--json"]
    assert cmd[cmd.index("-m") + 1] == "gpt-5.x"
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
    assert cmd[-1] == "build it"


def test_parse_takes_last_agent_message():
    out = _lines({"type": "item.completed", "item": {"type": "agent_message", "text": "first"}},
                 {"type": "item.completed", "item": {"type": "reasoning", "text": "x"}},
                 {"type": "agent_message", "text": "final answer"})
    r = get_adapter("codex").parse(out, 0)
    assert r.outcome == "done" and r.text == "final answer" and r.turns == 2


def test_parse_quota_from_error_event():
    out = _lines({"type": "error", "message": "You've hit your usage limit"})
    assert get_adapter("codex").parse(out, 1).outcome == "quota"


def test_parse_plain_error_event():
    out = _lines({"type": "error", "message": "sandbox denied"})
    assert get_adapter("codex").parse(out, 1).outcome == "error"


def test_parse_blocked_and_structured(tmp_path):
    out = _lines({"type": "agent_message", "text": "blocked: bad-test: asserts impossible"})
    r = get_adapter("codex").parse(out, 0)
    assert r.outcome == "blocked" and r.blocked_question.startswith("bad-test:")
    out2 = _lines({"type": "agent_message", "text": json.dumps({"findings": []})})
    assert get_adapter("codex").parse(out2, 0).structured == {"findings": []}


def test_parse_no_message_is_error():
    assert get_adapter("codex").parse("", 0).outcome == "error"
    assert get_adapter("codex").parse("garbage\nlines", 0).outcome == "error"
