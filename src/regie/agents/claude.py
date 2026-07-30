from __future__ import annotations

import json
import re

from regie.agents.base import AgentRequest, AgentResult, register

_QUOTA_STATUS = {429, 529}
_QUOTA_TEXT = re.compile(r"(usage|rate).?limit", re.IGNORECASE)


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
        cmd = ["claude", "-p", req.prompt, "--output-format", "stream-json",
               "--verbose",
               "--max-turns", str(req.budgets.turns),
               "--model", req.binding.model, "--permission-mode", "acceptEdits"]
        if req.output_schema is not None:
            # The real CLI takes the schema INLINE, not as a file path
            # (verified against claude 2.1.220 during the first smoke test).
            # Inline also means no scratch file can ever leak into a commit.
            cmd += ["--json-schema", json.dumps(req.output_schema)]
        return cmd

    def parse(self, stdout: str, exit_code: int) -> AgentResult:
        doc = _last_json_object(stdout)
        if doc is None:
            return AgentResult(outcome="error", text=stdout[-2000:])
        result = doc.get("result", "")
        if isinstance(result, dict):
            structured = result
            text = json.dumps(result)
        else:
            text = str(result)
            structured = None
        reason = str(doc.get("terminal_reason", ""))
        if (doc.get("api_error_status") in _QUOTA_STATUS
                or re.search(r"quota|limit", reason, re.IGNORECASE)
                or (doc.get("is_error") and _QUOTA_TEXT.search(text))):
            return AgentResult(outcome="quota", text=text[-2000:])
        for line in text.splitlines():
            if line.strip().lower().startswith("blocked:"):
                return AgentResult(outcome="blocked", text=text,
                                   blocked_question=line.split(":", 1)[1].strip())
        if doc.get("is_error") or exit_code != 0:
            return AgentResult(outcome="error", text=text[-2000:] or stdout[-2000:])
        if structured is None:
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    structured = parsed
            except (json.JSONDecodeError, TypeError):
                pass
        usage = dict(doc.get("usage") or {})
        usage["total_cost_usd"] = doc.get("total_cost_usd")
        usage["modelUsage"] = doc.get("modelUsage") or {}
        return AgentResult(outcome="done", text=text, structured=structured,
                           usage=usage, turns=int(doc.get("num_turns") or 0))


register("claude", ClaudeAdapter())
