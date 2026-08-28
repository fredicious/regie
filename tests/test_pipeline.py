import json
from pathlib import Path

import pytest

from regie.config import Profile, WorkflowConfig, load_config
from regie.models import (
    Attempt,
    Binding,
    Budgets,
    RunState,
    TaskSpec,
    TaskState,
)
from regie.pipeline import (
    PipelineContext,
    apply_direct_brief,
    run_task,
    run_tasks_stage,
)
from regie.rundir import RunDir

PROFILES_FAKE = None  # built by fixture below


@pytest.fixture
def cfg(fixture_repo, fake_profiles):
    (fixture_repo / "regie.toml").write_text("""
test_globs = ["tests/**"]
[commands]
test = "python -m pytest tests -q"
lint = "true"
""")
    return load_config(fixture_repo, fake_profiles)


def _run_state(repo) -> tuple[RunState, str]:
    spec = TaskSpec(id="T1", title="divide", profile="builder",
                    criteria=["Given 6,3 When divide Then 2"])
    run = RunState(id="r1", target_repo=str(repo), branch="regie/r1", stage="tasks")
    run.tasks["T1"] = TaskState(spec=spec)
    return run, "T1"


def _script(repo, step_results: list[dict]):
    """FakeAdapter reads .fake_agent.json per dispatch; tests queue behaviors by
    rewriting the file between stages via the 'queue' convention: the file holds
    a list, and a sitecustomize-free helper pops item 0 each run."""
    (repo / ".fake_agent.json").write_text(json.dumps(step_results.pop(0)))


RED_TEST = {"result": {"outcome": "done"}, "writes": {
    "tests/test_div.py": "from src.calc import divide\n\n"
                         "def test_div():\n    assert divide(6, 3) == 2\n",
    "src/calc.py": "def add(a, b):\n    return a + b\n\n"
                   "def divide(a, b):\n    raise NotImplementedError\n"}}
GREEN_BUILD = {"result": {"outcome": "done"}, "writes": {
    "src/calc.py": "def add(a, b):\n    return a + b\n\n"
                   "def divide(a, b):\n    return a // b\n"}}
CLEAN_REVIEW = {"result": {"outcome": "done",
                           "structured": {"findings": []}}}


def test_happy_path_test_build_review_done(regie_home, fixture_repo, cfg):
    rd = RunDir.create(regie_home, "r1")
    run, tid = _run_state(fixture_repo)
    ctx = PipelineContext(spec_excerpt="SPEC", decisions_path=rd.path / "decisions.md",
                          conventions="CONV")
    for step in (RED_TEST, GREEN_BUILD, CLEAN_REVIEW):
        _script(fixture_repo, [step])
        run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)
    assert run.tasks[tid].status == "done"
    assert (rd.path / "tasks" / "T1" / "context.md").exists()


def test_direct_brief_creates_one_owner_task_without_plan(regie_home, fixture_repo):
    rd = RunDir.create(regie_home, "r1")
    run = RunState(
        id="r1", target_repo=str(fixture_repo), branch="regie/r1",
        stage="intake", execution_route="direct",
        route_reason="no material risk signal requires an upfront plan",
    )

    apply_direct_brief(rd, run, "# Support multi-row selection\n\nUse shift-click.")

    assert run.stage == "tasks"
    assert list(run.tasks) == ["T1"]
    assert run.tasks["T1"].stage == "build"
    assert run.tasks["T1"].spec.execution == "direct"
    assert "Support multi-row selection" in (rd.path / "spec" / "spec.md").read_text()


def test_direct_owner_may_change_tests_and_skips_separate_test_writer(
        regie_home, fixture_repo, cfg):
    rd = RunDir.create(regie_home, "r1")
    run = RunState(id="r1", target_repo=str(fixture_repo), branch="regie/r1",
                   stage="tasks", execution_route="direct")
    spec = TaskSpec(
        id="T1", title="add divide", profile="implementer",
        criteria=["divide returns the quotient"], execution="direct")
    run.tasks["T1"] = TaskState(spec=spec, stage="build")
    ctx = PipelineContext(spec_excerpt="Direct brief", decisions_path=rd.path / "decisions.md")
    _script(fixture_repo, [{"result": {"outcome": "done"}, "writes": {
        "src/calc.py": "def add(a, b):\n    return a + b\n\ndef divide(a, b):\n    return a // b\n",
        "tests/test_div.py": (
            "from src.calc import divide\n\ndef test_divide():\n    assert divide(6, 3) == 2\n"),
    }}])

    run_task(rd, run, "T1", cfg, fixture_repo, ctx, max_dispatches=1)

    task = run.tasks["T1"]
    assert task.stage == "review"
    assert task.attempts["test"] == []
    assert all(gate.name != "diff-guard" for gate in task.attempts["build"][0].gate_results)


