from __future__ import annotations

from typing import Literal

from regie.models import Attempt, Binding

Action = Literal["retry", "escalate", "halt"]


def next_action(attempts: list[Attempt], binding: Binding,
                strength_order: list[str]) -> tuple[Action, Binding]:
    if any(a.outcome == "quota" for a in attempts):
        return "halt", binding
    n = len(attempts)
    if n < 2:
        return "retry", binding
    if n == 2:
        key = f"{binding.cli}:{binding.model}"
        try:
            idx = strength_order.index(key)
        except ValueError:
            return "halt", binding
        if idx + 1 < len(strength_order):
            cli, model = strength_order[idx + 1].split(":", 1)
            return "escalate", Binding(cli=cli, model=model, auth=binding.auth)
        return "halt", binding
    return "halt", binding
