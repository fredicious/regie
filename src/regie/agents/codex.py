"""Codex exec adapter. NOTE: codex exec --json exposes no usage/quota telemetry
(rate_limits always null in exec mode) — usage stays empty; quota is detected
only via error events."""
from __future__ import annotations

import json
import re

from regie.agents.base import AgentRequest, AgentResult, register

_QUOTA = re.compile(r"(usage|rate).?limit|quota", re.IGNORECASE)


class CodexAdapter:
    def build_command(self, req: AgentRequest) -> list[str]:
        cmd = ["codex", "exec", "--json", "-m", req.binding.model,
               "--sandbox", "workspace-write", "--skip-git-repo-check"]
        if req.output_schema is not None:
            schema_path = req.cwd / ".regie_schema.json"
            schema_path.write_text(json.dumps(req.output_schema))
            cmd += ["--output-schema", str(schema_path)]
        return cmd + [req.prompt]

    def parse(self, stdout: str, exit_code: int) -> AgentResult:
        text, turns, error_msg = None, 0, None
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
            elif ev.get("type") == "error":
                error_msg = str(ev.get("message", ""))
        if error_msg is not None:
            outcome = "quota" if _QUOTA.search(error_msg) else "error"
            return AgentResult(outcome=outcome, text=error_msg[-2000:])
        if text is None:
            return AgentResult(outcome="error", text=stdout[-2000:])
        for line in text.splitlines():
            if line.strip().lower().startswith("blocked:"):
                return AgentResult(outcome="blocked", text=text, turns=turns,
                                   blocked_question=line.split(":", 1)[1].strip())
        if exit_code != 0:
            return AgentResult(outcome="error", text=text[-2000:])
        structured = None
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                structured = parsed
        except json.JSONDecodeError:
            pass
        return AgentResult(outcome="done", text=text, structured=structured, turns=turns)


register("codex", CodexAdapter())