def test_direct_owner_escalates_to_plan_only_with_repository_evidence(
        regie_home, fixture_repo, cfg):
    rd = RunDir.create(regie_home, "r1")
    run = RunState(id="r1", target_repo=str(fixture_repo), branch="regie/r1",
                   stage="tasks", execution_route="direct")
    spec = TaskSpec(id="T1", title="change", profile="implementer",
                    criteria=["change it"], execution="direct")
    run.tasks["T1"] = TaskState(spec=spec, stage="build")
    ctx = PipelineContext(decisions_path=rd.path / "decisions.md")
    _script(fixture_repo, [{"result": {
        "outcome": "blocked",
        "blocked_question": "needs-planning: requires coordinated schema migration",
    }}])

    run_task(rd, run, "T1", cfg, fixture_repo, ctx, max_dispatches=1)

    assert run.stage == "plan"
    assert run.execution_route == "planned"
    assert run.tasks == {}
    assert "schema migration" in run.route_reason


def test_direct_owner_structured_clarification_halts_before_gates(
        regie_home, fixture_repo, cfg):
    rd = RunDir.create(regie_home, "r1")
    run = RunState(id="r1", target_repo=str(fixture_repo), branch="regie/r1",
                   stage="tasks", execution_route="direct")
    spec = TaskSpec(id="T1", title="selection", profile="implementer",
                    criteria=["select rows"], execution="direct")
    run.tasks["T1"] = TaskState(spec=spec, stage="build")
    ctx = PipelineContext(decisions_path=rd.path / "decisions.md")
    _script(fixture_repo, [{"result": {
        "outcome": "done",
        "structured": {
            "status": "clarify",
            "summary": "Two materially different interactions are possible.",
            "question": "Text selection or item selection?",
            "evidence": None,
        },
    }}])

    run_task(rd, run, "T1", cfg, fixture_repo, ctx, max_dispatches=1)

    assert run.stage == "halted"
    assert run.tasks["T1"].status == "blocked"
    assert "clarify: Text selection" in (run.halt_reason or "")
    assert run.tasks["T1"].attempts["build"][0].gate_results == []


def test_setup_failure_halts_before_any_agent_dispatch(
        regie_home, fixture_repo, cfg):
    rd = RunDir.create(regie_home, "r1")
    run, tid = _run_state(fixture_repo)
    cfg.commands["setup"] = "false"
    ctx = PipelineContext(decisions_path=rd.path / "decisions.md")

    run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)

    assert run.stage == "halted"
    assert "before agent dispatch" in (run.halt_reason or "")
    assert run.tasks[tid].attempts["test"] == []
    assert not (rd.path / "tasks" / tid / "attempt-1.out").exists()


def test_infrastructure_gate_preserves_direct_owner_edits_and_halts(
        regie_home, fixture_repo, cfg):
    rd = RunDir.create(regie_home, "r1")
    run = RunState(id="r1", target_repo=str(fixture_repo), branch="regie/r1",
                   stage="tasks", execution_route="direct")
    spec = TaskSpec(id="T1", title="change", profile="implementer",
                    criteria=["change it"], execution="direct")
    run.tasks["T1"] = TaskState(spec=spec, stage="build")
    cfg.commands["test"] = "missing-regie-test-tool"
    cfg.commands["lint"] = "true"
    ctx = PipelineContext(decisions_path=rd.path / "decisions.md")
    _script(fixture_repo, [{"result": {"outcome": "done"}, "writes": {
        "src/kept.py": "value = 1\n",
    }}])

    run_task(rd, run, "T1", cfg, fixture_repo, ctx, max_dispatches=1)

    assert run.stage == "halted"
    assert "implementation preserved" in (run.halt_reason or "")
    assert (fixture_repo / "src" / "kept.py").read_text() == "value = 1\n"
    attempt = run.tasks["T1"].attempts["build"][0]
    assert attempt.failure_kind == "infrastructure"


