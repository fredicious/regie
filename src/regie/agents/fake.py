from __future__ import annotations

import json
import sys

from regie.agents.base import AgentRequest, AgentResult, register

_SCRIPT = """
import json, os, pathlib, time
qdir = pathlib.Path(".fake_agent_queue")
queued = sorted((int(p.stem), p) for p in qdir.glob("*.json")) if qdir.is_dir() else []
if queued:
    _n, _p = queued[0]
    spec = json.loads(_p.read_text())
    _p.rename(_p.with_name(_p.name + ".done"))
else:
    spec = json.loads(pathlib.Path(".fake_agent.json").read_text())
for rel, content in spec.get("writes", {}).items():
    p = pathlib.Path(rel); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
time.sleep(spec.get("sleep", 0))
print(json.dumps(spec.get("result", {"outcome": "done"})))
"""


class FakeAdapter:
    def build_command(self, req: AgentRequest) -> list[str]:
        return [sys.executable, "-c", _SCRIPT]

    def parse(self, stdout: str, exit_code: int) -> AgentResult:
        if exit_code != 0:
            return AgentResult(outcome="error", text=stdout[-2000:])
        try:
            return AgentResult(**json.loads(stdout.strip().splitlines()[-1]))
        except (json.JSONDecodeError, IndexError, TypeError, ValueError):
            return AgentResult(outcome="error", text=stdout[-2000:])


register("fake", FakeAdapter())
