from pathlib import Path
from types import SimpleNamespace

from regie.agents.base import AgentResult
from regie.config import load_config
from regie.models import CheckpointState, RunState, TaskSpec, TaskState
from regie.pipeline import (
    _plan_review_names,
    _run_final_review,
    _run_plan_reviews,
    run_tasks_stage,
)
from regie.rundir import RunDir

PROFILES = Path(__file__).parent.parent / "profiles"


def _task(**kwargs):
    return TaskSpec(
        id="T1", title="Add calculation", profile="builder",
        criteria=["Given input When calculated Then output"],
        planned_tests=["test_calculation"], file_scope=["src/calc.py"],
        checklist=["neighboring operations still work"], **kwargs)


def _cfg(fixture_repo):
    (fixture_repo / "regie.toml").write_text(
        'test_globs = ["tests/**"]\n'
        '[workflow]\nmax_parallel_tasks = 1\n'
        '[commands]\ntest = "true"\nlint = "true"\n')
    return load_config(fixture_repo, PROFILES)


def test_standard_single_task_uses_one_generic_plan_lens(
        regie_home, fixture_repo, monkeypatch):
    rundir = RunDir.create(regie_home, "r1")
    run = RunState(id="r1", target_repo=str(fixture_repo), branch="regie/r1",
                   workflow="standard")
    cfg = _cfg(fixture_repo)
    calls = []

    def pass_review(_rd, task_id, stage, _attempt, _request):
        calls.append((task_id, stage))
        return AgentResult(outcome="done", structured={
            "verdict": "pass", "evidence": ["verified against checkout"],
            "findings": [],
        })

    monkeypatch.setattr("regie.pipeline.run_agent", pass_review)
    structured = {"spec_markdown": "# Spec", "tasks": [_task().model_dump()]}

    assert _run_plan_reviews(
        rundir, run, cfg, fixture_repo, "brief", structured) == []
    assert {review.lens for review in run.plan_reviews} == {"plan-completeness"}
    assert len(calls) == 1


def test_explicit_critical_workflow_keeps_full_plan_panel(fixture_repo):
    cfg = _cfg(fixture_repo)

    names = _plan_review_names([_task()], cfg, "critical", "critical")

    assert names == ["plan-feasibility", "plan-completeness", "plan-scope"]


def test_auto_single_migration_uses_only_risk_design_lens(fixture_repo):
    cfg = _cfg(fixture_repo)
    task = _task(risk_tags=["migration"])

    names = _plan_review_names([task], cfg, "critical", "auto")

    assert names == ["architecture-design-reviewer"]


def test_plan_review_fails_over_when_primary_provider_has_quota(
        regie_home, fixture_repo, monkeypatch):
    rundir = RunDir.create(regie_home, "r1")
    run = RunState(id="r1", target_repo=str(fixture_repo), branch="regie/r1",
                   workflow="standard")
    cfg = _cfg(fixture_repo)
    calls = []

    monkeypatch.setattr(
        "regie.pipeline._plan_review_names",
        lambda _tasks, _cfg, _tier, _requested: ["plan-completeness"],
    )

    def quota_then_pass(_rd, _task_id, _stage, attempt_no, request):
        calls.append((attempt_no, request.binding.cli, request.binding.model))
        if len(calls) == 1:
            return AgentResult(outcome="quota")
        return AgentResult(outcome="done", structured={
            "verdict": "pass",
            "evidence": ["fallback verified the plan"],
            "findings": [],
        })

    monkeypatch.setattr("regie.pipeline.run_agent", quota_then_pass)
    structured = {"spec_markdown": "# Spec", "tasks": [_task().model_dump()]}

    assert _run_plan_reviews(
        rundir, run, cfg, fixture_repo, "brief", structured) == []
    assert [cli for _, cli, _ in calls] == ["claude", "codex"]
    assert [number for number, _, _ in calls] == [1, 2]
    assert run.plan_reviews[-1].lens == "plan-completeness"


def test_checkpoint_is_a_blocking_state_transition(
        regie_home, fixture_repo, monkeypatch):
    rundir = RunDir.create(regie_home, "r1")
    spec = _task(checkpoint="Review schema before dependent work")
    run = RunState(id="r1", target_repo=str(fixture_repo), branch="regie/r1",
                   workflow="standard", stage="tasks",
                   tasks={"T1": TaskState(spec=spec)},
                   checkpoints=[CheckpointState(task_id="T1", reason=spec.checkpoint)])

    def complete(_rd, state, task_id, _cfg, _repo, _ctx):
        state.tasks[task_id].status = "done"

    monkeypatch.setattr("regie.pipeline.run_task", complete)
    cfg = SimpleNamespace(workflow=SimpleNamespace(max_parallel_tasks=1))
    run_tasks_stage(rundir, run, cfg, fixture_repo)

    assert run.stage == "checkpoint"
    assert "Review schema" in (rundir.path / "checkpoint.md").read_text()


def test_final_integration_review_is_persisted(
        regie_home, fixture_repo, monkeypatch):
    rundir = RunDir.create(regie_home, "r1")
    (rundir.path / "spec").mkdir()
    (rundir.path / "spec" / "spec.md").write_text("# Spec")
    run = RunState(id="r1", target_repo=str(fixture_repo), branch="regie/r1",
                   base_sha="HEAD", workflow="standard")
    cfg = _cfg(fixture_repo)
    monkeypatch.setattr("regie.pipeline.run_agent", lambda *_args, **_kwargs: AgentResult(
        outcome="done", structured={"findings": []}))

    assert _run_final_review(rundir, run, cfg, fixture_repo) is None
    assert run.final_review_attempts[-1].outcome == "done"
    assert (rundir.task_dir("FINAL-REVIEW") / "context.md").exists()


def test_final_integration_review_fails_over_after_quota(
        regie_home, fixture_repo, monkeypatch):
    rundir = RunDir.create(regie_home, "r1")
    (rundir.path / "spec").mkdir()
    (rundir.path / "spec" / "spec.md").write_text("# Spec")
    run = RunState(id="r1", target_repo=str(fixture_repo), branch="regie/r1",
                   base_sha="HEAD", workflow="standard")
    cfg = _cfg(fixture_repo)
    calls = []

    def quota_then_pass(_rd, _task_id, _stage, _attempt_no, request):
        calls.append(request.binding.cli)
        if len(calls) == 1:
            return AgentResult(outcome="quota")
        return AgentResult(outcome="done", structured={"findings": []})

    monkeypatch.setattr("regie.pipeline.run_agent", quota_then_pass)

    assert _run_final_review(rundir, run, cfg, fixture_repo) is None
    assert calls == ["claude", "codex"]
    assert [attempt.outcome for attempt in run.final_review_attempts] == ["quota", "done"]