def test_build_gate_runs_before_test_for_preview_based_suites(
        regie_home, fixture_repo, cfg):
    rd = RunDir.create(regie_home, "r1")
    run = RunState(id="r1", target_repo=str(fixture_repo), branch="regie/r1",
                   stage="tasks", execution_route="direct")
    spec = TaskSpec(id="T1", title="change", profile="implementer",
                    criteria=["change it"], execution="direct")
    run.tasks["T1"] = TaskState(spec=spec, stage="build")
    cfg.commands.update({
        "build": "touch .preview-build",
        "test": "test -f .preview-build",
        "lint": "true",
    })
    ctx = PipelineContext(decisions_path=rd.path / "decisions.md")
    _script(fixture_repo, [{"result": {"outcome": "done"}, "writes": {
        "src/changed.py": "value = 1\n",
    }}])

    run_task(rd, run, "T1", cfg, fixture_repo, ctx, max_dispatches=1)

    assert run.tasks["T1"].stage == "review"
    names = [gate.name for gate in run.tasks["T1"].attempts["build"][0].gate_results]
    assert names[:3] == ["build", "test", "lint"]


def test_reviewer_blocker_routes_back_to_builder(regie_home, fixture_repo, cfg):
    rd = RunDir.create(regie_home, "r1")
    run, tid = _run_state(fixture_repo)
    ctx = PipelineContext(spec_excerpt="S", decisions_path=rd.path / "decisions.md",
                          conventions="C")
    for step in (RED_TEST, GREEN_BUILD):
        _script(fixture_repo, [step])
        run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)
    _script(fixture_repo, [{"result": {"outcome": "done", "structured": {"findings": [
        {"severity": "blocker", "title": "int division truncates"}]}}}])
    run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)
    assert run.tasks[tid].stage == "build"
    findings = json.loads((rd.path / "tasks" / "T1" / "findings.json").read_text())
    assert findings[0]["title"] == "int division truncates"


def test_repair_gets_fresh_review_ladder_without_losing_attempt_history(
        regie_home, fixture_repo, cfg):
    rd = RunDir.create(regie_home, "r1")
    run, tid = _run_state(fixture_repo)
    cfg.profiles["reviewer"] = cfg.profiles["reviewer"].model_copy(update={
        "bindings": [Binding(cli="fake2", model="review")],
        "hard_binding": None,
    })
    ctx = PipelineContext(
        spec_excerpt="S", decisions_path=rd.path / "decisions.md", conventions="C")
    repair = {"result": {"outcome": "done"}, "writes": {
        "src/calc.py": (
            "def add(a, b):\n    return a + b\n\n"
            "def divide(a, b):\n    return a // b  # reviewed repair\n"),
    }}
    blocker = {"result": {"outcome": "done", "structured": {"findings": [{
        "severity": "blocker", "title": "repair required", "detail": "fix it",
    }]}}}

    for step in (RED_TEST, GREEN_BUILD, blocker, repair, CLEAN_REVIEW):
        _script(fixture_repo, [step])
        run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)

    task = run.tasks[tid]
    assert task.status == "done"
    assert len(task.attempts["review"]) == 2
    assert task.review_cycle_start == 1


def test_builder_editing_tests_fails_diff_gate(regie_home, fixture_repo, cfg):
    rd = RunDir.create(regie_home, "r1")
    run, tid = _run_state(fixture_repo)
    ctx = PipelineContext(spec_excerpt="S", decisions_path=rd.path / "decisions.md",
                          conventions="C")
    _script(fixture_repo, [RED_TEST])
    run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)
    cheat = {"result": {"outcome": "done"}, "writes": {
        "tests/test_div.py": "def test_div():\n    assert True\n"}}
    _script(fixture_repo, [cheat])
    run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)
    attempts = run.tasks[tid].attempts["build"]
    assert attempts and attempts[-1].outcome == "failed"
    assert any(not g.passed and g.name == "diff-guard" for g in attempts[-1].gate_results)
    # The failed attempt's uncommitted edit to the test file must not persist
    # into the next attempt (diff-gate poisoning).
    assert (fixture_repo / "tests" / "test_div.py").read_text() != cheat[
        "writes"]["tests/test_div.py"]


