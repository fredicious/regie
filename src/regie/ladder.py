from __future__ import annotations

from typing import Literal

from regie.models import Attempt, Binding

Action = Literal["retry", "escalate", "halt"]


def _index(attempts: list[Attempt]) -> int:
    quota = sum(1 for a in attempts if a.outcome == "quota")
    other = len(attempts) - quota
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
