from __future__ import annotations

import os
import signal
import subprocess
import time

from regie.agents.base import AgentRequest, AgentResult, get_adapter
from regie.provider_health import ProviderHealthStore, binding_key, provider_key
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
    health = ProviderHealthStore(rundir.path.parents[1])
    decision = health.reserve(req.binding)
    if not decision.allowed:
        reset = f" until {decision.reset_at}" if decision.reset_at else ""
        message = (f"provider {provider_key(req.binding)} unavailable{reset}: "
                   f"{decision.reason}")
        out_path.write_text(message + "\n")
        result = AgentResult(
            outcome="quota", text=message, quota_kind=decision.kind or "unknown",
            quota_scope=("model" if decision.key == binding_key(req.binding)
                         else "provider"),
            quota_reset_at=decision.reset_at, quota_reason=decision.reason,
            quota_synthetic=True,
        )
        rundir.append_event({
            "kind": "attempt", "task": task_id, "stage": stage,
            "attempt": attempt_no, "outcome": result.outcome,
            "turns": 0, "usage": {}, "metrics": result.metrics.model_dump(),
            "provider": provider_key(req.binding), "quota": {
                "kind": result.quota_kind, "scope": result.quota_scope,
                "reset_at": result.quota_reset_at, "synthetic": True,
            },
        })
        return result

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
    if result.outcome == "quota":
        entry = health.record_quota(req.binding, result)
        # Adapters cannot always recover a reset timestamp. Surface the
        # circuit breaker's conservative fallback in state and events too.
        result.quota_kind = entry["kind"]
        result.quota_scope = entry["scope"]
        result.quota_reset_at = entry["unavailable_until"]
        result.quota_reason = entry["reason"]
        rundir.append_event({
            "kind": "provider_unavailable", "provider": provider_key(req.binding),
            "binding": req.binding.model_dump(), "quota": {
                "kind": result.quota_kind, "scope": result.quota_scope,
                "reset_at": result.quota_reset_at,
            }, "reason": result.quota_reason,
        })
    else:
        health.finish_probe(req.binding, decision, result)
    rundir.append_event({"kind": "attempt", "task": task_id, "stage": stage,
                         "attempt": attempt_no, "outcome": result.outcome,
                         "turns": result.turns, "usage": result.usage,
                         "metrics": result.metrics.model_dump(),
                         "provider": provider_key(req.binding),
                         "quota": ({"kind": result.quota_kind,
                                    "scope": result.quota_scope,
                                    "reset_at": result.quota_reset_at,
                                    "synthetic": result.quota_synthetic}
                                   if result.outcome == "quota" else None)})
    return result
