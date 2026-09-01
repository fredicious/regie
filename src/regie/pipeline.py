from __future__ import annotations

import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from regie.agents.base import AgentRequest, classify_agent_failure
from regie.config import Profile, RegieConfig
from regie.dispatch import run_agent
from regie.gates import diff_gate, match_globs, red_test_gate, run_command_gate
from regie.gitops import (
    GitError,
    changed_files,
    cherry_pick,
    ci_failures,
    ci_status,
    commit_all,
    create_pr,
    create_run_worktree,
    delete_branch,
    git,
    head_sha,
    pr_feedback,
    pr_snapshot,
    push_branch,
    rebuild_history,
    remove_run_worktree,
    run_commit_groups,
)
from regie.knowledge import prime as prime_knowledge
from regie.knowledge import propose_learnings
from regie.ladder import next_action
from regie.models import (
    Attempt,
    Binding,
    CheckpointState,
    CriterionEvidence,
    CycleError,
    Finding,
    GateResult,
    PlanReview,
    ProductOwnerDecision,
    RunState,
    TaskSpec,
    TaskState,
)
from regie.onboarding import bootstrap_command
from regie.packets import render_packet, write_packet
from regie.providers import task_cost, total_cost
from regie.research import research_repository
from regie.rundir import RunDir
from regie.workflow import (
    active_gate_plugins,
    checkpoint_report,
    infer_risks,
    plan_preflight,
    resolve_tier,
    review_profiles,
    scopes_overlap,
)

if TYPE_CHECKING:
    from regie.agents.base import AgentResult

# Strict-form schemas: codex's structured output rejects any object without
# additionalProperties:false (invalid_json_schema, found live 2026-07-31 when
# a quota skip-ahead handed a review to codex). Keep EVERY object level strict.
FINDINGS_SCHEMA = {
    "type": "object", "required": ["findings", "criterion_results"],
    "additionalProperties": False,
    "properties": {"findings": {"type": "array", "maxItems": 8, "items": {
        "type": "object",
        "required": ["severity", "title", "detail", "file"],
        "additionalProperties": False,
        "properties": {
            "severity": {"type": "string", "enum": ["blocker", "major", "minor"]},
            "title": {"type": "string", "maxLength": 160},
            "detail": {"type": "string", "maxLength": 1200},
            "file": {"type": ["string", "null"]},
        }}}, "criterion_results": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["criterion", "passed", "evidence", "file", "line"],
            "properties": {
                "criterion": {"type": "string"}, "passed": {"type": "boolean"},
                "evidence": {"type": "string"},
                "file": {"type": ["string", "null"]},
                "line": {"type": ["integer", "null"]},
            }}}}}

SPECIALIST_SCHEMA = {
    "type": "object", "required": ["findings"], "additionalProperties": False,
    "properties": {"findings": FINDINGS_SCHEMA["properties"]["findings"]},
}

DIRECT_OWNER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "summary", "question", "evidence"],
    "properties": {
        "status": {
            "type": "string",
            "enum": ["completed", "clarify", "needs_planning"],
        },
        "summary": {"type": "string", "maxLength": 1200},
        "question": {"type": ["string", "null"], "maxLength": 1800},
        "evidence": {"type": ["string", "null"], "maxLength": 1800},
    },
}

PLAN_REVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["verdict", "evidence", "findings"],
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "findings": SPECIALIST_SCHEMA["properties"]["findings"],
    },
}

PRODUCT_OWNER_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": [
        "action", "summary", "directives", "accepted_findings",
        "rejected_findings", "human_question",
    ],
    "properties": {
        "action": {
            "type": "string",
            "enum": ["revise", "accept", "ask_human", "halt"],
        },
        "summary": {"type": "string", "maxLength": 1200},
        "directives": {"type": "array", "items": {"type": "string"}},
        "accepted_findings": {
            "type": "array", "items": {"type": "string"},
        },
        "rejected_findings": {
            "type": "array", "items": {"type": "string"},
        },
        "human_question": {"type": ["string", "null"]},
    },
}

# Task items are FULLY specified (exact TaskSpec field names, no extras):
# smoke-test finding — with a bare {"type": "array"} the model invents its own
# reasonable-but-wrong field names (predicted_file_scope, dependencies, ...)
# and burns the ladder on pydantic rejections. The CLI's own schema validation
# now forces the shape before we ever see it.
_STR_ARRAY = {"type": "array", "items": {"type": "string"}}
PLAN_SCHEMA = {
    "type": "object", "required": ["spec_markdown", "tasks"],
    "additionalProperties": False,
    "properties": {
        "spec_markdown": {"type": "string"},
        "tasks": {"type": "array", "maxItems": 24, "items": {
            "type": "object",
            "required": ["id", "title", "complexity", "profile", "criteria",
                         "planned_tests", "file_scope", "checklist", "depends_on",
                         "risk_tags", "review_lenses", "external_dependencies",
                         "checkpoint", "parallel_safe"],
            "additionalProperties": False,
            "properties": {
                "id": {"type": "string"}, "title": {"type": "string"},
                "complexity": {"type": "string", "enum": ["standard", "hard"]},
                "profile": {"type": "string"}, "criteria": _STR_ARRAY,
                "planned_tests": _STR_ARRAY, "file_scope": _STR_ARRAY,
                "checklist": _STR_ARRAY, "depends_on": _STR_ARRAY,
                "risk_tags": _STR_ARRAY, "review_lenses": _STR_ARRAY,
                "external_dependencies": _STR_ARRAY,
                "checkpoint": {"type": ["string", "null"]},
                "parallel_safe": {"type": "boolean"},
            }}}}}


def _plan_schema(cfg: RegieConfig) -> dict:
    """Bind planner output to profile names that this installation can load."""
    schema = json.loads(json.dumps(PLAN_SCHEMA))
    profile = schema["properties"]["tasks"]["items"]["properties"]["profile"]
    profile["enum"] = sorted(cfg.profiles)
    return schema


SCRIBE_SCHEMA = {"type": "object",
                 "required": ["commit_messages", "pr_title", "pr_body"],
                 "additionalProperties": False,
                 "properties": {"commit_messages": {"type": "array",
                                                    "items": {"type": "string"}},
                                "pr_title": {"type": "string"},
                                "pr_body": {"type": "string"}}}

CRITERION_RE = re.compile(r"given.+when.+then", re.IGNORECASE | re.DOTALL)

_PLAN_TASK_ID = "PLAN"
_MAX_PLAN_CONTRACT_ATTEMPTS = 3
_PRODUCT_OWNER_TASK_ID = "PRODUCT-OWNER"
_MAX_PRODUCT_OWNER_REVISIONS = 1
_SCRIBE_TASK_ID = "SCRIBE"

_DEBUGGER_PROMPT_FALLBACK = Path(__file__).parent.parent.parent / "profiles" / "debugger.md"

CI_POLL_SECONDS = 30
CI_MAX_DEBUG_ROUNDS = 2
CI_WALL_MINUTES = 30


@dataclass
class PipelineContext:
    spec_excerpt: str = ""
    spec_path: Path = Path()
    decisions_path: Path = Path()
    conventions: str = ""
    convention_paths: list[Path] = field(default_factory=list)


def _artifacts(ctx: PipelineContext) -> dict[str, str]:
    artifacts = {"full spec": str(ctx.spec_path),
                 "decisions": str(ctx.decisions_path)}
    for path in ctx.convention_paths:
        artifacts[f"repository rules ({path.name})"] = str(path)
    return artifacts


def _decisions(ctx: PipelineContext) -> str:
    return ctx.decisions_path.read_text() if ctx.decisions_path.exists() else ""


def _review_profile(run: RunState, task_id: str, cfg: RegieConfig) -> Profile:
    """Cross-model rule: reviewer must not share the builder's model family.
    Returns the whole profile (not just a binding) so the review stage's
    ladder walks the borrowed profile's full bindings list, not a single
    escalation-less binding."""
    reviewer = cfg.profiles["reviewer"]
    builds = run.tasks[task_id].attempts["build"]
    if builds and builds[-1].binding.cli == reviewer.primary.cli:
        return cfg.profiles["builder"]
    return reviewer


def _review_binding(run: RunState, task_id: str, cfg: RegieConfig) -> Binding:
    """Primary binding of the cross-model reviewer profile — a thin
    accessor over _review_profile kept for callers/tests that only need
    the binding, not the whole profile."""
    return _review_profile(run, task_id, cfg).primary


def _stage_profile(task: TaskState, stage: str, cfg: RegieConfig) -> Profile:
    if stage == "build" and task.spec.execution == "direct":
        return cfg.profiles.get("implementer", cfg.profiles["builder"])
    return cfg.profiles[{"test": "test-writer", "build": "builder",
                         "review": "reviewer"}[stage]]


def _effective_bindings(profile: Profile, complexity: str) -> list[Binding]:
    """The ladder a stage actually walks. "hard" tasks start on the profile's
    explicit `hard:` binding (the big gun) when one is configured; remaining
    rungs keep only OTHER-vendor entries. Without a `hard:` binding the normal
    ladder applies (the primary may already BE the big gun, e.g. the builder).
    The hard pick is a separate key because the bindings list orders
    preference-then-failover, not strength (review catch, 2026-07-31)."""
    hard = profile.hard_binding
    if complexity != "hard" or hard is None:
        return profile.bindings
    return [hard] + [b for b in profile.bindings if b.cli != hard.cli]


