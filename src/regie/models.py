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


class TokenPolicy(BaseModel):
    """Per-profile controls that bound model work without weakening gates."""

    effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = "medium"
    tools: list[Literal["list", "read", "search", "patch", "shell"]] = Field(
        default_factory=lambda: ["list", "read", "search", "patch", "shell"])
    sandbox: Literal["read-only", "workspace-write"] = "workspace-write"
    context_chars: int = Field(default=16_000, ge=2_000, le=100_000)
    tool_result_chars: int = Field(default=12_000, ge=1_000, le=100_000)
    max_findings: int = Field(default=8, ge=1, le=50)
    max_finding_chars: int = Field(default=1_200, ge=100, le=10_000)
    cache_dynamic_system_sections: bool = False


class UsageMetrics(BaseModel):
    """Provider-neutral token telemetry; raw provider usage is retained too."""

    new_input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    tool_output_bytes: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        # reasoning_output_tokens is normally a subset of output_tokens.
        return (self.new_input_tokens + self.cached_input_tokens
                + self.cache_write_input_tokens + self.output_tokens)


class Budgets(BaseModel):
    turns: int = 40
    wall_minutes: int = 30
    stall_minutes: int = 5


class GateResult(BaseModel):
    name: str
    passed: bool
    detail: str = ""
    flaky: bool = False
    failure_kind: Literal["code", "infrastructure"] | None = None
    duration_seconds: float = Field(default=0.0, ge=0)


class Finding(BaseModel):
    severity: Literal["blocker", "major", "minor"]
    title: str
    detail: str = ""
    file: str | None = None


class CriterionEvidence(BaseModel):
    """A reviewer's explicit verdict for one acceptance criterion."""

    criterion: str
    passed: bool
    evidence: str
    file: str | None = None
    line: int | None = Field(default=None, ge=1)


class PlanReview(BaseModel):
    lens: str
    verdict: Literal["pass", "fail"]
    evidence: list[str] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)


class ProductOwnerDecision(BaseModel):
    """A bounded recovery recommendation; the engine remains authoritative."""

    action: Literal["revise", "accept", "ask_human", "halt"]
    summary: str
    directives: list[str] = Field(default_factory=list)
    accepted_findings: list[str] = Field(default_factory=list)
    rejected_findings: list[str] = Field(default_factory=list)
    human_question: str | None = None


class CheckpointState(BaseModel):
    task_id: str
    reason: str
    status: Literal["pending", "approved", "rejected"] = "pending"
    decided_at: str | None = None


class PRState(BaseModel):
    status: Literal[
        "not-created", "monitoring", "fixing", "waiting-review",
        "waiting-human", "ready", "merged", "closed",
    ] = "not-created"
    ci: Literal["unknown", "pending", "green", "red"] = "unknown"
    review_decision: str = ""
    unresolved_threads: int = 0
    last_comment_id: str = ""
    debug_rounds: int = 0
    updated_at: str | None = None


class ChildRun(BaseModel):
    run_id: str
    repo: str
    relation: str = "child"
    status: Literal["pending", "running", "done", "halted"] = "pending"


class Attempt(BaseModel):
    binding: Binding
    prompt_hash: str = ""
    outcome: Literal["done", "blocked", "failed", "quota"] | None = None
    blocked_question: str | None = None
    gate_results: list[GateResult] = Field(default_factory=list)
    usage: dict = Field(default_factory=dict)
    metrics: UsageMetrics = Field(default_factory=UsageMetrics)
    turns: int = 0
    failure_kind: str | None = None
    failure_signature: str | None = None
    made_progress: bool = False


