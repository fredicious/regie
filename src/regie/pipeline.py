from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from regie.agents.base import AgentRequest
from regie.config import Profile, RegieConfig
from regie.dispatch import run_agent
from regie.gates import diff_gate, match_globs, red_test_gate, run_command_gate
from regie.gitops import (
    GitError,
    ci_failures,
    ci_status,
    commit_all,
    create_pr,
    git,
    head_sha,
    push_branch,
    rebuild_history,
    run_commit_groups,
)
from regie.ladder import next_action
from regie.models import (
    Attempt,
    Binding,
    CycleError,
    Finding,
    GateResult,
    RunState,
    TaskSpec,
    TaskState,
)
from regie.packets import render_packet, write_packet
from regie.rundir import RunDir

if TYPE_CHECKING:
    from regie.agents.base import AgentResult

FINDINGS_SCHEMA = {"type": "object", "properties": {"findings": {"type": "array"}},
                   "required": ["findings"]}

# Task items are FULLY specified (exact TaskSpec field names, no extras):
# smoke-test finding — with a bare {"type": "array"} the model invents its own
# reasonable-but-wrong field names (predicted_file_scope, dependencies, ...)
# and burns the ladder on pydantic rejections. The CLI's own schema validation
# now forces the shape before we ever see it.
_STR_ARRAY = {"type": "array", "items": {"type": "string"}}
PLAN_SCHEMA = {
    "type": "object", "required": ["spec_markdown", "tasks"],
    "properties": {
        "spec_markdown": {"type": "string"},
        "tasks": {"type": "array", "items": {
            "type": "object",
            "required": ["id", "title", "profile", "criteria", "planned_tests"],
            "additionalProperties": False,
            "properties": {
                "id": {"type": "string"}, "title": {"type": "string"},
                "profile": {"type": "string"}, "criteria": _STR_ARRAY,
                "planned_tests": _STR_ARRAY, "file_scope": _STR_ARRAY,
                "checklist": _STR_ARRAY, "depends_on": _STR_ARRAY,
            }}}}}

SCRIBE_SCHEMA = {"type": "object",
                 "required": ["commit_messages", "pr_title", "pr_body"],
                 "properties": {"commit_messages": {"type": "array"},
                                "pr_title": {"type": "string"},
                                "pr_body": {"type": "string"}}}

CRITERION_RE = re.compile(r"given.+when.+then", re.IGNORECASE | re.DOTALL)

_PLAN_TASK_ID = "PLAN"
_SCRIBE_TASK_ID = "SCRIBE"

_DEBUGGER_PROMPT_FALLBACK = Path(__file__).parent.parent.parent / "profiles" / "debugger.md"

CI_POLL_SECONDS = 30
CI_MAX_DEBUG_ROUNDS = 2
CI_WALL_MINUTES = 30


@dataclass
class PipelineContext:
    spec_excerpt: str
    decisions_path: Path
    conventions: str


def _decisions(ctx: PipelineContext) -> str:
    return ctx.decisions_path.read_text() if ctx.decisions_path.exists() else ""


def _review_binding(run: RunState, task_id: str, cfg: RegieConfig) -> Binding:
    """Cross-model rule: reviewer must not share the builder's model family."""
    reviewer = cfg.profiles["reviewer"].binding
    builds = run.tasks[task_id].attempts["build"]
    if builds and builds[-1].binding.cli == reviewer.cli:
        return cfg.profiles["builder"].binding
    return reviewer


