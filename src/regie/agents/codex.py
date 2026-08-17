"""Codex exec adapter. NOTE: codex exec --json exposes no usage/quota telemetry
(rate_limits always null in exec mode) — usage stays empty; quota is detected
only via error events."""
from __future__ import annotations

import json
import re
import tempfile

from regie.agents.base import AgentRequest, AgentResult, register
from regie.models import UsageMetrics
from regie.quota import quota_metadata

_QUOTA = re.compile(
    r"(?:usage|rate|weekly|(?:5|five)[ -]?hour).?limit|quota|"
    r"limit\s+reached.*reset",
    re.IGNORECASE,
)


class CodexAdapter:
    def build_command(self, req: AgentRequest) -> list[str]:
        cmd = ["codex", "exec", "--json", "-m", req.binding.model,
               "--sandbox", req.token_policy.sandbox, "--skip-git-repo-check",
               "-c", f'model_reasoning_effort="{req.token_policy.effort}"']
        if req.output_schema is not None:
            # Outside req.cwd deliberately -- see claude.py's build_command
            # for why a worktree-local schema file is a scratch-leak hazard.
            fd, path = tempfile.mkstemp(suffix=".regie_schema.json")
            with open(fd, "w") as f:
                f.write(json.dumps(req.output_schema))
            cmd += ["--output-schema", path]
        prompt = req.instructions + "\n\n" + req.prompt if req.instructions else req.prompt
        return cmd + [prompt]

    def parse(self, stdout: str, exit_code: int) -> AgentResult:
        text, turns, error_msg, error_payload, usage, tool_output_bytes = (
            None, 0, None, None, {}, 0)
        for line in stdout.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            if ev.get("type") == "agent_message":
                text, turns = str(ev.get("text", "")), turns + 1
            elif (ev.get("type") == "item.completed"
                  and isinstance(ev.get("item"), dict)
                  and ev["item"].get("type") == "agent_message"):
                text, turns = str(ev["item"].get("text", "")), turns + 1
            elif ev.get("type") == "turn.completed" and isinstance(ev.get("usage"), dict):
                # Real codex 0.146 DOES report usage on turn.completed, contrary
                # to the Plan B doc assumption of "no exec-mode telemetry"
                # (verified live 2026-07-31).
                usage = ev["usage"]
            elif ev.get("type") == "item.completed" and isinstance(ev.get("item"), dict):
                item = ev["item"]
                if item.get("type") in ("command_execution", "mcp_tool_call"):
                    payload = item.get("aggregated_output", item.get("output", ""))
                    tool_output_bytes += len(str(payload).encode())
            elif ev.get("type") in ("error", "turn.failed"):
                # A failed turn carries a nested JSON error string; an `error`
                # event carries the message directly. Prefer the first error seen.
                msg = ev.get("message")
                if msg is None and isinstance(ev.get("error"), dict):
                    msg = ev["error"].get("message")
                if error_msg is None:
                    error_msg = str(msg or "")
                    error_payload = ev
        total_input = int(usage.get("input_tokens") or 0)
        cached = int(usage.get("cached_input_tokens") or 0)
        metrics = UsageMetrics(
            new_input_tokens=max(0, total_input - cached),
            cached_input_tokens=cached,
            cache_write_input_tokens=int(usage.get("cache_write_input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            reasoning_output_tokens=int(usage.get("reasoning_output_tokens") or 0),
            tool_output_bytes=tool_output_bytes,
        )
        telemetry = {"turns": turns, "usage": usage, "metrics": metrics}
        if error_msg is not None:
            outcome = "quota" if _QUOTA.search(error_msg) else "error"
            if outcome == "quota":
                quota = quota_metadata(error_msg, error_payload)
                return AgentResult(
                    outcome="quota", text=error_msg[-2000:], **telemetry,
                    quota_kind=quota.kind, quota_scope=quota.scope,
                    quota_reset_at=quota.reset_at, quota_reason=quota.reason,
                )
            return AgentResult(outcome="error", text=error_msg[-2000:], **telemetry)
        if text is None:
            return AgentResult(outcome="error", text=stdout[-2000:], **telemetry)
        for line in text.splitlines():
            if line.strip().lower().startswith("blocked:"):
                return AgentResult(outcome="blocked", text=text, **telemetry,
                                   blocked_question=line.split(":", 1)[1].strip())
        if exit_code != 0:
            return AgentResult(outcome="error", text=text[-2000:], **telemetry)
        structured = None
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                structured = parsed
        except json.JSONDecodeError:
            pass
        return AgentResult(outcome="done", text=text, structured=structured,
                           **telemetry)


register("codex", CodexAdapter())
