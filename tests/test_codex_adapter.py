import json

from regie.agents.base import AgentRequest, get_adapter
from regie.models import Binding, Budgets


def _req(tmp_path, schema=None):
    return AgentRequest(prompt="build it", cwd=tmp_path,
                        binding=Binding(cli="codex", model="gpt-5.x"),
                        budgets=Budgets(), output_schema=schema)


def _lines(*events) -> str:
    return "\n".join(json.dumps(e) for e in events)


def test_build_command_flags(tmp_path):
    cmd = get_adapter("codex").build_command(_req(tmp_path))
    assert cmd[:3] == ["codex", "exec", "--json"]
    assert cmd[cmd.index("-m") + 1] == "gpt-5.x"
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
    assert 'model_reasoning_effort="medium"' in cmd
    assert cmd[-1] == "build it"


def test_build_command_writes_schema_file(tmp_path):
    cmd = get_adapter("codex").build_command(_req(tmp_path, schema={"type": "object"}))
    path = cmd[cmd.index("--output-schema") + 1]
    with open(path) as f:
        assert json.loads(f.read()) == {"type": "object"}
    # Must live outside the agent's cwd: a schema file inside the worktree is
    # untracked scratch a later `git add -A` could sweep into history.
    assert not path.startswith(str(tmp_path))


def test_parse_takes_last_agent_message():
    out = _lines({"type": "item.completed", "item": {"type": "agent_message", "text": "first"}},
                 {"type": "item.completed", "item": {"type": "reasoning", "text": "x"}},
                 {"type": "agent_message", "text": "final answer"})
    r = get_adapter("codex").parse(out, 0)
    assert r.outcome == "done" and r.text == "final answer" and r.turns == 2


def test_parse_quota_from_error_event():
    out = _lines({"type": "error", "message": "You've hit your usage limit"})
    assert get_adapter("codex").parse(out, 1).outcome == "quota"


def test_parse_quota_reads_structured_reset_time():
    out = _lines({"type": "turn.failed", "error": {
        "message": "weekly usage limit reached",
        "reset_at": "2026-08-21T12:00:00Z"}})
    result = get_adapter("codex").parse(out, 1)
    assert result.outcome == "quota" and result.quota_kind == "weekly"
    assert result.quota_reset_at == "2026-08-21T12:00:00+00:00"


def test_parse_exact_five_hour_limit_message():
    out = _lines({"type": "error",
                  "message": "5-hour limit reached - resets at 3:00 PM"})
    result = get_adapter("codex").parse(out, 1)
    assert result.outcome == "quota" and result.quota_kind == "session"
    assert result.quota_reset_at is not None


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


def test_parse_blocked_precedence_over_exit_code():
    out = _lines({"type": "agent_message", "text": "blocked: why?"})
    assert get_adapter("codex").parse(out, 1).outcome == "blocked"


# Real streams captured from codex-cli 0.146.0 on 2026-07-31 (ChatGPT auth).
REAL_SUCCESS = """{"type":"thread.started","thread_id":"t1"}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"pong"}}
{"type":"turn.completed","usage":{"input_tokens":13970,"cached_input_tokens":10624,"output_tokens":5}}"""

REAL_MODEL_ERROR = """{"type":"thread.started","thread_id":"t2"}
{"type":"item.completed","item":{"id":"item_0","type":"error","message":"Model metadata not found"}}
{"type":"turn.started"}
{"type":"error","message":"model not supported"}
{"type":"turn.failed","error":{"message":"model not supported"}}"""


def test_parse_real_success_stream_captures_usage():
    r = get_adapter("codex").parse(REAL_SUCCESS, 0)
    assert r.outcome == "done" and r.text == "pong" and r.turns == 1
    # Plan B assumed no usage telemetry; real 0.146 provides it on turn.completed
    assert r.usage.get("input_tokens") == 13970 and r.usage.get("output_tokens") == 5
    assert r.metrics.new_input_tokens == 3346
    assert r.metrics.cached_input_tokens == 10624


def test_parse_real_error_stream_is_error():
    r = get_adapter("codex").parse(REAL_MODEL_ERROR, 1)
    assert r.outcome == "error"


def test_parse_turn_failed_without_error_event():
    stream = ('{"type":"turn.started"}\n'
              '{"type":"turn.failed","error":{"message":"you have hit your usage limit"}}')
    assert get_adapter("codex").parse(stream, 1).outcome == "quota"