def test_gate_failure_writes_note_naming_failing_gate(regie_home, fixture_repo, cfg):
    rd = RunDir.create(regie_home, "r1")
    run, tid = _run_state(fixture_repo)
    ctx = PipelineContext(spec_excerpt="S", decisions_path=rd.path / "decisions.md",
                          conventions="C")
    _script(fixture_repo, [RED_TEST])
    run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)
    cheat = {"result": {"outcome": "done"}, "writes": {
        "tests/test_div.py": "def test_div():\n    assert True\n"}}
    _script(fixture_repo, [cheat])
    run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)
    note_path = rd.task_dir(tid) / "note-build.md"
    assert note_path.exists()
    assert "diff-guard" in note_path.read_text()


def test_halt_after_ladder_exhaustion(regie_home, fixture_repo, cfg):
    rd = RunDir.create(regie_home, "r1")
    run, tid = _run_state(fixture_repo)
    ctx = PipelineContext(spec_excerpt="S", decisions_path=rd.path / "decisions.md",
                          conventions="C")
    bad = {"result": {"outcome": "done"}, "writes": {
        "tests/test_div.py": "def test_div():\n    assert True\n"}}  # never red
    for _ in range(4):
        _script(fixture_repo, [bad])
        run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)
        if run.stage == "halted":
            break
    assert run.stage == "halted" and run.tasks[tid].status == "failed"


def test_escape_hatch_persists_across_resume(regie_home, fixture_repo, cfg):
    rd = RunDir.create(regie_home, "r1")
    run, tid = _run_state(fixture_repo)
    ctx = PipelineContext(spec_excerpt="S", decisions_path=rd.path / "decisions.md",
                          conventions="C")
    _script(fixture_repo, [RED_TEST])
    run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)
    _script(fixture_repo, [{"result": {"outcome": "blocked",
                                       "blocked_question": "bad-test: divide should floor, not truncate"}}])
    run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)
    assert run.tasks[tid].stage == "test"
    assert run.tasks[tid].escaped is True
    persisted = rd.read_state()
    assert persisted.tasks[tid].escaped is True


def test_repeat_bad_test_claim_absorbed_as_failed_attempt(regie_home, fixture_repo, cfg):
    rd = RunDir.create(regie_home, "r1")
    run, tid = _run_state(fixture_repo)
    ctx = PipelineContext(spec_excerpt="S", decisions_path=rd.path / "decisions.md",
                          conventions="C")
    _script(fixture_repo, [RED_TEST])
    run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)
    bad_test_claim = {"result": {"outcome": "blocked",
                                 "blocked_question": "bad-test: divide should floor, not truncate"}}
    _script(fixture_repo, [bad_test_claim])
    run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)
    assert run.tasks[tid].escaped is True
    red_test_again = {"result": {"outcome": "done"}, "writes": {
        "tests/test_div.py": "from src.calc import divide\n\n"
                             "def test_div():\n    assert divide(6, 3) == 2  # re-affirmed\n",
        "src/calc.py": "def add(a, b):\n    return a + b\n\n"
                       "def divide(a, b):\n    raise NotImplementedError\n"}}
    _script(fixture_repo, [red_test_again])
    run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)  # re-pass test stage
    _script(fixture_repo, [bad_test_claim])
    run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)
    assert run.stage != "halted"
    attempts = run.tasks[tid].attempts["build"]
    assert attempts and attempts[-1].outcome == "failed"
    assert run.tasks[tid].stage == "build"


def test_run_tasks_stage_skips_done_tasks(regie_home, fixture_repo, cfg):
    rd = RunDir.create(regie_home, "r1")
    run, tid = _run_state(fixture_repo)
    run.tasks[tid].status = "done"
    rd.write_state(run)
    run_tasks_stage(rd, run, cfg, Path(fixture_repo))  # no .fake_agent.json → would error if dispatched
    assert run.tasks[tid].status == "done" and run.stage != "halted"


