from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

from regie.config import GatePlugin, RegieConfig
from regie.models import ExecutionRoute, RunState, TaskSpec, WorkflowTier

RISK_LENSES = {
    "security": "security-reviewer",
    "migration": "migration-reviewer",
    "api": "api-reviewer",
    "ui": "ui-reviewer",
    "architecture": "architecture-reviewer",
}

VALID_RISK_TAGS = frozenset({*RISK_LENSES, "external"})
TASK_REVIEW_PROFILES = frozenset(RISK_LENSES.values())


_PLANNING_SIGNALS = {
    "security": ("authentication", "authorization", "permission", "password",
                 "secret", "cryptography", "security boundary"),
    "migration": ("migration", "database schema", "data migration", "backfill",
                  "destructive", "data loss"),
    "public API": ("public api", "breaking api", "openapi", "graphql schema"),
    "architecture": ("architecture", "cross-service", "multiple services",
                     "distributed", "concurrency", "multi-repository"),
    "external dependency": ("external dependency", "third-party integration",
                            "production credential", "production access"),
}


def route_brief(brief: str, requested: WorkflowTier,
                cfg: RegieConfig) -> tuple[ExecutionRoute, str]:
    """Choose the smallest safe workflow before spending a planner call.

    Explicit tiers are authority. Auto routing is deliberately upgrade-biased:
    a low-risk request starts with one owner, while deterministic risk evidence
    enters the planned workflow. The owner can still escalate after inspecting
    the repository by returning ``needs-planning:``.
    """
    selected = requested if requested != "auto" else cfg.workflow.default_tier
    if selected == "fast":
        return "direct", "fast workflow explicitly selected"
    if selected in {"standard", "critical"}:
        return "planned", f"{selected} workflow explicitly selected"
    if not cfg.workflow.direct_execution:
        return "planned", "direct execution disabled by project configuration"

    normalized = " ".join(brief.lower().split())
    for label, signals in _PLANNING_SIGNALS.items():
        matched = next((signal for signal in signals if signal in normalized), None)
        if matched:
            return "planned", f"brief contains {label} signal: {matched}"
    return "direct", "no material risk signal requires an upfront plan"


def infer_risks(task: TaskSpec) -> list[str]:
    text = " ".join([
        task.title, *task.criteria, *task.file_scope, *task.checklist,
        *task.external_dependencies,
    ]).lower()
    triggers = {
        "security": ("auth", "permission", "secret", "token", "password", "crypto"),
        "migration": ("migration", "schema", ".sql", "database", "alembic"),
        "api": ("/api/", "endpoint", "route", "openapi", "graphql", "public api"),
        "ui": (".tsx", ".jsx", ".css", ".html", "frontend", "accessibility"),
    }
    risks = list(task.risk_tags)
    for risk, needles in triggers.items():
        if risk not in risks and any(needle in text for needle in needles):
            risks.append(risk)
    if task.complexity == "hard" and "architecture" not in risks:
        risks.append("architecture")
    if task.external_dependencies and "external" not in risks:
        risks.append("external")
    return risks


def resolve_tier(run: RunState, cfg: RegieConfig) -> WorkflowTier:
    requested = run.workflow if run.workflow != "auto" else cfg.workflow.default_tier
    if requested != "auto":
        return requested
    tasks = list(run.tasks.values())
    risks = {risk for task in tasks for risk in infer_risks(task.spec)}
    if any(task.spec.complexity == "hard" for task in tasks) or risks & {
        "security", "migration", "external",
    }:
        return "critical"
    if len(tasks) <= 1 and not risks:
        return "fast"
    return "standard"


def plan_preflight(tasks: list[TaskSpec]) -> list[str]:
    """Mechanical checks before model-reviewing a plan."""
    errors: list[str] = []
    for task in tasks:
        if not task.file_scope:
            errors.append(f"task {task.id}: predicted file_scope must be non-empty")
        if not task.checklist:
            errors.append(f"task {task.id}: reviewer checklist must be non-empty")
        if task.external_dependencies and not task.checkpoint:
            errors.append(
                f"task {task.id}: external dependencies require a checkpoint")
        checkpoint_evidence = " ".join([
            task.title, *task.criteria, *task.checklist,
            *task.external_dependencies,
        ]).lower()
        authority_signals = (
            "production", "credential", "destructive", "irreversible",
            "delete data", "drop table", "billing", "publish", "deploy",
        )
        if (task.checkpoint and not task.external_dependencies
                and not any(signal in checkpoint_evidence
                            for signal in authority_signals)):
            errors.append(
                f"task {task.id}: checkpoint has no external, destructive, "
                "or irreversible authority boundary")
        if len(task.file_scope) > 12 and task.complexity != "hard":
            errors.append(
                f"task {task.id}: scope spans {len(task.file_scope)} paths; split or mark hard")
        if task.checkpoint == "":
            errors.append(f"task {task.id}: checkpoint reason cannot be blank")
    return errors


def review_profiles(task: TaskSpec, cfg: RegieConfig) -> list[str]:
    """Return explicitly requested, task-scoped specialist profiles.

    Risk tags inform routing and plan design review. They are not themselves
    evidence that an additional post-build agent will add value; the pipeline
    activates those reviewers from the actual task and changed files.
    """
    return [name for name in task.review_lenses
            if name in TASK_REVIEW_PROFILES and name in cfg.profiles]


def scopes_overlap(tasks: list[TaskSpec]) -> bool:
    seen: list[str] = []
    for task in tasks:
        if not task.parallel_safe:
            return True
        for path in task.file_scope:
            if any(_paths_overlap(path, prior) for prior in seen):
                return True
            seen.append(path)
    return False


def _paths_overlap(left: str, right: str) -> bool:
    lprefix = left.rstrip("*/")
    rprefix = right.rstrip("*/")
    return (fnmatch(left, right) or fnmatch(right, left)
            or (lprefix and rprefix and
                (lprefix.startswith(rprefix + "/") or rprefix.startswith(lprefix + "/"))))


def active_gate_plugins(cfg: RegieConfig, stage: str, tier: str,
                        changed: list[str]) -> list[GatePlugin]:
    active = []
    for plugin in cfg.gate_plugins:
        if stage not in plugin.stages or tier not in plugin.tiers:
            continue
        if plugin.trigger_globs and not any(
                fnmatch(path, pattern)
                for path in changed for pattern in plugin.trigger_globs):
            continue
        active.append(plugin)
    return active


def checkpoint_report(task: TaskSpec, repo: Path) -> str:
    risks = ", ".join(infer_risks(task)) or "none"
    deps = ", ".join(task.external_dependencies) or "none"
    return (
        f"# Checkpoint: {task.id} — {task.title}\n\n"
        f"Reason: {task.checkpoint}\n\n"
        f"Risks: {risks}\n\nExternal dependencies: {deps}\n\n"
        f"Worktree: `{repo}`\n")
