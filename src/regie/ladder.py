from __future__ import annotations

from typing import Literal

from regie.models import Attempt, Binding

Action = Literal["retry", "escalate", "halt"]


def _index(attempts: list[Attempt]) -> int:
    quota = sum(1 for a in attempts if a.outcome == "quota")
    # A killed budget/stall/wall attempt has already demonstrated that the
    # current rung cannot finish within its envelope. Retrying it unchanged is
    # pure spend, so it consumes both same-model chances and escalates once.
    other = sum(2 if a.failure_kind in {"budget", "stall", "wall"} else 1
                for a in attempts if a.outcome != "quota")
    return max(0, other - 1) + quota


def next_action(attempts: list[Attempt], bindings: list[Binding]) -> tuple[Action, Binding]:
    """Position on the profile's own bindings ladder from the attempts already
    recorded for this stage: index = max(0, non_quota_attempts - 1) + quota_attempts.
    `attempts` must be non-empty (callers only reach the ladder once a stage
    has at least one recorded attempt)."""
    index = _index(attempts)
    if index >= len(bindings):
        return "halt", bindings[-1]
    previous_index = _index(attempts[:-1])
    action: Action = "retry" if index == previous_index else "escalate"
    return action, bindings[index]