def _failure_signature(kind: str, text: str) -> str:
    stable = re.sub(r"\b\d+(?:\.\d+)?\b", "#", text.lower())
    stable = re.sub(r"/[^\s:]+", "<path>", stable)
    return f"{kind}:{hashlib.sha256(stable[:4000].encode()).hexdigest()[:16]}"


def _budget_reason(run: RunState, cfg: RegieConfig, task_id: str | None = None) -> str | None:
    run_limit = cfg.workflow.max_run_usd
    if run_limit and total_cost(run) >= run_limit:
        return f"run cost budget exhausted (${total_cost(run):.2f}/${run_limit:.2f})"
    if task_id is not None:
        task_limit = cfg.workflow.max_task_usd
        spent = task_cost(run.tasks[task_id])
        if task_limit and spent >= task_limit:
            return f"task {task_id} cost budget exhausted (${spent:.2f}/${task_limit:.2f})"
    return None


def _change_manifest(repo: Path, start_sha: str) -> str:
    if not start_sha:
        return ""
    try:
        names = git(repo, "diff", "--name-only", f"{start_sha}..HEAD")
        stat = git(repo, "diff", "--stat", f"{start_sha}..HEAD")
    except GitError:
        return ""
    return f"Changed files:\n{names or '(none)'}\n\nDiff stat:\n{stat or '(none)'}"


def _dispatch(rundir: RunDir, run: RunState, task_id: str, stage: str,
              profile: Profile, cfg: RegieConfig, repo: Path,
              ctx: PipelineContext, extra: str) -> tuple[Attempt, AgentResult]:
    task = run.tasks[task_id]
    attempts = task.attempts[stage]
    ladder_attempts = (
        attempts[task.review_cycle_start:] if stage == "review" else attempts
    )
    ladder_profile = _review_profile(run, task_id, cfg) if stage == "review" else profile
    ladder = _effective_bindings(ladder_profile, task.spec.complexity)
    binding = ladder[0]
    if ladder_attempts:
        _action, binding = next_action(ladder_attempts, ladder)
        # caller already checked for halt; retry keeps binding, escalate upgrades
    packet = render_packet(
        task.spec, ctx.spec_excerpt, _decisions(ctx), ctx.conventions,
        extra=extra, stage=stage, context_budget=profile.token_policy.context_chars,
        artifacts=_artifacts(ctx),
        change_manifest=_change_manifest(repo, task.start_sha) if stage == "review" else "")
    write_packet(rundir.task_dir(task_id), packet)
    schema = DIRECT_OWNER_SCHEMA if (
        stage == "build" and task.spec.execution == "direct") else None
    if stage == "review":
        schema = json.loads(json.dumps(FINDINGS_SCHEMA))
        schema["properties"]["findings"]["maxItems"] = profile.token_policy.max_findings
        item = schema["properties"]["findings"]["items"]["properties"]
        item["detail"]["maxLength"] = profile.token_policy.max_finding_chars
    req = AgentRequest(prompt=packet, instructions=profile.prompt_text(), cwd=repo,
                       binding=binding, budgets=profile.budgets,
                       token_policy=profile.token_policy,
                       allowed_commands=(getattr(cfg, "commands", {})
                                         if stage in ("test", "build") else {}),
                       output_schema=schema)
    attempt = Attempt(binding=binding, prompt_hash=profile.prompt_hash())
    result = run_agent(rundir, task_id, stage, len(attempts) + 1, req)
    if (stage == "build" and task.spec.execution == "direct"
            and result.outcome == "done" and result.structured):
        status = result.structured.get("status")
        if status == "clarify":
            question = str(result.structured.get("question") or "").strip()
            if question:
                result.outcome = "blocked"
                result.blocked_question = "clarify: " + question
            else:
                result.outcome = "error"
                result.text = "direct owner returned clarify without a question"
        elif status == "needs_planning":
            evidence = str(result.structured.get("evidence") or "").strip()
            if evidence:
                result.outcome = "blocked"
                result.blocked_question = "needs-planning: " + evidence
            else:
                result.outcome = "error"
                result.text = "direct owner requested planning without evidence"
    attempt.outcome = {"done": "done", "blocked": "blocked",
                       "quota": "quota"}.get(result.outcome, "failed")
    attempt.blocked_question = result.blocked_question
    attempt.usage, attempt.metrics, attempt.turns = result.usage, result.metrics, result.turns
    if attempt.outcome == "failed":
        attempt.failure_kind = result.failure_kind or classify_agent_failure(result.text)
        attempt.failure_signature = _failure_signature(attempt.failure_kind, result.text)
    attempts.append(attempt)
    return attempt, result


def _halt(rundir: RunDir, run: RunState, task_id: str, reason: str) -> None:
    run.tasks[task_id].status = "failed" if "blocked" not in reason else "blocked"
    run.stage = "halted"
    run.halt_reason = reason
    rundir.write_state(run)


def _ladder_halt_reason(stage: str, last: Attempt, task_id: str) -> str:
    if last.outcome == "quota":
        return f"quota exhausted on {last.binding.cli}:{last.binding.model} during {stage}"
    return f"{stage} ladder exhausted on {task_id}"


def _should_halt(rundir: RunDir, run: RunState, task_id: str, stage: str,
                 cfg: RegieConfig) -> bool:
    attempts = run.tasks[task_id].attempts[stage]
    ladder_attempts = (
        attempts[run.tasks[task_id].review_cycle_start:]
        if stage == "review" else attempts
    )
    if not ladder_attempts:
        return False
    ladder_profile = (_review_profile(run, task_id, cfg) if stage == "review" else
                      _stage_profile(run.tasks[task_id], stage, cfg))
    ladder = _effective_bindings(ladder_profile, run.tasks[task_id].spec.complexity)
    action, _ = next_action(ladder_attempts, ladder)
    if action == "halt":
        _halt(
            rundir,
            run,
            task_id,
            _ladder_halt_reason(stage, ladder_attempts[-1], task_id),
        )
        return True
    return False


def _specialist_profiles(task: TaskSpec, repo: Path, start_sha: str,
                         cfg: RegieConfig) -> list[str]:
    """Deterministically activate expensive review expertise only on risk."""
    try:
        files = [p for p in git(repo, "diff", "--name-only", f"{start_sha}..HEAD").splitlines()
                 if p]
    except GitError:
        files = []
    evidence = " ".join([task.title, *task.criteria, *task.checklist,
                         *task.risk_tags, *task.review_lenses,
                         *task.external_dependencies, *files]).lower()
    selected: list[str] = review_profiles(task, cfg)
    triggers = {
        "security-reviewer": ("auth", "permission", "secret", "token", "crypto",
                              "sanitize", "injection", "password"),
        "migration-reviewer": ("migration", "schema", ".sql", "database", "alembic"),
        "api-reviewer": ("/api/", "endpoint", "route", "public api", "openapi", "graphql"),
        "ui-reviewer": (".tsx", ".jsx", ".css", ".html", "accessibility", "frontend", " ui "),
    }
    for profile_name, needles in triggers.items():
        if (profile_name in cfg.profiles and profile_name not in selected
                and any(needle in evidence for needle in needles)):
            selected.append(profile_name)
    roots = {path.split("/", 1)[0] for path in files}
    if ("architecture-reviewer" in cfg.profiles
            and "architecture-reviewer" not in selected
            and (len(files) >= 8 or len(roots) >= 3 or task.complexity == "hard")):
        selected.append("architecture-reviewer")
    return selected


def _run_specialist_reviews(rundir: RunDir, run: RunState, task_id: str,
                            cfg: RegieConfig, repo: Path,
                            ctx: PipelineContext) -> tuple[list[Finding], str | None]:
    task = run.tasks[task_id]
    if not cfg.workflow.design_reviews or resolve_tier(run, cfg) == "fast":
        return [], None
    selected = _specialist_profiles(task.spec, repo, task.start_sha, cfg)
    findings: list[Finding] = []
    manifest = _change_manifest(repo, task.start_sha)
    for name in selected:
        profile = cfg.profiles[name]
        attempts = task.specialist_attempts.setdefault(name, [])
        completed = False
        for binding in profile.bindings:
            packet = render_packet(
                task.spec, ctx.spec_excerpt, _decisions(ctx), ctx.conventions,
                stage="specialist-review", context_budget=profile.token_policy.context_chars,
                artifacts=_artifacts(ctx), change_manifest=manifest)
            write_packet(rundir.task_dir(f"{task_id}-{name.upper()}"), packet)
            schema = json.loads(json.dumps(SPECIALIST_SCHEMA))
            schema["properties"]["findings"]["maxItems"] = profile.token_policy.max_findings
            schema["properties"]["findings"]["items"]["properties"]["detail"][
                "maxLength"] = profile.token_policy.max_finding_chars
            req = AgentRequest(
                prompt=packet, instructions=profile.prompt_text(), cwd=repo,
                binding=binding, budgets=profile.budgets,
                token_policy=profile.token_policy, output_schema=schema)
            result = run_agent(rundir, f"{task_id}-{name.upper()}",
                               f"review:{name}", len(attempts) + 1, req)
            attempt = Attempt(
                binding=binding, prompt_hash=profile.prompt_hash(), turns=result.turns,
                usage=result.usage, metrics=result.metrics,
                outcome={"done": "done", "quota": "quota",
                         "blocked": "blocked"}.get(result.outcome, "failed"))
            if attempt.outcome == "failed":
                attempt.failure_kind = (
                    result.failure_kind or classify_agent_failure(result.text)
                )
                attempt.failure_signature = _failure_signature(attempt.failure_kind, result.text)
            attempts.append(attempt)
            _discard_worktree_scratch(repo)
            if result.outcome == "done" and result.structured is not None:
                findings.extend(Finding(**raw)
                                for raw in result.structured.get("findings", []))
                completed = True
                break
        if not completed:
            return findings, f"specialist review unavailable: {name}"
    return findings, None


