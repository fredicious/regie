import json
from pathlib import Path
from types import SimpleNamespace

from regie.agents.base import AgentResult
from regie.config import load_config
from regie.models import (
    CheckpointState,
    Finding,
    ProductOwnerDecision,
    RunState,
    TaskSpec,
    TaskState,
)
from regie.pipeline import (
    _finding_signature,
    _handle_serious_findings,
    _plan_review_names,
    _run_execution_product_owner,
    _run_final_review,
    _run_plan_reviews,
    _same_finding,
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
                   base_sha="HEAD", workflow="standard",
                   tasks={"T1": TaskState(spec=_task()),
                          "T2": TaskState(spec=TaskSpec(
                              id="T2", title="Wire calculation", profile="builder",
                              criteria=["Given input When wired Then output"],
                              file_scope=["src/wire.py"], checklist=["wiring works"]))})
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
                   base_sha="HEAD", workflow="standard",
                   tasks={"T1": TaskState(spec=_task()),
                          "T2": TaskState(spec=TaskSpec(
                              id="T2", title="Wire calculation", profile="builder",
                              criteria=["Given input When wired Then output"],
                              file_scope=["src/wire.py"], checklist=["wiring works"]))})
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


def test_final_integration_review_skips_single_task(
        regie_home, fixture_repo, monkeypatch):
    rundir = RunDir.create(regie_home, "single")
    run = RunState(
        id="single", target_repo=str(fixture_repo), branch="regie/single",
        base_sha="HEAD", workflow="standard",
        tasks={"T1": TaskState(spec=_task(risk_tags=["migration"]))},
    )
    cfg = _cfg(fixture_repo)
    calls = []
    monkeypatch.setattr(
        "regie.pipeline.run_agent", lambda *_args, **_kwargs: calls.append(True)
    )

    assert _run_final_review(rundir, run, cfg, fixture_repo) is None
    assert calls == []


def test_product_owner_arbitrates_second_execution_revision(
        regie_home, fixture_repo, monkeypatch):
    rundir = RunDir.create(regie_home, "execution-po")
    run = RunState(
        id="execution-po", target_repo=str(fixture_repo), branch="regie/execution-po",
        stage="tasks", tasks={"T1": TaskState(spec=_task())},
    )
    cfg = _cfg(fixture_repo)
    calls = []
    decision = ProductOwnerDecision(
        action="revise",
        summary="Preserve future data without disabling later user saves.",
        directives=["Separate the startup rewrite decision from later user edits."],
        rejected_findings=["Supporting rollback to an old bundle is outside the brief."],
    )

    def advise(*args, **_kwargs):
        calls.append(args[-1])
        return decision, None

    monkeypatch.setattr("regie.pipeline._run_execution_product_owner", advise)

    for number in range(1, 3):
        _handle_serious_findings(
            rundir, run, "T1", cfg, fixture_repo,
            [Finding(severity="major", title=f"finding {number}", detail="fix it")],
        )

    task = run.tasks["T1"]
    assert len(calls) == 1
    assert task.review_revisions == 2
    assert task.execution_recovery_used
    assert task.product_owner_decision == decision
    assert task.stage == "build"
    assert "Separate the startup rewrite" in (
        rundir.task_dir("T1") / "note-build.md"
    ).read_text()
    assert "do not re-open explicitly rejected scope" in (
        rundir.task_dir("T1") / "note-review.md"
    ).read_text()


