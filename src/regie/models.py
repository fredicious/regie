from __future__ import annotations

from graphlib import CycleError as _GraphCycleError
from graphlib import TopologicalSorter
from typing import Literal

from pydantic import BaseModel, Field


class CycleError(Exception):
    pass


class Binding(BaseModel, frozen=True):
    cli: str
    model: str
    auth: str = "subscription"


class Budgets(BaseModel):
    turns: int = 40
    wall_minutes: int = 30
    stall_minutes: int = 5


class GateResult(BaseModel):
    name: str
    passed: bool
    detail: str = ""
    flaky: bool = False


class Finding(BaseModel):
    severity: Literal["blocker", "major", "minor"]
    title: str
    detail: str = ""
    file: str | None = None


class Attempt(BaseModel):
    binding: Binding
    prompt_hash: str = ""
    outcome: Literal["done", "blocked", "failed", "quota"] | None = None
    blocked_question: str | None = None
    gate_results: list[GateResult] = Field(default_factory=list)
    usage: dict = Field(default_factory=dict)
    turns: int = 0


class TaskSpec(BaseModel):
    id: str
    title: str
    profile: str
    criteria: list[str]
    file_scope: list[str] = Field(default_factory=list)
    checklist: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    planned_tests: list[str] = Field(default_factory=list)


TaskStage = Literal["test", "build", "review"]


def _empty_attempts() -> dict[str, list[Attempt]]:
    return {"test": [], "build": [], "review": []}


class TaskState(BaseModel):
    spec: TaskSpec
    stage: TaskStage = "test"
    status: Literal["pending", "running", "done", "blocked", "failed"] = "pending"
    attempts: dict[str, list[Attempt]] = Field(default_factory=_empty_attempts)
    escaped: bool = False


RunStage = Literal["intake", "plan", "approve", "tasks", "finalize", "pr", "done", "halted"]


class RunState(BaseModel):
    id: str
    target_repo: str
    branch: str
    base_sha: str = ""
    worktree_path: str = ""
    base_branch: str = "main"
    pr_url: str = ""
    autonomous: bool = False
    stage: RunStage = "intake"
    tasks: dict[str, TaskState] = Field(default_factory=dict)
    halt_reason: str | None = None
    planner_attempts: list[Attempt] = Field(default_factory=list)

    def ordered_task_ids(self) -> list[str]:
        graph = {tid: sorted(t.spec.depends_on) for tid, t in sorted(self.tasks.items())}
        sorter = TopologicalSorter(graph)
        try:
            order = []
            sorter.prepare()
            while sorter.is_active():
                ready = sorted(sorter.get_ready())
                order.extend(ready)
                sorter.done(*ready)
            return order
        except _GraphCycleError as exc:
            raise CycleError(str(exc)) from exc
