"""Opt-in direct OpenAI Responses API adapter.

The network/tool loop runs in a child process so it inherits Régie's existing
wall/stall supervision contract. It deliberately exposes bounded local tools,
not an unrestricted shell.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

from regie.agents.base import AgentRequest, AgentResult, register
from regie.quota import quota_metadata


class OpenAIResponsesAdapter:
    def build_command(self, req: AgentRequest) -> list[str]:
        fd, path = tempfile.mkstemp(suffix=".regie_openai_request.json")
        payload = req.model_dump(mode="json")
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle)
        return [sys.executable, "-m", "regie.agents.openai_api_runner", path]

    def parse(self, stdout: str, exit_code: int) -> AgentResult:
        for line in reversed(stdout.splitlines()):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("regie_result"):
                value.pop("regie_result", None)
                result = AgentResult(**value)
                if result.outcome == "quota" and result.quota_kind is None:
                    quota = quota_metadata(result.text, value)
                    result.quota_kind = quota.kind
                    result.quota_scope = quota.scope
                    result.quota_reset_at = quota.reset_at
                    result.quota_reason = quota.reason
                return result
        return AgentResult(outcome="error", text=stdout[-2000:] or
                           f"openai-api runner exited {exit_code}")


register("openai-api", OpenAIResponsesAdapter())