def test_repeated_execution_finding_invokes_product_owner_early(
        regie_home, fixture_repo, monkeypatch):
    rundir = RunDir.create(regie_home, "repeat-po")
    run = RunState(
        id="repeat-po", target_repo=str(fixture_repo), branch="regie/repeat-po",
        stage="tasks", tasks={"T1": TaskState(spec=_task())},
    )
    cfg = _cfg(fixture_repo)
    decision = ProductOwnerDecision(
        action="accept", summary="The repeated finding is outside the contract.",
        rejected_findings=["Rollback compatibility was not requested."],
    )
    calls = []
    monkeypatch.setattr(
        "regie.pipeline._run_execution_product_owner",
        lambda *args, **_kwargs: (calls.append(args[-1]) or decision, None),
    )
    finding = Finding(severity="major", title="rollback compatibility", detail="scope")

    _handle_serious_findings(rundir, run, "T1", cfg, fixture_repo, [finding])
    _handle_serious_findings(rundir, run, "T1", cfg, fixture_repo, [finding])

    assert len(calls) == 1
    assert run.tasks["T1"].status == "done"


def test_finding_signatures_detect_semantic_repeat_without_collapsing_rollback():
    startup_loss = Finding(
        severity="major", file="src/app.js",
        title="Startup destroys data from newer storage versions",
    )
    future_loss = Finding(
        severity="major", file="src/app.js",
        title="Unknown future-version data is immediately destroyed",
    )
    rollback = Finding(
        severity="major", file="src/app.js",
        title="The in-place rewrite makes rollback data-destructive",
    )

    assert _same_finding(startup_loss, future_loss)
    assert not _same_finding(startup_loss, rollback)
    assert _finding_signature(startup_loss) == (
        "src/app.js|data,destroy,future,storage,version"
    )


def test_findings_persist_signatures_and_recovery_telemetry(
        regie_home, fixture_repo):
    rundir = RunDir.create(regie_home, "finding-signature")
    run = RunState(
        id="finding-signature", target_repo=str(fixture_repo),
        branch="regie/finding-signature", stage="tasks",
        tasks={"T1": TaskState(spec=_task())},
    )
    cfg = _cfg(fixture_repo)
    finding = Finding(
        severity="major", file="src/calc.py",
        title="Division silently truncates decimal results", detail="fix it",
    )

    _handle_serious_findings(rundir, run, "T1", cfg, fixture_repo, [finding])

    recorded = json.loads((rundir.task_dir("T1") / "findings.json").read_text())
    event = json.loads((rundir.path / "events.jsonl").read_text().splitlines()[-1])
    assert recorded[0]["signature"] == _finding_signature(finding)
    assert event["kind"] == "review_findings"
    assert event["semantic_repeat"] is False
    assert event["recovery"] is False


def test_execution_product_owner_packet_does_not_duplicate_current_findings(
        regie_home, fixture_repo, monkeypatch):
    rundir = RunDir.create(regie_home, "po-packet")
    (rundir.path / "brief.md").write_text("# Keep the requested behavior bounded")
    (rundir.path / "spec").mkdir()
    (rundir.path / "spec" / "spec.md").write_text("# Accepted spec")
    run = RunState(
        id="po-packet", target_repo=str(fixture_repo), branch="regie/po-packet",
        stage="tasks", tasks={"T1": TaskState(spec=_task(), review_revisions=2)},
    )
    cfg = _cfg(fixture_repo)
    prompts = []
    decision = ProductOwnerDecision(
        action="revise", summary="Apply one coherent repair.",
        directives=["Keep current behavior while fixing the required boundary."],
    )

    def capture(_rd, _task_id, _stage, _attempt, request):
        prompts.append(request.prompt)
        return AgentResult(outcome="done", structured=decision.model_dump())

    monkeypatch.setattr("regie.pipeline.run_agent", capture)
    current = Finding(
        severity="major", title="CURRENT-UNIQUE", detail="current detail",
    )
    prior = [{
        "severity": "major", "title": "PRIOR-UNIQUE", "detail": "prior detail",
        "file": None,
    }]

    actual, error = _run_execution_product_owner(
        rundir, run, "T1", cfg, fixture_repo, [current], prior_findings=prior
    )

    assert error is None and actual == decision
    assert prompts[0].count("CURRENT-UNIQUE") == 1
    assert prompts[0].count("PRIOR-UNIQUE") == 1