def test_build_attempt_with_no_changes_fails_changes_gate(
        regie_home, fixture_repo, cfg):
    rd = RunDir.create(regie_home, "r1")
    run, tid = _run_state(fixture_repo)
    ctx = PipelineContext(spec_excerpt="S", decisions_path=rd.path / "decisions.md",
                          conventions="C")
    _script(fixture_repo, [RED_TEST])
    run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)
    # builder "succeeds" but writes nothing at all
    _script(fixture_repo, [{"result": {"outcome": "done"}}])
    run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)
    attempt = run.tasks[tid].attempts["build"][-1]
    # No changes + red suite = a failed TEST gate (the suite is still red);
    # no empty-commit crash, and no dedicated changes-gate burning ladders
    # on legitimate stage re-entries where work is already committed.
    assert attempt.outcome == "failed"
    assert any(g.name == "test" and not g.passed for g in attempt.gate_results)


def test_dispatch_death_writes_retry_note(regie_home, fixture_repo, cfg):
    rd = RunDir.create(regie_home, "r1")
    run, tid = _run_state(fixture_repo)
    ctx = PipelineContext(spec_excerpt="S", decisions_path=rd.path / "decisions.md",
                          conventions="C")
    _script(fixture_repo, [{"result": {"outcome": "error",
                                       "text": "turn budget exhausted (--max-turns reached)"}}])
    run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)
    note = (rd.path / "tasks" / tid / "note-test.md").read_text()
    assert "died before gates" in note and "turn budget" in note
FAIL_ATTEMPT = {"result": {"outcome": "error"}}
QUOTA_ATTEMPT = {"result": {"outcome": "quota"}}


def test_blocked_attempt_discards_partial_edits(regie_home, fixture_repo, cfg):
    rd = RunDir.create(regie_home, "r1")
    run, tid = _run_state(fixture_repo)
    ctx = PipelineContext(spec_excerpt="S", decisions_path=rd.path / "decisions.md",
                          conventions="C")
    _script(fixture_repo, [{"result": {"outcome": "blocked",
                                       "blocked_question": "which cache?"},
                            "writes": {"src/partial.py": "junk\n"}}])
    run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)
    assert run.stage == "halted"
    assert not (fixture_repo / "src" / "partial.py").exists()


def test_reviewer_leftovers_discarded(regie_home, fixture_repo, cfg):
    rd = RunDir.create(regie_home, "r1")
    run, tid = _run_state(fixture_repo)
    ctx = PipelineContext(spec_excerpt="S", decisions_path=rd.path / "decisions.md",
                          conventions="C")
    for step in (RED_TEST, GREEN_BUILD):
        _script(fixture_repo, [step])
        run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)
    # reviewer has no authority to write, but a real model might anyway —
    # nothing it leaves may survive into later stage commits
    _script(fixture_repo, [{"result": {"outcome": "done",
                                       "structured": {"findings": []}},
                            "writes": {"src/review_scratch.py": "oops\n"}}])
    run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)
    assert run.tasks[tid].status == "done"
    assert not (fixture_repo / "src" / "review_scratch.py").exists()

def test_ac11_third_build_attempt_escalates_to_profiles_second_binding(
        regie_home, fixture_repo, cfg):
    """Builder profile is bindings: [fake:m1, fake:m2] (fake_profiles fixture).
    Two failed build attempts must retry on fake:m1; the third must escalate
    to fake:m2, per AC11."""
    rd = RunDir.create(regie_home, "r1")
    run, tid = _run_state(fixture_repo)
    ctx = PipelineContext(spec_excerpt="S", decisions_path=rd.path / "decisions.md",
                          conventions="C")
    _script(fixture_repo, [RED_TEST])
    run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)  # test -> build

    for _ in range(2):
        _script(fixture_repo, [FAIL_ATTEMPT])
        run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)
    assert run.stage != "halted"
    build_attempts = run.tasks[tid].attempts["build"]
    assert [a.binding for a in build_attempts] == [Binding(cli="fake", model="m1")] * 2

    _script(fixture_repo, [FAIL_ATTEMPT])
    run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)
    assert run.tasks[tid].attempts["build"][-1].binding == Binding(cli="fake", model="m2")

def _profile(name: str, tmp_path: Path, bindings: list[Binding]) -> Profile:
    prompt = tmp_path / f"{name}.md"
    prompt.write_text(f"You are {name}.")
    return Profile(name=name, bindings=bindings, prompt_path=prompt, budgets=Budgets())