def _gate_and_advance(rundir: RunDir, run: RunState, task_id: str, stage: str,
                      gates: list[GateResult], attempt: Attempt, on_pass,
                      repo: Path) -> None:
    _record_gate_events(rundir, task_id, stage, gates)
    attempt.gate_results = gates
    if all(g.passed for g in gates):
        on_pass()
    else:
        attempt.outcome = "failed"
        failed = [g for g in gates if not g.passed]
        detail = "\n".join(f"{g.name}: {g.detail[:1500]}" for g in failed)
        attempt.failure_kind = "gate"
        attempt.failure_signature = _failure_signature("gate", detail)
        earlier = run.tasks[task_id].attempts[stage][:-1]
        if any(a.failure_signature == attempt.failure_signature for a in earlier):
            attempt.failure_kind = "repeated-gate"
        _write_note(rundir, task_id, stage,
                    "Previous attempt failed gates:\n" + "\n".join(
                        f"- {g.name}: {g.detail[:1500]}" for g in failed))
        infrastructure = [
            gate for gate in failed if gate.failure_kind == "infrastructure"
        ]
        if infrastructure:
            attempt.failure_kind = "infrastructure"
            task = run.tasks[task_id]
            task.status = "blocked"
            run.stage = "halted"
            run.halt_reason = (
                "infrastructure gate failed; implementation preserved: "
                + ", ".join(gate.name for gate in infrastructure)
            )
            rundir.append_event({
                "kind": "infrastructure_blocked",
                "task": task_id,
                "stage": stage,
                "gates": [gate.name for gate in infrastructure],
                "reason": run.halt_reason,
            })
            rundir.write_state(run)
            return
        # Discard the failed attempt's uncommitted worktree edits so the next
        # attempt starts from a clean tree instead of building on top of them.
        git(repo, "checkout", "--", ".")
        git(repo, "clean", "-fd")
    rundir.write_state(run)


def _record_gate_events(rundir: RunDir, task_id: str, stage: str,
                        gates: list[GateResult]) -> None:
    for gate in gates:
        rundir.append_event({
            "kind": "gate",
            "task": task_id,
            "stage": stage,
            "gate": gate.name,
            "outcome": "passed" if gate.passed else "failed",
            "flaky": gate.flaky,
            "failure_kind": gate.failure_kind,
            "duration_seconds": round(gate.duration_seconds, 3),
        })


def _prepare_environment(rundir: RunDir, run: RunState, task_id: str,
                         cfg: RegieConfig, repo: Path) -> bool:
    """Prepare one isolated worktree before spending any model tokens."""
    command = getattr(cfg, "commands", {}).get("setup") or bootstrap_command(repo)
    if not command:
        return True
    marker_dir = rundir.path / "environment"
    marker_dir.mkdir(exist_ok=True)
    key = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:16]
    marker = marker_dir / f"{key}.json"
    if marker.is_file():
        return True

    gate = run_command_gate("setup", command, repo)
    (marker_dir / f"{key}.log").write_text(gate.detail)
    rundir.append_event({
        "kind": "environment_setup",
        "task": task_id,
        "stage": "setup",
        "command": command,
        "outcome": "passed" if gate.passed else "failed",
        "failure_kind": gate.failure_kind,
        "duration_seconds": round(gate.duration_seconds, 3),
    })
    if not gate.passed:
        run.tasks[task_id].status = "blocked"
        run.stage = "halted"
        run.halt_reason = (
            f"worktree setup failed before agent dispatch: {gate.detail[-500:]}"
        )
        rundir.write_state(run)
        return False
    marker.write_text(json.dumps({"command": command, "prepared": True}, indent=2))
    return True


def _policy_gates(run: RunState, cfg: RegieConfig, repo: Path,
                  stage: str, start_sha: str, *, include_build: bool = True
                  ) -> list[GateResult]:
    tier = resolve_tier(run, cfg)
    gates: list[GateResult] = []
    if stage in {"build", "finalize"}:
        names = ["typecheck"]
        if include_build:
            names.append("build")
        if tier != "fast":
            names.append("coverage")
        for name in names:
            if name in cfg.commands:
                gates.append(run_command_gate(name, cfg.commands[name], repo,
                                              rerun_on_fail=(name == "coverage")))
    try:
        changed = [line for line in
                   git(repo, "diff", "--name-only", f"{start_sha}..HEAD").splitlines()
                   if line]
    except GitError:
        changed = []
    for plugin in active_gate_plugins(cfg, stage, tier, changed):
        gates.append(run_command_gate(plugin.name, plugin.command, repo))
    return gates


def run_task(rundir: RunDir, run: RunState, task_id: str, cfg: RegieConfig,
             repo: Path, ctx: PipelineContext,
             max_dispatches: int | None = None) -> None:
    """Advance one task through test → build → review. Test seam: max_dispatches."""
    task = run.tasks[task_id]
    if not _prepare_environment(rundir, run, task_id, cfg, repo):
        return
    task.status = "running"
    if not task.start_sha:
        task.start_sha = head_sha(repo)
    dispatched = 0
    primed = (prime_knowledge(rundir, repo, task.spec, "implementation")
              if cfg.workflow.knowledge else [])

    while task.status == "running":
        if max_dispatches is not None and dispatched >= max_dispatches:
            return
        stage = task.stage
        budget_reason = _budget_reason(run, cfg, task_id)
        if budget_reason:
            _halt(rundir, run, task_id, budget_reason)
            return
        if _should_halt(rundir, run, task_id, stage, cfg):
            return
        extra = _notes_for(rundir, task_id, stage)
        if primed:
            extra += "\n\nRelevant project knowledge:\n" + "\n".join(
                f"- {entry.fact}" for entry in primed)
        profile = _stage_profile(task, stage, cfg)
        pre_dispatch = set(changed_files(repo))
        attempt, result = _dispatch(rundir, run, task_id, stage, profile, cfg,
                                    repo, ctx, extra)
        attempt.made_progress = set(changed_files(repo)) != pre_dispatch
        dispatched += 1

        if attempt.outcome == "quota":
            rundir.write_state(run)
            if _should_halt(rundir, run, task_id, stage, cfg):
                return
            continue
        if attempt.outcome == "blocked":
            # A blocked agent may have made partial edits before deciding to
            # stop; nothing ungated may survive into a later stage's commit
            # (dogfood finding: blocked-path leftovers were swept into
            # test-stage commits by the next commit_all).
            git(repo, "checkout", "--", ".")
            git(repo, "clean", "-fd")
            question = attempt.blocked_question or ""
            if (stage == "build" and task.spec.execution == "direct"
                    and question.startswith("needs-planning:")):
                run.tasks = {}
                run.checkpoints = []
                run.execution_route = "planned"
                run.route_reason = (
                    "direct owner found planning evidence: "
                    + question.removeprefix("needs-planning:").strip()
                )
                run.stage = "plan"
                rundir.append_event({
                    "kind": "workflow_escalated",
                    "task": task_id,
                    "stage": "build",
                    "route": "planned",
                    "reason": run.route_reason,
                })
                rundir.write_state(run)
                return
            if stage == "build" and question.startswith("bad-test:"):
                if not task.escaped:
                    task.escaped = True
                    task.stage = "test"
                    _write_note(rundir, task_id, "test", f"Builder claims: {question}")
                    rundir.write_state(run)
                    continue
                # No ping-pong: one escape per task. A repeat bad-test claim is
                # absorbed as a failed build attempt so the ladder can retry,
                # escalate, or eventually halt through exhaustion.
                attempt.outcome = "failed"
                rundir.write_state(run)
                continue
            _halt(rundir, run, task_id, f"blocked: {question}")
            return
        if attempt.outcome == "failed":
            # Dispatch-level death (budget kill, CLI error, unparseable
            # output): without a note the retry repeats the mistake blind.
            if result.text:
                _write_note(rundir, task_id, stage,
                            "Previous attempt died before gates:\n"
                            + result.text[:1500])
            rundir.write_state(run)
            continue

        if stage == "test":
            gates = [red_test_gate(repo, cfg.commands["test"]),
                     run_command_gate("lint", cfg.commands["lint"], repo)]
            def _pass_test(pre=pre_dispatch):
                # No new changes with a satisfied red contract means the work
                # was already committed (stage re-entry) — advance without an
                # empty commit (dogfood finding: the old hard changes-gate
                # burned ladders on legitimate re-entries).
                if set(changed_files(repo)) - pre:
                    commit_all(repo, f"test({task_id}): red tests for {task.spec.title}")
                task.stage = "build"
            _gate_and_advance(rundir, run, task_id, stage, gates, attempt,
                              _pass_test, repo)
        elif stage == "build":
            gates = []
            if "build" in cfg.commands:
                gates.append(run_command_gate("build", cfg.commands["build"], repo))
            gates.extend([
                run_command_gate("test", cfg.commands["test"], repo,
                                 rerun_on_fail=True),
                run_command_gate("lint", cfg.commands["lint"], repo),
            ])
            if task.spec.execution != "direct":
                gates.append(diff_gate(repo, cfg.test_globs))
            gates.extend(_policy_gates(
                run, cfg, repo, "build", task.start_sha, include_build=False))
            def _pass_build(pre=pre_dispatch):
                if set(changed_files(repo)) - pre:
                    commit_all(repo, f"feat({task_id}): {task.spec.title}")
                task.review_cycle_start = len(task.attempts["review"])
                task.stage = "review"
            _gate_and_advance(rundir, run, task_id, stage, gates, attempt,
                              _pass_build, repo)
        else:  # review
            # The reviewer has no authority to edit; discard anything a
            # misbehaving reviewer left so it can't ride into later commits.
            git(repo, "checkout", "--", ".")
            git(repo, "clean", "-fd")
            findings = [Finding(**f) for f in
                        (result.structured or {}).get("findings", [])]
            evidence = [CriterionEvidence(**raw) for raw in
                        (result.structured or {}).get("criterion_results", [])]
            task.criterion_evidence = evidence
            if evidence:
                (rundir.task_dir(task_id) / "criterion-evidence.json").write_text(
                    json.dumps([item.model_dump() for item in evidence], indent=2))
            for item in evidence:
                if not item.passed:
                    findings.append(Finding(
                        severity="blocker",
                        title=f"acceptance criterion failed: {item.criterion[:100]}",
                        detail=item.evidence, file=item.file))
            serious = [f for f in findings if f.severity in ("blocker", "major")]
            if not serious:
                specialist_findings, specialist_error = _run_specialist_reviews(
                    rundir, run, task_id, cfg, repo, ctx)
                if specialist_error:
                    _halt(rundir, run, task_id, specialist_error)
                    return
                findings.extend(specialist_findings)
                serious = [f for f in findings if f.severity in ("blocker", "major")]
            minors = [f for f in findings if f.severity == "minor"]
            tdir = rundir.task_dir(task_id)
            if minors:
                _append_json(tdir / "minor-findings.json", minors)
            if serious:
                _append_json(tdir / "findings.json", serious)
                _write_note(rundir, task_id, "build",
                            "Review findings to fix:\n" + "\n".join(
                                f"- [{f.severity}] {f.title}: {f.detail}" for f in serious))
                task.stage = "build"
            else:
                task.status = "done"
            rundir.write_state(run)


