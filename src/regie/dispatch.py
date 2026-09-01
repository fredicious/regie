from __future__ import annotations

import json
import os
import signal
import subprocess
import time

from regie.agents.base import (
    AgentRequest,
    AgentResult,
    classify_agent_failure,
    get_adapter,
)
from regie.provider_health import ProviderHealthStore, binding_key, provider_key
from regie.rundir import RunDir

_POLL = 0.1


def _codex_line_is_progress(line: str) -> bool:
    """Reconnect/cache chatter is output, but it is not agent progress."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return False
    if not isinstance(event, dict) or event.get("type") in {"error", "turn.failed"}:
        return False
    item = event.get("item")
    if isinstance(item, dict) and item.get("type") == "error":
        return False
    return bool(event.get("type"))


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
    attempt_started = time.time()
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
            "binding": req.binding.model_dump(),
            "duration_seconds": round(max(0.0, time.time() - attempt_started), 3),
        })
        return result

    adapter = get_adapter(req.binding.cli)
    killed = ""
    with out_path.open("wb") as out:
        proc = subprocess.Popen(adapter.build_command(req), cwd=req.cwd,
                                stdin=subprocess.DEVNULL,
                                stdout=out, stderr=subprocess.STDOUT,
                                start_new_session=True)
        started = last_progress = time.time()
        pending_output = ""
        last_size = 0
        while proc.poll() is None:
            time.sleep(_POLL)
            now = time.time()
            size = out_path.stat().st_size
            if size > last_size:
                if req.binding.cli == "codex":
                    with out_path.open("rb") as current:
                        current.seek(last_size)
                        pending_output += current.read(size - last_size).decode(
                            errors="replace")
                    lines = pending_output.split("\n")
                    pending_output = lines.pop()
                    if any(_codex_line_is_progress(line) for line in lines):
                        last_progress = now
                else:
                    last_progress = now
                last_size = size
            if now - started > wall:
                killed = "killed: wall budget"
                break
            if now - last_progress > stall:
                killed = "killed: stall budget"
                break
        if killed:
            _kill_group(proc)

    if killed:
        result = AgentResult(
            outcome="error",
            text=killed,
            failure_kind=classify_agent_failure(killed),
        )
    else:
        result = adapter.parse(out_path.read_text(errors="replace"), proc.returncode)
        if result.outcome == "error" and result.failure_kind is None:
            result.failure_kind = classify_agent_failure(result.text)
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
    semantic_outcome = (
        result.structured.get("status")
        if isinstance(result.structured, dict) else None
    )
    rundir.append_event({"kind": "attempt", "task": task_id, "stage": stage,
                         "attempt": attempt_no, "outcome": result.outcome,
                         "semantic_outcome": semantic_outcome,
                         "turns": result.turns, "usage": result.usage,
                         "metrics": result.metrics.model_dump(),
                         "provider": provider_key(req.binding),
                         "binding": req.binding.model_dump(),
                         "duration_seconds": round(
                             max(0.0, time.time() - attempt_started), 3),
                         "failure_kind": result.failure_kind,
                         "quota": ({"kind": result.quota_kind,
                                    "scope": result.quota_scope,
                                    "reset_at": result.quota_reset_at,
                                    "synthetic": result.quota_synthetic}
                                   if result.outcome == "quota" else None)})
    return result
