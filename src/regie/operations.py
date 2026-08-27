"""Small operator transitions shared by the CLI and control room."""

from __future__ import annotations

from datetime import UTC, datetime

from regie.models import RunState
from regie.rundir import RunDir


def approve_waiting_state(rundir: RunDir, state: RunState) -> str:
    """Durably approve a spec or the currently reached task checkpoint."""
    if state.stage == "approve":
        state.stage = "tasks"
        detail = "spec approved"
    elif state.stage == "checkpoint":
        checkpoint = next(
            (
                item for item in state.checkpoints
                if item.status == "pending"
                and item.task_id in state.tasks
                and state.tasks[item.task_id].status == "done"
            ),
            None,
        )
        if checkpoint is None:
            raise ValueError("run has no reached pending checkpoint")
        checkpoint.status = "approved"
        checkpoint.decided_at = datetime.now(UTC).isoformat()
        state.stage = "tasks"
        detail = f"checkpoint approved for {checkpoint.task_id}"
    else:
        raise ValueError(f"run is not awaiting approval (stage={state.stage})")
    rundir.write_state(state)
    rundir.append_event({"kind": "operator_action", "action": "approve", "detail": detail})
    return detail


def record_clarification(rundir: RunDir, state: RunState, answer: str) -> str:
    """Persist a human answer for a blocked agent; resume remains explicit."""
    reason = state.halt_reason or ""
    marker = "clarify:"
    if state.stage != "halted" or marker not in reason.lower():
        raise ValueError("run is not awaiting clarification")
    answer = answer.strip()
    if not answer:
        raise ValueError("clarification answer cannot be empty")
    index = reason.lower().index(marker)
    question = reason[index + len(marker):].strip()
    decisions = rundir.path / "decisions.md"
    prior = decisions.read_text().rstrip() if decisions.exists() else "# Operator decisions"
    timestamp = datetime.now(UTC).isoformat()
    decisions.write_text(
        f"{prior}\n\n## Clarification {timestamp}\n\n"
        f"Question: {question}\n\nAnswer: {answer}\n"
    )
    rundir.append_event({
        "kind": "operator_action",
        "action": "clarify",
        "detail": question,
    })
    return question