def _append_json(path: Path, findings: list[Finding]) -> None:
    existing = json.loads(path.read_text()) if path.exists() else []
    existing.extend(f.model_dump() for f in findings)
    path.write_text(json.dumps(existing, indent=2))


def _write_note(rundir: RunDir, task_id: str, stage: str, note: str) -> None:
    (rundir.task_dir(task_id) / f"note-{stage}.md").write_text(note)


def _notes_for(rundir: RunDir, task_id: str, stage: str) -> str:
    path = rundir.task_dir(task_id) / f"note-{stage}.md"
    return path.read_text() if path.exists() else ""


def _render_plan_packet(brief_text: str, conventions: str, decisions: str,
                        extra: str, *, context_budget: int = 20_000,
                        artifacts: dict[str, str] | None = None) -> str:
    fixed_budget = max(2_000, context_budget)
    brief_budget = fixed_budget // 2
    other_budget = max(1_000, fixed_budget // 6)
    def bounded(value: str, budget: int) -> str:
        if len(value) <= budget:
            return value
        return value[:budget] + "\n[... truncated; read full artifact]"
    artifact_lines = "\n".join(
        f"- {name}: `{path}`" for name, path in (artifacts or {}).items()) or "- (none)"
    return "\n\n".join([
        f"# Brief\n{bounded(brief_text, brief_budget)}",
        f"## Conventions\n{bounded(conventions, other_budget)}",
        f"## Decisions so far\n{bounded(decisions, other_budget) or '(none yet)'}",
        f"## Notes\n{bounded(extra, other_budget) or '(none)'}",
        f"## Full artifacts\n{artifact_lines}",
    ]) + "\n"


def _validate_plan(structured: dict | None, cfg: RegieConfig) -> list[str]:
    errors: list[str] = []
    if not structured or "spec_markdown" not in structured or "tasks" not in structured:
        return ["planner output missing spec_markdown or tasks"]
    specs: list[TaskSpec] = []
    seen_ids: set[str] = set()
    for i, raw in enumerate(structured["tasks"]):
        try:
            spec = TaskSpec(**raw)
        except Exception as exc:  # noqa: BLE001 - any pydantic validation error
            errors.append(f"task[{i}]: invalid task spec: {exc}")
            continue
        if spec.id in seen_ids:
            errors.append(f"task {spec.id}: duplicate task id")
        seen_ids.add(spec.id)
        if not spec.planned_tests:
            errors.append(f"task {spec.id}: planned_tests must be non-empty")
        for criterion in spec.criteria:
            if not CRITERION_RE.search(criterion):
                errors.append(f"task {spec.id}: criterion not Given/When/Then: {criterion}")
        if spec.profile not in cfg.profiles:
            errors.append(f"task {spec.id}: unknown profile '{spec.profile}'")
        specs.append(spec)

    all_ids = {s.id for s in specs}
    for spec in specs:
        for dep in spec.depends_on:
            if dep not in all_ids:
                errors.append(f"task {spec.id}: depends_on unknown task '{dep}'")

    if specs:
        probe = RunState(id="probe", target_repo="", branch="")
        probe.tasks = {s.id: TaskState(spec=s) for s in specs}
        try:
            probe.ordered_task_ids()
        except CycleError:
            errors.append("task DAG has a cycle")
    return errors


def _halt_run(rundir: RunDir, run: RunState, reason: str) -> None:
    run.stage = "halted"
    run.halt_reason = reason
    rundir.write_state(run)


_PLAN_LENSES = (
    "plan-feasibility", "plan-completeness", "plan-scope",
)


def _plan_review_names(tasks: list[TaskSpec], cfg: RegieConfig,
                       tier: str, requested_workflow: str = "auto") -> list[str]:
    if not cfg.workflow.plan_reviews or tier == "fast":
        return []
    risks = {risk for task in tasks for risk in infer_risks(task)}
    hard = any(task.complexity == "hard" for task in tasks)
    external = any(task.external_dependencies for task in tasks)
    names: list[str] = []

    # Explicit critical mode is the operator asking for the full panel. Auto
    # mode must earn each generic lens from plan evidence; otherwise a bounded
    # one-task migration spends more time reviewing its plan than changing code.
    if requested_workflow == "critical":
        names.extend(_PLAN_LENSES)
    else:
        if len(tasks) > 1 or requested_workflow == "standard":
            names.append("plan-completeness")
        if hard or external or "security" in risks:
            names.append("plan-feasibility")
        if len(tasks) >= 3 or hard or "architecture" in risks:
            names.append("plan-scope")

    names = [name for name in names if name in cfg.profiles]
    design = {
        "security": "security-design-reviewer",
        "ui": "ux-design-reviewer",
        "api": "architecture-design-reviewer",
        "architecture": "architecture-design-reviewer",
        "migration": "architecture-design-reviewer",
    }
    if cfg.workflow.design_reviews:
        for risk in sorted(risks):
            name = design.get(risk)
            if name and name in cfg.profiles and name not in names:
                names.append(name)
    return names


def _run_plan_reviews(rundir: RunDir, run: RunState, cfg: RegieConfig,
                      worktree: Path, brief: str, structured: dict) -> list[str]:
    tasks = [TaskSpec(**raw) for raw in structured["tasks"]]
    prospective = run.model_copy(deep=True)
    prospective.tasks = {task.id: TaskState(spec=task) for task in tasks}
    tier = resolve_tier(prospective, cfg)
    names = _plan_review_names(tasks, cfg, tier, run.workflow)
    if not names:
        return []
    packet = "\n\n".join([
        "# Original brief\n" + brief,
        "## Proposed spec\n" + structured["spec_markdown"],
        "## Proposed task plan\n```json\n"
        + json.dumps(structured["tasks"], indent=2) + "\n```",
        ("## Rules\nReturn PASS only when this plan is executable against the "
         "repository and complete for your lens. Cite concrete evidence."),
    ]) + "\n"
    failures: list[str] = []
    for name in names:
        profile = cfg.profiles[name]
        result = None
        last_outcome = "unavailable"
        for attempt_no, binding in enumerate(profile.bindings, 1):
            candidate = run_agent(
                rundir, f"PLAN-{name.upper()}", f"plan-review:{name}", attempt_no,
                AgentRequest(
                    prompt=packet,
                    instructions=profile.prompt_text(),
                    cwd=worktree,
                    binding=binding,
                    budgets=profile.budgets,
                    token_policy=profile.token_policy,
                    output_schema=PLAN_REVIEW_SCHEMA,
                ),
            )
            last_outcome = candidate.outcome
            if candidate.outcome == "done" and candidate.structured:
                result = candidate
                break
        if result is None:
            failures.append(f"{name}: reviewer unavailable ({last_outcome})")
            continue
        review = PlanReview(lens=name, **result.structured)
        run.plan_reviews.append(review)
        if review.verdict == "fail":
            failures.extend(
                f"{name}: {finding.title}: {finding.detail}"
                for finding in review.findings)
            if not review.findings:
                failures.append(f"{name}: failed without a finding")
    rundir.write_state(run)
    return failures


def _write_product_owner_decision(
    rundir: RunDir,
    decision: ProductOwnerDecision,
) -> None:
    payload = decision.model_dump()
    (rundir.path / "product-owner-decision.json").write_text(
        json.dumps(payload, indent=2) + "\n"
    )
    lines = [
        "# Product Owner recovery decision",
        "",
        f"**Action:** {decision.action}",
        "",
        decision.summary,
    ]
    for title, values in (
        ("Directives", decision.directives),
        ("Accepted findings", decision.accepted_findings),
        ("Rejected findings", decision.rejected_findings),
    ):
        if values:
            lines.extend(["", f"## {title}", "", *[f"- {item}" for item in values]])
    if decision.human_question:
        lines.extend(["", "## Human question", "", decision.human_question])
    (rundir.path / "product-owner-decision.md").write_text("\n".join(lines) + "\n")


def _run_product_owner(
    rundir: RunDir,
    run: RunState,
    cfg: RegieConfig,
    worktree: Path,
    brief: str,
    structured: dict,
    deterministic_errors: list[str],
    review_errors: list[str],
) -> tuple[ProductOwnerDecision | None, str | None]:
    """Ask one bounded advisor to resolve plan non-convergence.

    The returned decision is only a recommendation. ``plan_stage`` validates
    its authority before changing state; notably, ``accept`` cannot waive
    schema/preflight failures.
    """
    profile = cfg.profiles.get("product-owner")
    if profile is None:
        return None, "product-owner profile is not configured"
    packet = "\n\n".join([
        "# Recovery boundary\nThe plan did not converge after three reviewed drafts.",
        "## Original brief\n" + brief,
        "## Latest proposed spec\n" + str(structured.get("spec_markdown", "")),
        "## Latest proposed task plan\n```json\n"
        + json.dumps(structured.get("tasks", []), indent=2) + "\n```",
        "## Deterministic validation failures\n"
        + ("\n".join(f"- {item}" for item in deterministic_errors) or "- None"),
        "## Review-panel findings\n"
        + ("\n".join(f"- {item}" for item in review_errors) or "- None"),
        "## Attempt and provider evidence\n"
        + json.dumps([
            {
                "provider": attempt.binding.cli,
                "model": attempt.binding.model,
                "outcome": attempt.outcome,
                "failure_kind": attempt.failure_kind,
                "failure_signature": attempt.failure_signature,
            }
            for attempt in run.planner_attempts
        ], indent=2),
        ("## Authority boundary\nYou may consolidate findings, reject advisory "
         "scope suggestions, or direct one final planner revision. You may not "
         "waive deterministic validation, tests, security gates, destructive "
         "approval, credentials, provider policy, or budgets. Ask the human "
         "when their authority is required."),
    ]) + "\n"
    write_packet(rundir.task_dir(_PRODUCT_OWNER_TASK_ID), packet)
    first_attempt = len(run.product_owner_attempts) + 1
    last_outcome = "unavailable"
    for offset, binding in enumerate(profile.bindings):
        result = run_agent(
            rundir,
            _PRODUCT_OWNER_TASK_ID,
            "product-owner",
            first_attempt + offset,
            AgentRequest(
                prompt=packet,
                instructions=profile.prompt_text(),
                cwd=worktree,
                binding=binding,
                budgets=profile.budgets,
                token_policy=profile.token_policy,
                output_schema=PRODUCT_OWNER_SCHEMA,
            ),
        )
        outcome = {"done": "done", "quota": "quota", "blocked": "blocked"}.get(
            result.outcome, "failed"
        )
        attempt = Attempt(
            binding=binding,
            prompt_hash=profile.prompt_hash(),
            outcome=outcome,
            blocked_question=result.blocked_question,
            turns=result.turns,
            usage=result.usage,
            metrics=result.metrics,
        )
        if outcome == "failed":
            attempt.failure_kind = (
                result.failure_kind or classify_agent_failure(result.text)
            )
            attempt.failure_signature = _failure_signature(
                attempt.failure_kind, result.text
            )
        run.product_owner_attempts.append(attempt)
        rundir.write_state(run)
        last_outcome = outcome
        if result.outcome != "done" or not result.structured:
            continue
        try:
            decision = ProductOwnerDecision(**result.structured)
        except Exception:  # noqa: BLE001 - malformed provider contract; try next rung
            attempt.outcome = "failed"
            attempt.failure_kind = "contract"
            attempt.failure_signature = _failure_signature(
                "contract", json.dumps(result.structured, sort_keys=True)
            )
            rundir.write_state(run)
            last_outcome = "invalid contract"
            continue
        run.product_owner_decision = decision
        _write_product_owner_decision(rundir, decision)
        rundir.append_event({
            "kind": "product_owner_decision",
            "task": _PRODUCT_OWNER_TASK_ID,
            "stage": "product-owner",
            "action": decision.action,
            "summary": decision.summary,
        })
        rundir.write_state(run)
        return decision, None
    return None, f"product owner unavailable ({last_outcome})"


def _apply_plan(rundir: RunDir, run: RunState, structured: dict) -> None:
    spec_dir = rundir.path / "spec"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.md").write_text(structured["spec_markdown"])
    specs = [TaskSpec(**t) for t in structured["tasks"]]
    for spec in specs:
        spec.risk_tags = infer_risks(spec)
    run.tasks = {s.id: TaskState(spec=s) for s in specs}
    run.checkpoints = [CheckpointState(task_id=s.id, reason=s.checkpoint)
                       for s in specs if s.checkpoint]
    run.stage = "tasks" if run.autonomous else "approve"
    rundir.write_state(run)


def apply_direct_brief(rundir: RunDir, run: RunState, brief_text: str) -> None:
    """Compile an accepted low-risk brief into one owner task without an LLM plan."""
    first = next((line.strip().lstrip("#").strip() for line in brief_text.splitlines()
                  if line.strip()), "Implement the requested change")
    title = first[:120]
    spec_dir = rundir.path / "spec"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.md").write_text(
        "# Direct brief contract\n\n" + brief_text.strip() + "\n"
    )
    spec = TaskSpec(
        id="T1",
        title=title,
        profile="implementer",
        criteria=[brief_text.strip()],
        planned_tests=["Add or adapt the smallest focused regression tests needed by the brief."],
        checklist=[
            "Every explicit brief requirement is covered by implementation evidence.",
            "Focused regression tests prove the changed behavior.",
            "The solution reuses existing code or platform capabilities before adding machinery.",
            "No unrelated refactor, speculative abstraction, or unnecessary dependency was added.",
        ],
        execution="direct",
    )
    run.tasks = {spec.id: TaskState(spec=spec, stage="build")}
    run.stage = "tasks"
    rundir.append_event({
        "kind": "workflow_routed",
        "task": spec.id,
        "stage": "intake",
        "route": "direct",
        "reason": run.route_reason,
    })
    rundir.write_state(run)