def _dispatch(rundir: RunDir, run: RunState, task_id: str, stage: str,
              profile: Profile, cfg: RegieConfig, repo: Path,
              ctx: PipelineContext, extra: str) -> tuple[Attempt, AgentResult]:
    task = run.tasks[task_id]
    attempts = task.attempts[stage]
    binding = (_review_binding(run, task_id, cfg) if stage == "review"
               else profile.binding)
    if attempts:
        _action, binding = next_action(attempts, attempts[-1].binding,
                                       cfg.binding_strength)
        # caller already checked for halt; retry keeps binding, escalate upgrades
    packet = render_packet(task.spec, ctx.spec_excerpt, _decisions(ctx),
                           ctx.conventions, extra=extra)
    write_packet(rundir.task_dir(task_id), packet)
    req = AgentRequest(prompt=profile.prompt_text() + "\n\n" + packet, cwd=repo,
                       binding=binding, budgets=profile.budgets,
                       output_schema=FINDINGS_SCHEMA if stage == "review" else None)
    attempt = Attempt(binding=binding, prompt_hash=profile.prompt_hash())
    result = run_agent(rundir, task_id, stage, len(attempts) + 1, req)
    attempt.outcome = {"done": "done", "blocked": "blocked",
                       "quota": "quota"}.get(result.outcome, "failed")
    attempt.blocked_question = result.blocked_question
    attempt.usage, attempt.turns = result.usage, result.turns
    attempts.append(attempt)
    return attempt, result


def _halt(rundir: RunDir, run: RunState, task_id: str, reason: str) -> None:
    run.tasks[task_id].status = "failed" if "blocked" not in reason else "blocked"
    run.stage = "halted"
    run.halt_reason = reason
    rundir.write_state(run)


def _should_halt(rundir: RunDir, run: RunState, task_id: str, stage: str,
                 cfg: RegieConfig) -> bool:
    attempts = run.tasks[task_id].attempts[stage]
    if not attempts:
        return False
    action, _ = next_action(attempts, attempts[-1].binding, cfg.binding_strength)
    if action == "halt":
        _halt(rundir, run, task_id, f"{stage} ladder exhausted on {task_id}")
        return True
    return False


def _gate_and_advance(rundir: RunDir, run: RunState, task_id: str, stage: str,
                      gates: list[GateResult], attempt: Attempt, on_pass,
                      repo: Path) -> None:
    attempt.gate_results = gates
    if all(g.passed for g in gates):
        on_pass()
    else:
        attempt.outcome = "failed"
        failed = [g for g in gates if not g.passed]
        _write_note(rundir, task_id, stage,
                    "Previous attempt failed gates:\n" + "\n".join(
                        f"- {g.name}: {g.detail[:1500]}" for g in failed))
        # Discard the failed attempt's uncommitted worktree edits so the next
        # attempt starts from a clean tree instead of building on top of them.
        git(repo, "checkout", "--", ".")
        git(repo, "clean", "-fd")
    rundir.write_state(run)


