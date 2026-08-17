from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

from regie.models import Binding


@dataclass
class ProviderHealth:
    status: str
    detail: str


def health(binding: Binding) -> ProviderHealth:
    if binding.cli in {"fake", "fake2"}:
        return ProviderHealth("ready", "test adapter")
    if binding.cli == "openai-api":
        return (ProviderHealth("ready", "OPENAI_API_KEY configured")
                if os.environ.get("OPENAI_API_KEY")
                else ProviderHealth("unavailable", "OPENAI_API_KEY is missing"))
    binary = {"claude": "claude", "codex": "codex"}.get(binding.cli, binding.cli)
    path = shutil.which(binary)
    return (ProviderHealth("ready", path)
            if path else ProviderHealth("unavailable", f"{binary} not found on PATH"))


def total_cost(run) -> float:
    attempts = list(run.planner_attempts) + list(run.final_review_attempts)
    for task in run.tasks.values():
        for stage_attempts in task.attempts.values():
            attempts.extend(stage_attempts)
        for stage_attempts in task.specialist_attempts.values():
            attempts.extend(stage_attempts)
    return sum(attempt.metrics.cost_usd for attempt in attempts)


def task_cost(task) -> float:
    attempts = [attempt for values in task.attempts.values() for attempt in values]
    attempts += [attempt for values in task.specialist_attempts.values() for attempt in values]
    return sum(attempt.metrics.cost_usd for attempt in attempts)