def plan_stage(rundir: RunDir, run: RunState, cfg: RegieConfig, worktree: Path) -> None:
    """Advance the run from stage "plan" to "approve" (or "tasks" when
    run.autonomous). Mirrors run_task's dispatch/ladder shape over
    run.planner_attempts, using the pseudo-task dir tasks/PLAN/."""
    brief_text = (rundir.path / "brief.md").read_text()
    decisions_path = rundir.path / "decisions.md"
    decisions = decisions_path.read_text() if decisions_path.exists() else ""
    conventions = _conventions(worktree)
    profile = cfg.profiles["planner"]
    if not run.research_path:
        research_repository(rundir, worktree)
        run.research_path = str(rundir.path / "research.md")
    if cfg.workflow.knowledge and not run.knowledge_snapshot:
        selected = prime_knowledge(rundir, worktree, None, "planning")
        run.knowledge_snapshot = [entry.id for entry in selected]
    rundir.write_state(run)

    while True:
        attempts = run.planner_attempts
        binding = profile.primary
        if attempts:
            action, binding = next_action(attempts, profile.bindings)
            if action == "halt":
                contract_attempts = sum(
                    attempt.failure_kind == "contract" for attempt in attempts
                )
                if attempts[-1].failure_kind == "contract":
                    recovery_revisions = (
                        _MAX_PRODUCT_OWNER_REVISIONS
                        if run.product_owner_decision is not None
                        and run.product_owner_decision.action == "revise"
                        else 0
                    )
                    if contract_attempts < (
                        _MAX_PLAN_CONTRACT_ATTEMPTS + recovery_revisions
                    ):
                        # Provider failover and plan convergence are separate
                        # budgets. A valid provider response rejected by preflight
                        # or review must get a bounded repair pass even when quota
                        # skips already consumed the routing ladder.
                        binding = attempts[-1].binding
                    else:
                        _halt_run(
                            rundir,
                            run,
                            "plan validation did not converge after "
                            f"{contract_attempts} reviewed drafts",
                        )
                        return
                else:
                    _halt_run(
                        rundir,
                        run,
                        _ladder_halt_reason("plan", attempts[-1], _PLAN_TASK_ID),
                    )
                    return

        extra = _notes_for(rundir, _PLAN_TASK_ID, "plan")
        packet = _render_plan_packet(
            brief_text, conventions, decisions, extra,
            context_budget=profile.token_policy.context_chars,
            artifacts={"full brief": str(rundir.path / "brief.md"),
                       "decisions": str(decisions_path),
                       "repository research": run.research_path,
                       "knowledge prime": str(rundir.path / "knowledge-prime.md"),
                       **{f"repository rules ({p.name})": str(p)
                          for p in _convention_paths(worktree)}})
        write_packet(rundir.task_dir(_PLAN_TASK_ID), packet)

        req = AgentRequest(prompt=packet, instructions=profile.prompt_text(), cwd=worktree,
                           binding=binding, budgets=profile.budgets,
                           token_policy=profile.token_policy,
                           output_schema=_plan_schema(cfg))
        attempt = Attempt(binding=binding, prompt_hash=profile.prompt_hash())
        result = run_agent(rundir, _PLAN_TASK_ID, "plan", len(attempts) + 1, req)
        attempt.outcome = {"done": "done", "blocked": "blocked",
                           "quota": "quota"}.get(result.outcome, "failed")
        attempt.blocked_question = result.blocked_question
        attempt.usage, attempt.metrics, attempt.turns = result.usage, result.metrics, result.turns
        if attempt.outcome == "failed":
            attempt.failure_kind = (
                result.failure_kind or classify_agent_failure(result.text)
            )
            attempt.failure_signature = _failure_signature(attempt.failure_kind, result.text)
        attempts.append(attempt)
        rundir.write_state(run)

        if attempt.outcome == "quota":
            continue
        if attempt.outcome == "blocked":
            _halt_run(rundir, run, f"blocked: {attempt.blocked_question or ''}")
            return
        if attempt.outcome == "failed":
            continue

        deterministic_errors = _validate_plan(result.structured, cfg)
        prospective_tasks = ([TaskSpec(**raw) for raw in result.structured["tasks"]]
                             if not deterministic_errors and result.structured else [])
        advanced_profiles = any(name in cfg.profiles for name in _PLAN_LENSES)
        if advanced_profiles:
            deterministic_errors.extend(plan_preflight(prospective_tasks))
        review_errors: list[str] = []
        if not deterministic_errors:
            review_errors = _run_plan_reviews(
                rundir, run, cfg, worktree, brief_text, result.structured)
        errors = [*deterministic_errors, *review_errors]
        if errors:
            attempt.outcome = "failed"
            attempt.failure_kind = "contract"
            attempt.failure_signature = _failure_signature("contract", "\n".join(errors))
            _write_note(rundir, _PLAN_TASK_ID, "plan",
                       "Previous plan failed validation:\n" + "\n".join(
                           f"- {e}" for e in errors))
            rundir.write_state(run)
            contract_attempts = sum(
                item.failure_kind == "contract" for item in run.planner_attempts
            )
            if contract_attempts >= _MAX_PLAN_CONTRACT_ATTEMPTS:
                if run.product_owner_decision is not None:
                    _halt_run(
                        rundir,
                        run,
                        "plan validation did not converge after Product Owner recovery",
                    )
                    return
                decision, po_error = _run_product_owner(
                    rundir,
                    run,
                    cfg,
                    worktree,
                    brief_text,
                    result.structured or {},
                    deterministic_errors,
                    review_errors,
                )
                if decision is None:
                    _halt_run(
                        rundir,
                        run,
                        "plan validation did not converge after "
                        f"{contract_attempts} reviewed drafts; {po_error}",
                    )
                    return
                if decision.action == "revise":
                    if not decision.directives:
                        _halt_run(
                            rundir,
                            run,
                            "Product Owner requested revision without directives",
                        )
                        return
                    _write_note(
                        rundir,
                        _PLAN_TASK_ID,
                        "plan",
                        "Product Owner recovery directives (one final revision):\n"
                        + "\n".join(f"- {item}" for item in decision.directives)
                        + "\n\nUnresolved validation evidence:\n"
                        + "\n".join(f"- {item}" for item in errors),
                    )
                    continue
                if decision.action == "accept":
                    if deterministic_errors:
                        _halt_run(
                            rundir,
                            run,
                            "Product Owner cannot accept a plan with deterministic "
                            "validation failures",
                        )
                        return
                    if any(
                        not item.startswith("plan-scope:") for item in review_errors
                    ):
                        _halt_run(
                            rundir,
                            run,
                            "Product Owner cannot accept mandatory feasibility, "
                            "completeness, design, or reviewer-availability failures",
                        )
                        return
                    _apply_plan(rundir, run, result.structured)
                    run.workflow = resolve_tier(run, cfg)
                    rundir.write_state(run)
                    return
                if decision.action == "ask_human":
                    question = decision.human_question or decision.summary
                    _halt_run(
                        rundir, run, f"Product Owner requests human decision: {question}"
                    )
                    return
                _halt_run(rundir, run, f"Product Owner halted recovery: {decision.summary}")
                return
            continue

        _apply_plan(rundir, run, result.structured)
        run.workflow = resolve_tier(run, cfg)
        rundir.write_state(run)
        return