def run_task(rundir: RunDir, run: RunState, task_id: str, cfg: RegieConfig,
             repo: Path, ctx: PipelineContext,
             max_dispatches: int | None = None) -> None:
    """Advance one task through test → build → review. Test seam: max_dispatches."""
    task = run.tasks[task_id]
    task.status = "running"
    dispatched = 0

    while task.status == "running":
        if max_dispatches is not None and dispatched >= max_dispatches:
            return
        stage = task.stage
        if _should_halt(rundir, run, task_id, stage, cfg):
            return
        extra = _notes_for(rundir, task_id, stage)
        profile = cfg.profiles[{"test": "test-writer", "build": "builder",
                                "review": "reviewer"}[stage]]
        attempt, result = _dispatch(rundir, run, task_id, stage, profile, cfg,
                                    repo, ctx, extra)
        dispatched += 1

        if attempt.outcome == "quota":
            _halt(rundir, run, task_id, f"quota exhausted during {stage}")
            return
        if attempt.outcome == "blocked":
            question = attempt.blocked_question or ""
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
            rundir.write_state(run)
            continue

        if stage == "test":
            gates = [red_test_gate(repo, cfg.commands["test"]),
                     run_command_gate("lint", cfg.commands["lint"], repo)]
            def _pass_test():
                commit_all(repo, f"test({task_id}): red tests for {task.spec.title}")
                task.stage = "build"
            _gate_and_advance(rundir, run, task_id, stage, gates, attempt,
                              _pass_test, repo)
        elif stage == "build":
            gates = [run_command_gate("test", cfg.commands["test"], repo,
                                      rerun_on_fail=True),
                     run_command_gate("lint", cfg.commands["lint"], repo),
                     diff_gate(repo, cfg.test_globs)]
            def _pass_build():
                commit_all(repo, f"feat({task_id}): {task.spec.title}")
                task.stage = "review"
            _gate_and_advance(rundir, run, task_id, stage, gates, attempt,
                              _pass_build, repo)
        else:  # review
            findings = [Finding(**f) for f in
                        (result.structured or {}).get("findings", [])]
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
                        extra: str) -> str:
    return "\n\n".join([
        f"# Brief\n{brief_text}",
        f"## Conventions\n{conventions}",
        f"## Decisions so far\n{decisions or '(none yet)'}",
        f"## Notes\n{extra or '(none)'}",
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


def _apply_plan(rundir: RunDir, run: RunState, structured: dict) -> None:
    spec_dir = rundir.path / "spec"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.md").write_text(structured["spec_markdown"])
    specs = [TaskSpec(**t) for t in structured["tasks"]]
    run.tasks = {s.id: TaskState(spec=s) for s in specs}
    run.stage = "tasks" if run.autonomous else "approve"
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

    while True:
        attempts = run.planner_attempts
        if attempts:
            action, _ = next_action(attempts, attempts[-1].binding, cfg.binding_strength)
            if action == "halt":
                _halt_run(rundir, run, "planner ladder exhausted")
                return

        extra = _notes_for(rundir, _PLAN_TASK_ID, "plan")
        packet = _render_plan_packet(brief_text, conventions, decisions, extra)
        write_packet(rundir.task_dir(_PLAN_TASK_ID), packet)

        binding = profile.binding
        if attempts:
            _action, binding = next_action(attempts, attempts[-1].binding,
                                           cfg.binding_strength)
        req = AgentRequest(prompt=profile.prompt_text() + "\n\n" + packet, cwd=worktree,
                           binding=binding, budgets=profile.budgets,
                           output_schema=PLAN_SCHEMA)
        attempt = Attempt(binding=binding, prompt_hash=profile.prompt_hash())
        result = run_agent(rundir, _PLAN_TASK_ID, "plan", len(attempts) + 1, req)
        attempt.outcome = {"done": "done", "blocked": "blocked",
                           "quota": "quota"}.get(result.outcome, "failed")
        attempt.blocked_question = result.blocked_question
        attempt.usage, attempt.turns = result.usage, result.turns
        attempts.append(attempt)
        rundir.write_state(run)

        if attempt.outcome == "quota":
            _halt_run(rundir, run, "quota exhausted during plan")
            return
        if attempt.outcome == "blocked":
            _halt_run(rundir, run, f"blocked: {attempt.blocked_question or ''}")
            return
        if attempt.outcome == "failed":
            continue

        errors = _validate_plan(result.structured, cfg)
        if errors:
            attempt.outcome = "failed"
            _write_note(rundir, _PLAN_TASK_ID, "plan",
                       "Previous plan failed validation:\n" + "\n".join(
                           f"- {e}" for e in errors))
            rundir.write_state(run)
            continue

        _apply_plan(rundir, run, result.structured)
        return


def run_tasks_stage(rundir: RunDir, run: RunState, cfg: RegieConfig,
                    repo: Path) -> None:
    ctx = PipelineContext(
        spec_excerpt=(rundir.path / "spec" / "spec.md").read_text()
        if (rundir.path / "spec" / "spec.md").exists() else "",
        decisions_path=rundir.path / "decisions.md",
        conventions=_conventions(repo))
    for task_id in run.ordered_task_ids():
        if run.tasks[task_id].status == "done":
            continue
        run_task(rundir, run, task_id, cfg, repo, ctx)
        if run.stage == "halted":
            return
    run.stage = "finalize"
    rundir.write_state(run)


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

    gates = [run_command_gate("test", cfg.commands["test"], worktree,
                              rerun_on_fail=True),
             run_command_gate("lint", cfg.commands["lint"], worktree)]
    if "typecheck" in cfg.commands:
        gates.append(run_command_gate("typecheck", cfg.commands["typecheck"], worktree))
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
        if not eval_gate.passed:
            _halt_run(rundir, run, f"eval gate failed: {eval_gate.detail[:500]}")
            return

    try:
        git(worktree, "fetch", "origin")
        git(worktree, "rebase", f"origin/{run.base_branch}")
    except GitError:
        try:
            git(worktree, "rebase", "--abort")
        except GitError:
            pass  # best-effort -- the halt below is what matters
        _halt_run(rundir, run,
                 f"rebase conflict — resolve manually in {worktree} then regie resume")
        return

    run.stage = "pr"
    rundir.write_state(run)


def reconcile(rundir: RunDir, run: RunState, repo: Path) -> int:
    """Resume reconciliation: any WAL intent without a recorded attempt means we
    crashed mid-dispatch — mark it failed and discard uncommitted worktree edits."""
    from collections import Counter

    intents = Counter()
    bindings: dict[tuple[str, str], dict] = {}
    for rec in rundir.read_intents():
        key = (rec["task"], rec["stage"])
        intents[key] += 1
        bindings[key] = rec.get("binding", {"cli": "fake", "model": "?"})
    fixed = 0
    for (task_id, stage), count in intents.items():
        if task_id == _PLAN_TASK_ID:
            attempts = run.planner_attempts
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
    parts = []
    for name in ("CLAUDE.md", "AGENTS.md"):
        p = repo / name
        if p.exists():
            parts.append(p.read_text())
    return "\n\n".join(parts)


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
    """Best-effort commit-message/PR-copy polish: one dispatch of the planner
    profile, never blocking the run. Any dispatch failure or structural
    mismatch (wrong outcome, missing/mis-sized commit_messages) falls back to
    deterministic output derived from the commit groups and spec -- scribe
    output is local to this call, never recorded onto RunState."""
    spec_text = _spec_text(rundir)
    fallback = ([g[0] for g in groups], _fallback_title(spec_text, run.id), spec_text)

    profile = cfg.profiles["planner"]
    log_subjects = git(worktree, "log", "--reverse", "--format=%s",
                       f"{run.base_sha}..HEAD")
    prompt = "\n\n".join([
        f"# Spec\n{spec_text}",
        # Smoke-test finding: without an explicit count the model writes one
        # message per raw commit (test+feat), not one per task group, and the
        # size check rejects it. State the contract in the prompt.
        (f"## Your job\nWrite a PR title, a PR body, and EXACTLY "
         f"{len(groups)} conventional commit message(s) — one per task group "
         f"listed below, in order. Do NOT write one message per git-log line."),
        ("## Commit message rules (Conventional Commits)\n"
         "- Format: `type(scope): subject` — type from feat/fix/test/refactor/"
         "chore/docs; scope is the MODULE or package the change touches (e.g. "
         "`calc`, `api`, `search`), NEVER a task id like T1.\n"
         "- Subject: imperative mood, lowercase, no trailing period, ≤50 chars "
         "(72 hard max).\n"
         "- After a blank line, an optional short body explaining WHY the "
         "change was made — the diff already shows the what.\n"
         "- PR title follows the same conventional format and summarizes the "
         "whole change."),
        "## Task groups (one commit message each, replacing these defaults)\n" +
        "\n".join(f"{i + 1}. {g[0]}" for i, g in enumerate(groups)),
        f"## git log subjects (context only)\n{log_subjects}",
    ]) + "\n"
    write_packet(rundir.task_dir(_SCRIBE_TASK_ID), prompt)
    req = AgentRequest(prompt=profile.prompt_text() + "\n\n" + prompt, cwd=worktree,
                       binding=profile.binding, budgets=profile.budgets,
                       output_schema=SCRIBE_SCHEMA)
    result = run_agent(rundir, _SCRIBE_TASK_ID, "scribe", 1, req)
    if result.outcome != "done" or not result.structured:
        return fallback

    messages = result.structured.get("commit_messages")
    if not isinstance(messages, list) or len(messages) != len(groups):
        return fallback
    title = result.structured.get("pr_title") or fallback[1]
    body = result.structured.get("pr_body") or fallback[2]
    return list(messages), title, body


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
    binding/budgets paired with the packaged debugger prompt."""
    if "debugger" in cfg.profiles:
        return cfg.profiles["debugger"]
    builder = cfg.profiles["builder"]
    return Profile(name="debugger", binding=builder.binding,
                   prompt_path=_DEBUGGER_PROMPT_FALLBACK, budgets=builder.budgets)


def _debug_review_binding(debugger_binding: Binding, cfg: RegieConfig) -> Binding:
    """Same cross-model rule as _review_binding, applied against the
    debugger's binding rather than a task's build attempts."""
    reviewer = cfg.profiles["reviewer"].binding
    if debugger_binding.cli == reviewer.cli:
        return cfg.profiles["builder"].binding
    return reviewer


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
    req = AgentRequest(prompt=profile.prompt_text() + "\n\n" + packet, cwd=worktree,
                       binding=profile.binding, budgets=profile.budgets)
    result = run_agent(rundir, task_id, "debug", 1, req)
    if result.outcome == "quota":
        _discard_worktree_scratch(worktree)
        _halt_run(rundir, run, f"quota exhausted during debugger round {round_no}")
        return False
    if result.outcome != "done":
        _discard_worktree_scratch(worktree)
        return False

    gates = [run_command_gate("test", cfg.commands["test"], worktree, rerun_on_fail=True),
             run_command_gate("lint", cfg.commands["lint"], worktree),
             diff_gate(worktree, cfg.test_globs)]
    if not all(g.passed for g in gates):
        _discard_worktree_scratch(worktree)
        return False
    commit_all(worktree,
               f"fix(ci): debugger round {round_no}\n\n{REGIE_TRAILER}")

    reviewer = cfg.profiles["reviewer"]
    review_req = AgentRequest(prompt=reviewer.prompt_text() + "\n\n" + packet, cwd=worktree,
                              binding=_debug_review_binding(profile.binding, cfg),
                              budgets=reviewer.budgets, output_schema=FINDINGS_SCHEMA)
    review_result = run_agent(rundir, task_id, "review", 1, review_req)
    if review_result.outcome == "quota":
        # The fix(ci) commit above is already part of history at this
        # point -- a quota-halted reviewer must not leave it there forever.
        git(worktree, "reset", "--hard", pre_round_sha)
        git(worktree, "clean", "-fd")
        _halt_run(rundir, run, f"quota exhausted during debugger round {round_no}")
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
    while True:
        status = ci_status(worktree)
        if status == "green":
            run.stage = "done"
            rundir.write_state(run)
            return
        if status == "red":
            debug_round += 1
            if debug_round > CI_MAX_DEBUG_ROUNDS:
                _halt_run(rundir, run,
                         f"CI red after {CI_MAX_DEBUG_ROUNDS} debugger rounds")
                return
            _debugger_round(rundir, run, cfg, worktree, debug_round, ci_failures(worktree))
            if run.stage == "halted":
                return
            continue
        if time.monotonic() - started >= CI_WALL_MINUTES * 60:
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

        push_branch(worktree, run.branch)
        run.pr_url = create_pr(worktree, run.base_branch, title, body_file)
        run.pushed = True
        rundir.write_state(run)

    _ci_loop(rundir, run, cfg, worktree)