class _Cfg:
    def __init__(self, builder: Profile, reviewer: Profile):
        self.profiles = {"builder": builder, "reviewer": reviewer}
        self.workflow = WorkflowConfig(knowledge=False)

def test_ac15_flip_walks_builder_bindings_list(regie_home, fixture_repo, tmp_path):
    """The build stage's last attempt ran on fake2:strong; the reviewer's
    primary family is also fake2, so the review flips to the builder's
    profile — including its two-rung ladder, not the reviewer's one-rung
    list."""
    builder = _profile("builder", tmp_path,
                       [Binding(cli="fake", model="m1"), Binding(cli="fake2", model="strong")])
    reviewer = _profile("reviewer", tmp_path, [Binding(cli="fake2", model="rev1")])
    cfg = _Cfg(builder, reviewer)

    rd = RunDir.create(regie_home, "r1")
    run, tid = _run_state(fixture_repo)
    task = run.tasks[tid]
    task.stage = "review"
    task.attempts["build"].append(
        Attempt(binding=Binding(cli="fake2", model="strong"), outcome="done"))
    ctx = PipelineContext(spec_excerpt="S", decisions_path=rd.path / "decisions.md",
                          conventions="C")

    for _ in range(3):
        _script(fixture_repo, [FAIL_ATTEMPT])
        run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)

    assert run.stage != "halted"
    review_bindings = [a.binding for a in task.attempts["review"]]
    assert review_bindings == [builder.primary, builder.primary,
                              Binding(cli="fake2", model="strong")]

def test_ac15_no_flip_walks_reviewers_own_bindings_list(regie_home, fixture_repo, tmp_path):
    """The build stage's last attempt ran on fake:m1, a different family from
    the reviewer's fake2 primary, so no flip occurs: the review stage walks
    the reviewer's own two-rung list."""
    builder = _profile("builder", tmp_path, [Binding(cli="fake", model="m1")])
    reviewer = _profile("reviewer", tmp_path,
                        [Binding(cli="fake2", model="rev1"), Binding(cli="fake2", model="rev2")])
    cfg = _Cfg(builder, reviewer)

    rd = RunDir.create(regie_home, "r1")
    run, tid = _run_state(fixture_repo)
    task = run.tasks[tid]
    task.stage = "review"
    task.attempts["build"].append(
        Attempt(binding=Binding(cli="fake", model="m1"), outcome="done"))
    ctx = PipelineContext(spec_excerpt="S", decisions_path=rd.path / "decisions.md",
                          conventions="C")

    for _ in range(3):
        _script(fixture_repo, [FAIL_ATTEMPT])
        run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)

    assert run.stage != "halted"
    review_bindings = [a.binding for a in task.attempts["review"]]
    assert review_bindings == [reviewer.primary, reviewer.primary,
                              Binding(cli="fake2", model="rev2")]


def test_ac12_quota_advances_to_next_binding_without_burning_retry(
        regie_home, fixture_repo, cfg):
    """Builder profile is bindings: [fake:m1, fake:m2] (fake_profiles fixture).
    A quota outcome on the first build attempt (fake:m1) must dispatch attempt
    2 directly on fake:m2 -- not retry fake:m1 -- while the quota attempt
    itself stays recorded with outcome "quota". fake:m2 must then still get
    its own two attempts (retry, then escalate-halt) before the run halts."""
    rd = RunDir.create(regie_home, "r1")
    run, tid = _run_state(fixture_repo)
    ctx = PipelineContext(spec_excerpt="S", decisions_path=rd.path / "decisions.md",
                          conventions="C")
    _script(fixture_repo, [RED_TEST])
    run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)  # test -> build

    _script(fixture_repo, [QUOTA_ATTEMPT])
    run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)
    assert run.stage != "halted"

    _script(fixture_repo, [FAIL_ATTEMPT])
    run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)
    assert run.stage != "halted"

    _script(fixture_repo, [FAIL_ATTEMPT])
    run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)
    assert run.stage != "halted"

    build_attempts = run.tasks[tid].attempts["build"]
    assert [(a.binding, a.outcome) for a in build_attempts] == [
        (Binding(cli="fake", model="m1"), "quota"),
        (Binding(cli="fake", model="m2"), "failed"),
        (Binding(cli="fake", model="m2"), "failed"),
    ]

    run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)
    assert run.stage == "halted"
    assert len(run.tasks[tid].attempts["build"]) == 3  # no dispatch beyond exhaustion
    assert run.halt_reason == f"build ladder exhausted on {tid}"