def run_tasks_stage(rundir: RunDir, run: RunState, cfg: RegieConfig,
                    repo: Path) -> None:
    ctx = PipelineContext(
        spec_excerpt=(rundir.path / "spec" / "spec.md").read_text()
        if (rundir.path / "spec" / "spec.md").exists() else "",
        spec_path=rundir.path / "spec" / "spec.md",
        decisions_path=rundir.path / "decisions.md",
        conventions=_conventions(repo),
        convention_paths=_convention_paths(repo))
    for layer in run.task_layers():
        pending = [task_id for task_id in layer
                   if run.tasks[task_id].status != "done"]
        if not pending:
            continue
        batch_specs = [run.tasks[task_id].spec for task_id in pending]
        can_parallel = (cfg.workflow.max_parallel_tasks > 1 and len(pending) > 1
                        and not scopes_overlap(batch_specs))
        if can_parallel:
            _run_parallel_batch(rundir, run, cfg, repo, ctx, pending)
        else:
            for task_id in pending:
                run_task(rundir, run, task_id, cfg, repo, ctx)
                if run.stage != "tasks":
                    return
        if run.stage != "tasks":
            return
        checkpoint = next((item for item in run.checkpoints
                           if item.status == "pending"
                           and run.tasks[item.task_id].status == "done"), None)
        if checkpoint:
            report = checkpoint_report(run.tasks[checkpoint.task_id].spec, repo)
            (rundir.path / "checkpoint.md").write_text(report)
            run.stage = "checkpoint"
            rundir.write_state(run)
            return
    run.stage = "finalize"
    rundir.write_state(run)


def _run_parallel_batch(rundir: RunDir, run: RunState, cfg: RegieConfig,
                        repo: Path, ctx: PipelineContext,
                        task_ids: list[str]) -> None:
    """Execute one independent DAG layer in isolated task worktrees."""
    base = head_sha(repo)
    task_worktrees: dict[str, tuple[Path, str]] = {}
    root = rundir.path / "task-worktrees"
    root.mkdir(exist_ok=True)
    try:
        for task_id in task_ids:
            slug = re.sub(r"[^a-zA-Z0-9-]+", "-", task_id).strip("-").lower()
            branch = f"{run.branch}-task-{slug}"
            path = root / slug
            create_run_worktree(repo, branch, base, path)
            task_worktrees[task_id] = (path, branch)

        workers = min(cfg.workflow.max_parallel_tasks, len(task_ids))
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="regie-task") as pool:
            futures = {
                pool.submit(
                    run_task, rundir, run, task_id, cfg, path,
                    PipelineContext(
                        spec_excerpt=ctx.spec_excerpt, spec_path=ctx.spec_path,
                        decisions_path=ctx.decisions_path,
                        conventions=_conventions(path),
                        convention_paths=_convention_paths(path))): task_id
                for task_id, (path, _branch) in task_worktrees.items()
            }
            for future in as_completed(futures):
                task_id = futures[future]
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001 - convert worker crash to run halt
                    _halt_run(rundir, run, f"parallel task {task_id} crashed: {exc}")

        if run.stage == "halted" or any(
                run.tasks[task_id].status != "done" for task_id in task_ids):
            return
        for task_id in sorted(task_ids):
            path, _branch = task_worktrees[task_id]
            commits = git(path, "rev-list", "--reverse", f"{base}..HEAD").splitlines()
            try:
                for commit in commits:
                    cherry_pick(repo, commit)
            except GitError as exc:
                try:
                    git(repo, "cherry-pick", "--abort")
                except GitError:
                    pass
                _halt_run(rundir, run,
                          f"parallel integration conflict for {task_id}: {exc}")
                return
        rundir.write_state(run)
    finally:
        for path, branch in task_worktrees.values():
            try:
                remove_run_worktree(repo, path)
            except GitError:
                pass
            try:
                delete_branch(repo, branch)
            except GitError:
                pass


def _is_ancestor(worktree: Path, maybe_ancestor: str, ref: str) -> bool:
    """True if `maybe_ancestor` is already reachable from `ref` (HEAD sits on
    top of it), so no rebase is needed."""
    try:
        git(worktree, "merge-base", "--is-ancestor", maybe_ancestor, ref)
        return True
    except GitError:
        return False


def _rebase_conflicts(worktree: Path) -> list[str]:
    try:
        out = git(worktree, "diff", "--name-only", "--diff-filter=U")
    except GitError:
        return []
    return [p for p in out.splitlines() if p]


def _drift_count(worktree: Path, base_sha: str, base_ref: str) -> int:
    try:
        out = git(worktree, "rev-list", "--count", f"{base_sha}..{base_ref}")
        return int(out.strip() or "0")
    except (GitError, ValueError):
        return 0


