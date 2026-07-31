"""Codex exec adapter. NOTE: codex exec --json exposes no usage/quota telemetry
(rate_limits always null in exec mode) — usage stays empty; quota is detected
only via error events."""
from __future__ import annotations

import json
import re
import tempfile

from regie.agents.base import AgentRequest, AgentResult, register

_QUOTA = re.compile(r"(usage|rate).?limit|quota", re.IGNORECASE)


class CodexAdapter:
    def build_command(self, req: AgentRequest) -> list[str]:
        cmd = ["codex", "exec", "--json", "-m", req.binding.model,
               "--sandbox", "workspace-write", "--skip-git-repo-check"]
        if req.output_schema is not None:
            # Outside req.cwd deliberately -- see claude.py's build_command
            # for why a worktree-local schema file is a scratch-leak hazard.
            fd, path = tempfile.mkstemp(suffix=".regie_schema.json")
            with open(fd, "w") as f:
                f.write(json.dumps(req.output_schema))
            cmd += ["--output-schema", path]
        return cmd + [req.prompt]

    def parse(self, stdout: str, exit_code: int) -> AgentResult:
        text, turns, error_msg, usage = None, 0, None, {}
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
            elif ev.get("type") in ("error", "turn.failed"):
                # A failed turn carries a nested JSON error string; an `error`
                # event carries the message directly. Prefer the first error seen.
                msg = ev.get("message")
                if msg is None and isinstance(ev.get("error"), dict):
                    msg = ev["error"].get("message")
                if error_msg is None:
                    error_msg = str(msg or "")
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
        return AgentResult(outcome="done", text=text, structured=structured,
                           turns=turns, usage=usage)


register("codex", CodexAdapter())