def test_ac13_quota_with_no_next_binding_halts_naming_provider(
        regie_home, fixture_repo, tmp_path):
    """A one-element builder bindings list means a quota outcome has nowhere
    to advance to: the run must halt immediately, naming "quota", the
    exhausted "cli:model", and the stage -- and never dispatch again on that
    same binding."""
    builder = _profile("builder", tmp_path, [Binding(cli="fake", model="m1")])

    class _OneBindingCfg:
        def __init__(self):
            self.profiles = {"builder": builder}
            self.workflow = WorkflowConfig(knowledge=False)

    cfg = _OneBindingCfg()
    rd = RunDir.create(regie_home, "r1")
    run, tid = _run_state(fixture_repo)
    run.tasks[tid].stage = "build"
    ctx = PipelineContext(spec_excerpt="S", decisions_path=rd.path / "decisions.md",
                          conventions="C")

    _script(fixture_repo, [QUOTA_ATTEMPT])
    run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)

    assert run.stage == "halted"
    assert "quota" in run.halt_reason
    assert "fake:m1" in run.halt_reason
    assert "build" in run.halt_reason
    assert len(run.tasks[tid].attempts["build"]) == 1



def test_hard_task_starts_on_strongest_rung(regie_home, fixture_repo, cfg):
    rd = RunDir.create(regie_home, "rh")
    spec = TaskSpec(id="T1", title="hard one", profile="builder",
                    criteria=["c"], complexity="hard")
    run = RunState(id="rh", target_repo=str(fixture_repo), branch="regie/rh",
                   stage="tasks", tasks={"T1": TaskState(spec=spec)})
    ctx = PipelineContext(spec_excerpt="S", decisions_path=rd.path / "decisions.md",
                          conventions="C")
    _script(fixture_repo, [{"result": {"outcome": "blocked", "blocked_question": "?"}}])
    run_task(rd, run, "T1", cfg, fixture_repo, ctx, max_dispatches=1)
    # fake_profiles declare hard: fake:m2 — hard tasks start there
    assert run.tasks["T1"].attempts["test"][0].binding.model == "m2"


def test_effective_bindings_shapes():
    import pathlib as _pl

    from regie.config import Profile
    from regie.pipeline import _effective_bindings
    prompt = _pl.Path(__file__)
    mk = lambda cli, model: Binding(cli=cli, model=model)
    base = {"name": "p", "prompt_path": prompt, "budgets": Budgets()}
    prof = Profile(bindings=[mk("claude", "opus"), mk("claude", "fable"),
                             mk("codex", "gpt-5.6-sol")],
                   hard_binding=mk("claude", "fable"), **base)
    # standard: untouched
    assert _effective_bindings(prof, "standard") == prof.bindings
    # hard: explicit big gun first, then only OTHER-vendor rungs
    hard = _effective_bindings(prof, "hard")
    assert hard[0].model == "fable"
    assert [b.cli for b in hard[1:]] == ["codex"]
    # no hard binding configured -> hard tasks use the normal ladder
    plain = Profile(bindings=[mk("codex", "gpt-5.6-sol"), mk("claude", "sonnet")], **base)
    assert _effective_bindings(plain, "hard") == plain.bindings


def test_all_output_schemas_are_strict_for_codex():
    """codex structured output rejects any object missing
    additionalProperties:false — every schema object level must be strict."""
    from regie.pipeline import FINDINGS_SCHEMA, PLAN_SCHEMA, SCRIBE_SCHEMA

    def assert_strict(node, path="$"):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False, path
            for k, v in node.items():
                assert_strict(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                assert_strict(v, f"{path}[{i}]")

    for name, schema in (("plan", PLAN_SCHEMA), ("findings", FINDINGS_SCHEMA),
                         ("scribe", SCRIBE_SCHEMA)):
        assert_strict(schema, name)