def finalize_stage(rundir: RunDir, run: RunState, cfg: RegieConfig,
                   worktree: Path) -> None:
    """Advance the run from stage "finalize" to "pr": full command gates, the
    eval predicate, then rebase onto the base branch. Debugger rounds on gate
    failure only exist at the PR stage in v1 -- any failure here halts."""
    # Every artifact that passed gates was already committed by its stage via
    # commit_all. Anything still uncommitted here is by definition ungated
    # (e.g. agent-invocation scaffolding) and must never enter the squashed
    # PR -- discard it, matching reconcile's discard idiom, rather than
    # committing it.
    git(worktree, "checkout", "--", ".")
    git(worktree, "clean", "-fd")

    gates = []
    if "build" in cfg.commands:
        gates.append(run_command_gate("build", cfg.commands["build"], worktree))
    gates.extend([
        run_command_gate("test", cfg.commands["test"], worktree,
                         rerun_on_fail=True),
        run_command_gate("lint", cfg.commands["lint"], worktree),
    ])
    gates.extend(_policy_gates(
        run, cfg, worktree, "finalize", run.base_sha, include_build=False))
    _record_gate_events(rundir, "FINALIZE", "finalize", gates)
    for gate in gates:
        if not gate.passed:
            _halt_run(rundir, run, f"{gate.name} gate failed: {gate.detail[:500]}")
            return

    changed = [p for p in
              git(worktree, "diff", "--name-only", f"{run.base_sha}..HEAD").splitlines()
              if p]
    if cfg.commands.get("eval") and any(match_globs(p, cfg.eval_trigger_globs)
                                        for p in changed):
        eval_gate = run_command_gate("eval", cfg.commands["eval"], worktree)
        _record_gate_events(rundir, "FINALIZE", "finalize", [eval_gate])
        if not eval_gate.passed:
            _halt_run(rundir, run, f"eval gate failed: {eval_gate.detail[:500]}")
            return

    final_error = _run_final_review(rundir, run, cfg, worktree)
    if final_error:
        _halt_run(rundir, run, final_error)
        return

    git(worktree, "fetch", "origin")
    base_ref = f"origin/{run.base_branch}"
    # Idempotent rebase: if the branch is already on top of the current base
    # (e.g. a human resolved the conflict in the worktree and ran resume), do
    # NOT rebase again -- just refresh the pinned base and proceed. This is the
    # fix for the 2026-07-30 dogfood pain, where a manual resolve could not be
    # cleanly resumed because finalize insisted on re-rebasing.
    if not _is_ancestor(worktree, base_ref, "HEAD"):
        try:
            git(worktree, "rebase", base_ref)
        except GitError:
            conflicted = _rebase_conflicts(worktree)
            drift = _drift_count(worktree, run.base_sha, base_ref)
            try:
                git(worktree, "rebase", "--abort")
            except GitError:
                pass  # best-effort -- the halt below is what matters
            _halt_run(rundir, run,
                      f"rebase conflict ({drift} new commit(s) on "
                      f"{run.base_branch}) in: {', '.join(conflicted) or '?'}. "
                      f"Resolve in {worktree} (git rebase {base_ref}, fix, "
                      f"--continue) then regie resume.")
            return

    run.base_sha = git(worktree, "rev-parse", base_ref).strip()
    if cfg.workflow.submit_pr:
        run.stage = "pr"
    else:
        run.stage = "reflect" if cfg.workflow.reflection else "done"
    rundir.write_state(run)


def _run_final_review(rundir: RunDir, run: RunState, cfg: RegieConfig,
                      worktree: Path) -> str | None:
    if (not cfg.workflow.final_review or resolve_tier(run, cfg) == "fast"
            or "integration-reviewer" not in cfg.profiles):
        return None
    if run.final_review_attempts and run.final_review_attempts[-1].outcome == "done":
        return None
    profile = cfg.profiles["integration-reviewer"]
    packet = "\n\n".join([
        "# Final cross-task integration review",
        "## Spec\n" + _spec_text(rundir),
        "## Combined change\n" + _change_manifest(worktree, run.base_sha),
        ("## Mandate\nCheck cross-task contracts, missing wiring, duplicated "
         "abstractions, incompatible assumptions, and regressions. Do not edit."),
    ]) + "\n"
    write_packet(rundir.task_dir("FINAL-REVIEW"), packet)
    result = None
    last_outcome = "unavailable"
    first_attempt = len(run.final_review_attempts) + 1
    for offset, binding in enumerate(profile.bindings):
        candidate = run_agent(
            rundir, "FINAL-REVIEW", "final-review", first_attempt + offset,
            AgentRequest(
                prompt=packet,
                instructions=profile.prompt_text(),
                cwd=worktree,
                binding=binding,
                budgets=profile.budgets,
                token_policy=profile.token_policy,
                output_schema=SPECIALIST_SCHEMA,
            ),
        )
        last_outcome = candidate.outcome
        attempt = Attempt(
            binding=binding,
            prompt_hash=profile.prompt_hash(),
            outcome={"done": "done", "quota": "quota", "blocked": "blocked"}.get(
                candidate.outcome, "failed"),
            turns=candidate.turns,
            usage=candidate.usage,
            metrics=candidate.metrics,
        )
        run.final_review_attempts.append(attempt)
        rundir.write_state(run)
        if candidate.outcome == "done" and candidate.structured:
            result = candidate
            break
    if result is None:
        return f"final integration review unavailable: {last_outcome}"
    findings = [Finding(**raw) for raw in result.structured.get("findings", [])]
    serious = [finding for finding in findings
               if finding.severity in {"blocker", "major"}]
    if serious:
        _append_json(rundir.task_dir("FINAL-REVIEW") / "findings.json", serious)
        return "final integration review failed: " + "; ".join(
            finding.title for finding in serious)
    return None


def reconcile(rundir: RunDir, run: RunState, repo: Path) -> int:
    """Resume reconciliation: any WAL intent without a recorded attempt means we
    crashed mid-dispatch — mark it failed and discard uncommitted worktree edits."""
    from collections import Counter

    intents = Counter()
    bindings: dict[tuple[str, str], dict] = {}
    for rec in rundir.read_intents():
        # A reset marker zeroes a task's WAL history: written whenever
        # attempts are intentionally cleared (halted-reset, operator surgery).
        # Without it, historic intents resurrect as phantom "orphans" on every
        # later resume and instantly re-exhaust the fresh ladder (dogfood
        # finding: 34 synthetic attempts fabricated after a state reset).
        if rec.get("reset"):
            for key in [k for k in intents if k[0] == rec["task"]]:
                del intents[key]
            continue
        key = (rec["task"], rec["stage"])
        intents[key] += 1
        bindings[key] = rec.get("binding", {"cli": "fake", "model": "?"})
    fixed = 0
    for (task_id, stage), count in intents.items():
        if task_id == _PLAN_TASK_ID:
            attempts = run.planner_attempts
        elif task_id == _PRODUCT_OWNER_TASK_ID:
            attempts = run.product_owner_attempts
        elif task_id in run.tasks:
            attempts = run.tasks[task_id].attempts[stage]
        else:
            continue  # stale intent referencing a task that no longer exists
        while len(attempts) < count:
            attempts.append(Attempt(binding=Binding(**bindings[(task_id, stage)]),
                                    outcome="failed"))
            fixed += 1
    if fixed:
        git(repo, "checkout", "--", ".")
        git(repo, "clean", "-fd")
        rundir.write_state(run)
    return fixed


def _conventions(repo: Path) -> str:
    return "\n\n".join(path.read_text() for path in _convention_paths(repo))


def _convention_paths(repo: Path) -> list[Path]:
    return [repo / name for name in ("CLAUDE.md", "AGENTS.md")
            if (repo / name).exists()]


def _spec_text(rundir: RunDir) -> str:
    path = rundir.path / "spec" / "spec.md"
    return path.read_text() if path.exists() else ""


REGIE_TRAILER = "Co-authored-by: Régie <regie@noreply.local>"


def _fallback_title(spec_text: str, run_id: str) -> str:
    # Prefer the first CONTENT line: bare headings like "## Goal" made awful
    # PR titles (smoke-test finding). Headings are kept only as a last resort.
    first_heading = ""
    for line in spec_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            first_heading = first_heading or stripped.lstrip("#").strip()
            continue
        return stripped[:72]
    return first_heading or run_id


def _scribe(rundir: RunDir, run: RunState, cfg: RegieConfig, worktree: Path,
           groups: list[tuple[str, list[str]]]) -> tuple[list[str], str, str]:
    """Derive commit/PR copy without spending a planner-grade invocation."""
    del cfg, worktree
    spec_text = _spec_text(rundir)
    return [g[0] for g in groups], _fallback_title(spec_text, run.id), spec_text


def _append_minors(rundir: RunDir, body: str) -> str:
    tasks_dir = rundir.path / "tasks"
    lines: list[str] = []
    if tasks_dir.is_dir():
        for task_dir in sorted(tasks_dir.iterdir()):
            findings_path = task_dir / "minor-findings.json"
            if not findings_path.exists():
                continue
            for finding in json.loads(findings_path.read_text()):
                lines.append(f"- [{task_dir.name}] {finding.get('title', '')}: "
                            f"{finding.get('detail', '')}")
    if not lines:
        return body
    return body + "\n\n## Review notes (minor)\n" + "\n".join(lines) + "\n"


def _debugger_profile(cfg: RegieConfig) -> Profile:
    """The debugger is a builder variant, dispatched only from the PR stage's
    CI-red path. If the target repo's own profiles dir doesn't define one
    (e.g. fixtures that predate this feature), fall back to the builder's
    full bindings list/budgets paired with the packaged debugger prompt."""
    if "debugger" in cfg.profiles:
        return cfg.profiles["debugger"]
    builder = cfg.profiles["builder"]
    return Profile(name="debugger", bindings=builder.bindings,
                   prompt_path=_DEBUGGER_PROMPT_FALLBACK, budgets=builder.budgets,
                   token_policy=builder.token_policy)


def _debug_review_binding(debugger_binding: Binding, cfg: RegieConfig) -> Binding:
    """Same cross-model rule as _review_binding, applied against the
    debugger's binding rather than a task's build attempts."""
    reviewer = cfg.profiles["reviewer"].primary
    if debugger_binding.cli == reviewer.cli:
        return cfg.profiles["builder"].primary
    return reviewer


