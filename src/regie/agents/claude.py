from __future__ import annotations

import json
import re

from regie.agents.base import (
    AgentRequest,
    AgentResult,
    blocked_question_from_text,
    register,
)
from regie.models import UsageMetrics
from regie.quota import quota_metadata

_QUOTA_STATUS = {429, 529}
_QUOTA_TEXT = re.compile(
    r"(?:usage|rate|weekly|(?:5|five)[ -]?hour).?limit|quota|"
    r"limit\s+reached.*reset",
    re.IGNORECASE,
)


def _last_json_object(stdout: str) -> dict | None:
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                doc = json.loads(line)
                if isinstance(doc, dict):
                    return doc
            except json.JSONDecodeError:
                continue
    return None


class ClaudeAdapter:
    def build_command(self, req: AgentRequest) -> list[str]:
        # stream-json (with --verbose) emits an event line per step, giving
        # the dispatch stall-detector a genuine liveness signal — plain json
        # buffers ALL output until completion, so any attempt longer than the
        # stall budget was killed mid-work (dogfood finding: deaths at exactly
        # stall_minutes). The final stream line is the same result object the
        # parser already reads via _last_json_object.
        tool_names = {"list": "Glob", "read": "Read", "search": "Grep",
                      "patch": "Edit", "shell": "Bash"}
        tools = [tool_names[t] for t in req.token_policy.tools]
        permission_mode = ("plan" if req.token_policy.sandbox == "read-only"
                           else "acceptEdits")
        cmd = ["claude", "-p", req.prompt, "--output-format", "stream-json",
               "--verbose",
               "--max-turns", str(req.budgets.turns),
               "--model", req.binding.model,
               "--effort", req.token_policy.effort,
               "--tools", ",".join(tools),
               "--permission-mode", permission_mode]
        if req.instructions:
            cmd += ["--append-system-prompt", req.instructions]
        if not req.token_policy.cache_dynamic_system_sections:
            cmd.append("--exclude-dynamic-system-prompt-sections")
        if req.token_policy.sandbox == "read-only":
            cmd += ["--disallowedTools", "Edit,Write,NotebookEdit"]
        if req.output_schema is not None:
            # The real CLI takes the schema INLINE, not as a file path
            # (verified against claude 2.1.220 during the first smoke test).
            # Inline also means no scratch file can ever leak into a commit.
            cmd += ["--json-schema", json.dumps(req.output_schema)]
        return cmd

    def parse(self, stdout: str, exit_code: int) -> AgentResult:
        tool_output_bytes = 0
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "user":
                continue
            for item in ((event.get("message") or {}).get("content") or []):
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    tool_output_bytes += len(json.dumps(item.get("content", "")).encode())
        doc = _last_json_object(stdout)
        if doc is None:
            return AgentResult(outcome="error", text=stdout[-2000:])
        usage = dict(doc.get("usage") or {})
        usage["total_cost_usd"] = doc.get("total_cost_usd")
        usage["modelUsage"] = doc.get("modelUsage") or {}
        metrics = UsageMetrics(
            new_input_tokens=int(usage.get("input_tokens") or 0),
            cached_input_tokens=int(usage.get("cache_read_input_tokens") or 0),
            cache_write_input_tokens=int(
                usage.get("cache_creation_input_tokens")
                or usage.get("cache_write_input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            reasoning_output_tokens=int(usage.get("reasoning_output_tokens") or 0),
            tool_output_bytes=tool_output_bytes,
            cost_usd=float(usage.get("total_cost_usd") or 0),
        )
        telemetry = {"usage": usage, "metrics": metrics,
                     "turns": int(doc.get("num_turns") or 0)}
        result = doc.get("result", "")
        if isinstance(result, dict):
            structured = result
            text = json.dumps(result)
        else:
            text = str(result)
            structured = None
        reason = str(doc.get("terminal_reason", ""))
        if (doc.get("api_error_status") in _QUOTA_STATUS
                or _QUOTA_TEXT.search(reason)
                or (doc.get("is_error") and _QUOTA_TEXT.search(text))):
            # Claude commonly puts the reset time in terminal_reason or an
            # adjacent error field rather than result. Preserve the combined
            # evidence so the global circuit breaker can recover on schedule.
            evidence = "\n".join(part for part in (
                reason, text, json.dumps(doc.get("errors") or ""),
                json.dumps(doc.get("error") or ""),
            ) if part and part != '""')
            quota = quota_metadata(evidence, doc)
            return AgentResult(
                outcome="quota", text=evidence[-2000:], **telemetry,
                quota_kind=quota.kind, quota_scope=quota.scope,
                quota_reset_at=quota.reset_at, quota_reason=quota.reason,
            )
        blocked = blocked_question_from_text(text)
        if blocked is not None:
            return AgentResult(outcome="blocked", text=text, **telemetry,
                               blocked_question=blocked)
        if (doc.get("subtype") == "error_max_turns"
                or str(doc.get("terminal_reason")) == "max_turns"):
            # Name the death precisely: a budget exhaustion retried blind is a
            # ladder burned for nothing (dogfood finding). The text lands in
            # the retry packet's Notes via the pipeline's dispatch-death note.
            return AgentResult(
                outcome="error",
                text=("turn budget exhausted (--max-turns reached) — the next "
                      "attempt must be more economical: plan edits before "
                      "reading broadly, batch related file changes, avoid "
                      "re-reading unchanged files. " + text[-1200:]), **telemetry)
        if doc.get("is_error") or exit_code != 0:
            return AgentResult(outcome="error", text=text[-2000:] or stdout[-2000:],
                               **telemetry)
        if structured is None:
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    structured = parsed
            except (json.JSONDecodeError, TypeError):
                pass
        return AgentResult(outcome="done", text=text, structured=structured,
                           **telemetry)


register("claude", ClaudeAdapter())
