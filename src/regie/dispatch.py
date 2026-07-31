from __future__ import annotations

import os
import signal
import subprocess
import time

from regie.agents.base import AgentRequest, AgentResult, get_adapter
from regie.rundir import RunDir

_POLL = 0.1


def _kill_group(proc: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def run_agent(rundir: RunDir, task_id: str, stage: str, attempt_no: int,
              req: AgentRequest, *, _wall_seconds: float | None = None,
              _stall_seconds: float | None = None) -> AgentResult:
    """Dispatch one agent attempt. Intent is logged BEFORE spawn (WAL);
    _wall_seconds/_stall_seconds are test seams overriding budget-derived limits."""
    wall = _wall_seconds or req.budgets.wall_minutes * 60
    stall = _stall_seconds or req.budgets.stall_minutes * 60
    rundir.append_intent({"task": task_id, "stage": stage, "attempt": attempt_no,
                          "binding": req.binding.model_dump()})

    out_path = rundir.task_dir(task_id) / f"attempt-{attempt_no}.out"
    adapter = get_adapter(req.binding.cli)
    killed = ""
    with out_path.open("wb") as out:
        proc = subprocess.Popen(adapter.build_command(req), cwd=req.cwd,
                                stdin=subprocess.DEVNULL,
                                stdout=out, stderr=subprocess.STDOUT,
                                start_new_session=True)
        started = last_growth = time.monotonic()
        last_size = 0
        while proc.poll() is None:
            time.sleep(_POLL)
            now = time.monotonic()
            size = out_path.stat().st_size
            if size > last_size:
                last_size, last_growth = size, now
            if now - started > wall:
                killed = "killed: wall budget"
                break
            if now - last_growth > stall:
                killed = "killed: stall budget"
                break
        if killed:
            _kill_group(proc)

    if killed:
        result = AgentResult(outcome="error", text=killed)
    else:
        result = adapter.parse(out_path.read_text(errors="replace"), proc.returncode)
    rundir.append_event({"kind": "attempt", "task": task_id, "stage": stage,
                         "attempt": attempt_no, "outcome": result.outcome,
                         "turns": result.turns, "usage": result.usage})
    return result