def _debug_review_profile(debugger_binding: Binding, cfg: RegieConfig) -> Profile:
    """Full cross-provider review ladder for a debugger result."""
    reviewer = cfg.profiles["reviewer"]
    return (cfg.profiles["builder"]
            if debugger_binding.cli == reviewer.primary.cli else reviewer)


def _discard_worktree_scratch(worktree: Path) -> None:
    git(worktree, "checkout", "--", ".")
    git(worktree, "clean", "-fd")


def _debugger_round(rundir: RunDir, run: RunState, cfg: RegieConfig, worktree: Path,
                    round_no: int, failure_detail: str) -> bool:
    """One gated debugger round: dispatch → test/lint/diff_gate → commit →
    reviewer dispatch. Returns whether the round produced a pushed fix; any
    failure path rolls the worktree all the way back to how it stood at
    round start -- no dirty scratch, and (once a fix has been committed) no
    orphaned fix(ci) commit left behind by a rejecting reviewer."""
    task_id = f"DEBUG-{round_no}"
    pre_round_sha = head_sha(worktree)
    profile = _debugger_profile(cfg)
    packet = (f"# CI failure — debugger round {round_no}\n\n"
             f"## Notes\n{failure_detail}\n")
    write_packet(rundir.task_dir(task_id), packet)
    pre_dispatch = set(changed_files(worktree))
    result = None
    debugger_binding = None
    for attempt_no, binding in enumerate(profile.bindings, 1):
        req = AgentRequest(
            prompt=packet, instructions=profile.prompt_text(), cwd=worktree,
            binding=binding, budgets=profile.budgets,
            token_policy=profile.token_policy, allowed_commands=cfg.commands)
        candidate = run_agent(rundir, task_id, "debug", attempt_no, req)
        if candidate.outcome == "quota":
            _discard_worktree_scratch(worktree)
            continue
        result, debugger_binding = candidate, binding
        break
    if result is None:
        _halt_run(rundir, run, f"quota exhausted across debugger providers in round {round_no}")
        return False
    if result.outcome != "done" or debugger_binding is None:
        _discard_worktree_scratch(worktree)
        return False

    gates = [run_command_gate("test", cfg.commands["test"], worktree, rerun_on_fail=True),
             run_command_gate("lint", cfg.commands["lint"], worktree),
             diff_gate(worktree, cfg.test_globs)]
    _record_gate_events(rundir, task_id, "debug", gates)
    if not all(g.passed for g in gates):
        _discard_worktree_scratch(worktree)
        return False
    if set(changed_files(worktree)) - pre_dispatch:
        commit_all(worktree,
                   f"fix(ci): debugger round {round_no}\n\n{REGIE_TRAILER}")

    reviewer = _debug_review_profile(debugger_binding, cfg)
    review_result = None
    first_review_attempt = len(profile.bindings) + 1
    for offset, binding in enumerate(reviewer.bindings):
        review_req = AgentRequest(
            prompt=packet, instructions=reviewer.prompt_text(), cwd=worktree,
            binding=binding, budgets=reviewer.budgets,
            token_policy=reviewer.token_policy, output_schema=FINDINGS_SCHEMA)
        candidate = run_agent(
            rundir, task_id, "review", first_review_attempt + offset, review_req)
        if candidate.outcome == "quota":
            continue
        review_result = candidate
        break
    if review_result is None:
        # The fix(ci) commit above is already part of history at this
        # point -- a quota-halted reviewer must not leave it there forever.
        git(worktree, "reset", "--hard", pre_round_sha)
        git(worktree, "clean", "-fd")
        _halt_run(rundir, run,
                  f"quota exhausted across review providers in debugger round {round_no}")
        return False
    findings = [Finding(**f) for f in (review_result.structured or {}).get("findings", [])]
    rejected = (review_result.outcome != "done"
               or any(f.severity in ("blocker", "major") for f in findings))
    if rejected:
        # The fix(ci) commit above is already part of history at this
        # point -- a rejecting reviewer must not leave it there forever.
        git(worktree, "reset", "--hard", pre_round_sha)
        git(worktree, "clean", "-fd")
        return False

    push_branch(worktree, run.branch)
    return True


def _ci_loop(rundir: RunDir, run: RunState, cfg: RegieConfig, worktree: Path) -> None:
    debug_round = 0
    started = time.monotonic()
    run.pr_state.status = "monitoring"
    while True:
        status = ci_status(worktree)
        snapshot = pr_snapshot(worktree)
        run.pr_state.ci = status
        run.pr_state.review_decision = snapshot.get("reviewDecision") or ""
        run.pr_state.unresolved_threads = int(snapshot.get("unresolvedThreads") or 0)
        run.pr_state.last_comment_id = snapshot.get("lastCommentId") or ""
        run.pr_state.updated_at = datetime.now(UTC).isoformat()
        if snapshot.get("state") == "MERGED":
            run.pr_state.status = "merged"
            run.stage = "reflect" if cfg.workflow.reflection else "done"
            rundir.write_state(run)
            return
        review_blocked = (run.pr_state.review_decision == "CHANGES_REQUESTED"
                          or run.pr_state.unresolved_threads > 0)
        if status == "green" and not review_blocked:
            run.pr_state.status = "ready"
            run.stage = "reflect" if cfg.workflow.reflection else "done"
            rundir.write_state(run)
            return
        if review_blocked:
            run.pr_state.status = "fixing"
            debug_round += 1
            if debug_round > CI_MAX_DEBUG_ROUNDS:
                run.pr_state.status = "waiting-human"
                _halt_run(rundir, run,
                          "PR review still requests changes after bounded fix rounds")
                return
            detail = pr_feedback(worktree) or "PR has unresolved review feedback"
            _debugger_round(rundir, run, cfg, worktree, debug_round, detail)
            if run.stage == "halted":
                return
            continue
        if status == "red":
            run.pr_state.status = "fixing"
            debug_round += 1
            run.pr_state.debug_rounds = debug_round
            if debug_round > CI_MAX_DEBUG_ROUNDS:
                _halt_run(rundir, run,
                         f"CI red after {CI_MAX_DEBUG_ROUNDS} debugger rounds")
                return
            _debugger_round(rundir, run, cfg, worktree, debug_round, ci_failures(worktree))
            if run.stage == "halted":
                return
            continue
        if time.monotonic() - started >= CI_WALL_MINUTES * 60:
            run.pr_state.status = "waiting-human"
            _halt_run(rundir, run, "CI timeout")
            return
        time.sleep(CI_POLL_SECONDS)


def pr_stage(rundir: RunDir, run: RunState, cfg: RegieConfig, worktree: Path) -> None:
    """Advance the run from stage "pr" to "done": squash each task's commits
    into one per group (scribe-polished, deterministic fallback on scribe
    failure), open the PR, then watch CI -- gating up to CI_MAX_DEBUG_ROUNDS
    debugger rounds on red before halting.

    Re-entrant once run.pushed is set: a resume after a halt that occurred
    at or after the first push (e.g. mid CI-watch) must not re-squash
    already-pushed history or attempt a second `gh pr create` -- it goes
    straight to the CI loop against the existing pr_url."""
    if not run.pushed:
        groups = run_commit_groups(worktree, run.base_sha)
        if not groups:
            _halt_run(rundir, run, "nothing to submit")
            return

        messages, title, body = _scribe(rundir, run, cfg, worktree, groups)
        messages = [m.rstrip() + "\n\n" + REGIE_TRAILER for m in messages]
        body = _append_minors(rundir, body)
        body_file = rundir.path / "pr-body.md"
        body_file.write_text(body)

        # Scribe only reads and returns structured copy -- it must never leave
        # anything in the tree. Discard any incidental scratch (matching
        # finalize_stage's discard idiom) so rebuild_history's dirty-worktree
        # guard only ever trips on a genuine defect.
        _discard_worktree_scratch(worktree)

        rebuild_history(worktree, run.base_sha,
                        list(zip(messages, [shas for _, shas in groups], strict=True)), run.id)

        # The spec travels WITH the PR (user-confirmed must-have): reviewers
        # see intent and implementation in one diff, and the target repo keeps
        # the decision record after merge. Committed after the squash so the
        # rewrite's tree-identity check stays a pure task-content invariant.
        spec_text = _spec_text(rundir)
        if spec_text:
            spec_dir = worktree / "specs"
            spec_dir.mkdir(exist_ok=True)
            (spec_dir / f"{run.id}.md").write_text(spec_text)
            commit_all(worktree,
                       f"docs(spec): {run.id}\n\n{REGIE_TRAILER}")

        push_branch(worktree, run.branch)
        run.pr_url = create_pr(worktree, run.base_branch, title, body_file)
        run.pushed = True
        rundir.write_state(run)

    _ci_loop(rundir, run, cfg, worktree)
    if run.stage == "reflect":
        reflect_stage(rundir, run, cfg)


def reflect_stage(rundir: RunDir, run: RunState, cfg: RegieConfig) -> None:
    if cfg.workflow.reflection:
        propose_learnings(rundir, run)
    run.stage = "done"
    rundir.write_state(run)
    if run.parent_id:
        try:
            parent_dir = RunDir.open(rundir.path.parents[1], run.parent_id)
            parent = parent_dir.read_state()
            for child in parent.children:
                if child.run_id == run.id:
                    child.status = "done"
            parent_dir.write_state(parent)
        except FileNotFoundError:
            pass
