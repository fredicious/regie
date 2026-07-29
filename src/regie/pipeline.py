from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from regie.agents.base import AgentRequest
from regie.config import Profile, RegieConfig
from regie.dispatch import run_agent
from regie.gates import diff_gate, red_test_gate, run_command_gate
from regie.gitops import commit_all, git
from regie.ladder import next_action
from regie.models import Attempt, Finding, GateResult, RunState
from regie.packets import render_packet, write_packet
from regie.rundir import RunDir

if TYPE_CHECKING:
    from regie.agents.base import AgentResult

FINDINGS_SCHEMA = {"type": "object", "properties": {"findings": {"type": "array"}},
                   "required": ["findings"]}


@dataclass
class PipelineContext:
    spec_excerpt: str
    decisions_path: Path
    conventions: str


def _decisions(ctx: PipelineContext) -> str:
    return ctx.decisions_path.read_text() if ctx.decisions_path.exists() else ""


def _dispatch(rundir: RunDir, run: RunState, task_id: str, stage: str,
              profile: Profile, cfg: RegieConfig, repo: Path,
              ctx: PipelineContext, extra: str) -> tuple[Attempt, AgentResult]:
    task = run.tasks[task_id]
    attempts = task.attempts[stage]
    binding = profile.binding
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
                commit_all(repo, f"test({task_id}): red tests")
                task.stage = "build"
            _gate_and_advance(rundir, run, task_id, stage, gates, attempt,
                              _pass_test, repo)
        elif stage == "build":
            gates = [run_command_gate("test", cfg.commands["test"], repo,
                                      rerun_on_fail=True),
                     run_command_gate("lint", cfg.commands["lint"], repo),
                     diff_gate(repo, cfg.test_globs)]
            def _pass_build():
                commit_all(repo, f"feat({task_id}): implement")
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


def reconcile(rundir: RunDir, run: RunState, repo: Path) -> int:
    """Resume reconciliation: any WAL intent without a recorded attempt means we
    crashed mid-dispatch — mark it failed and discard uncommitted worktree edits."""
    from collections import Counter

    from regie.models import Binding

    intents = Counter()
    bindings: dict[tuple[str, str], dict] = {}
    for rec in rundir.read_intents():
        key = (rec["task"], rec["stage"])
        intents[key] += 1
        bindings[key] = rec.get("binding", {"cli": "fake", "model": "?"})
    fixed = 0
    for (task_id, stage), count in intents.items():
        attempts = run.tasks[task_id].attempts[stage]
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