class TaskSpec(BaseModel):
    id: str
    title: str
    profile: str
    criteria: list[str]
    file_scope: list[str] = Field(default_factory=list)
    checklist: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    planned_tests: list[str] = Field(default_factory=list)
    risk_tags: list[str] = Field(default_factory=list)
    review_lenses: list[str] = Field(default_factory=list)
    external_dependencies: list[str] = Field(default_factory=list)
    checkpoint: str | None = None
    parallel_safe: bool = True
    # Upgrade-only routing hint from the planner: "hard" starts the stage
    # ladders at the profile's strongest rung. There is deliberately no
    # "trivial" downgrade — a wrong "hard" wastes a little quota, a wrong
    # "trivial" wastes two failed attempts plus review churn.
    complexity: Literal["standard", "hard"] = "standard"
    # Direct tasks are owned end-to-end by one implementer, including focused
    # tests. Planned tasks retain the separated test-writer/builder workflow.
    execution: Literal["direct", "tdd"] = "tdd"


TaskStage = Literal["test", "build", "review"]


def _empty_attempts() -> dict[str, list[Attempt]]:
    return {"test": [], "build": [], "review": []}


class TaskState(BaseModel):
    spec: TaskSpec
    stage: TaskStage = "test"
    status: Literal["pending", "running", "done", "blocked", "failed"] = "pending"
    attempts: dict[str, list[Attempt]] = Field(default_factory=_empty_attempts)
    specialist_attempts: dict[str, list[Attempt]] = Field(default_factory=dict)
    criterion_evidence: list[CriterionEvidence] = Field(default_factory=list)
    # Index of the first review attempt for the current post-build cycle.
    # Findings send a task back through build; the repaired code needs a fresh
    # verification ladder without discarding prior review telemetry.
    review_cycle_start: int = 0
    # A passed stage followed by a downstream revision request starts a new
    # provider ladder. Historical attempts remain telemetry, but must not make
    # a successful second/third revision look like provider exhaustion.
    test_cycle_start: int = 0
    build_cycle_start: int = 0
    review_revisions: int = 0
    execution_recovery_used: bool = False
    product_owner_decision: ProductOwnerDecision | None = None
    escaped: bool = False
    start_sha: str = ""


RunStage = Literal[
    "intake", "research", "plan", "approve", "tasks", "checkpoint",
    "finalize", "pr", "reflect", "done", "halted",
]
WorkflowTier = Literal["auto", "fast", "standard", "critical"]
ExecutionRoute = Literal["direct", "planned"]


class RunState(BaseModel):
    id: str
    target_repo: str
    branch: str
    base_sha: str = ""
    worktree_path: str = ""
    base_branch: str = "main"
    pr_url: str = ""
    pushed: bool = False
    autonomous: bool = False
    workflow: WorkflowTier = "auto"
    execution_route: ExecutionRoute = "planned"
    route_reason: str = ""
    stage: RunStage = "intake"
    tasks: dict[str, TaskState] = Field(default_factory=dict)
    halt_reason: str | None = None
    planner_attempts: list[Attempt] = Field(default_factory=list)
    plan_reviews: list[PlanReview] = Field(default_factory=list)
    product_owner_attempts: list[Attempt] = Field(default_factory=list)
    product_owner_decision: ProductOwnerDecision | None = None
    final_review_attempts: list[Attempt] = Field(default_factory=list)
    checkpoints: list[CheckpointState] = Field(default_factory=list)
    research_path: str = ""
    knowledge_snapshot: list[str] = Field(default_factory=list)
    pr_state: PRState = Field(default_factory=PRState)
    parent_id: str | None = None
    children: list[ChildRun] = Field(default_factory=list)

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

    def task_layers(self) -> list[list[str]]:
        """Stable topological layers, used as deterministic fan-out batches."""
        graph = {tid: set(t.spec.depends_on) for tid, t in sorted(self.tasks.items())}
        remaining = set(graph)
        completed: set[str] = set()
        layers: list[list[str]] = []
        while remaining:
            ready = sorted(tid for tid in remaining if graph[tid] <= completed)
            if not ready:
                raise CycleError("task DAG has a cycle")
            layers.append(ready)
            remaining.difference_update(ready)
            completed.update(ready)
        return layers
