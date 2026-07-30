import json
from pathlib import Path

import pytest

from regie.config import load_config
from regie.models import RunState, TaskSpec, TaskState
from regie.pipeline import PipelineContext, run_task, run_tasks_stage
from regie.rundir import RunDir

PROFILES_FAKE = None  # built by fixture below


@pytest.fixture
def cfg(fixture_repo, fake_profiles):
    (fixture_repo / "regie.toml").write_text("""
test_globs = ["tests/**"]
binding_strength = ["fake:m1", "fake:m2"]
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
