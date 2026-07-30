import json

import pytest

from regie.config import load_config
from regie.models import RunState
from regie.pipeline import plan_stage, reconcile
from regie.rundir import RunDir

PLAN = {"spec_markdown": "# Spec\n...", "tasks": [
    {"id": "T1", "title": "divide", "profile": "builder",
     "criteria": ["Given 6 and 3, When divide, Then 2"],
     "planned_tests": ["test_divide_exact"], "depends_on": []}]}


@pytest.fixture
def cfg(fixture_repo, fake_profiles):
    (fixture_repo / "regie.toml").write_text("""
test_globs = ["tests/**"]
[commands]
test = "python -m pytest tests -q"
lint = "true"
""")
    return load_config(fixture_repo, fake_profiles)


def _seed(regie_home, fixture_repo, plan_result):
    rd = RunDir.create(regie_home, "r1")
    (rd.path / "brief.md").write_text("# brief")
    run = RunState(id="r1", target_repo=str(fixture_repo), branch="regie/r1",
                   stage="plan", worktree_path=str(fixture_repo))
    (fixture_repo / ".fake_agent.json").write_text(json.dumps(
        {"result": {"outcome": "done", "structured": plan_result}}))
    return rd, run


def test_plan_stage_success_populates_tasks_and_stops_at_approve(
        regie_home, fixture_repo, cfg):
    rd, run = _seed(regie_home, fixture_repo, PLAN)
    plan_stage(rd, run, cfg, fixture_repo)
    assert run.stage == "approve"
    assert (rd.path / "spec" / "spec.md").read_text().startswith("# Spec")
    assert run.tasks["T1"].spec.planned_tests == ["test_divide_exact"]


def test_plan_stage_autonomous_skips_approve(regie_home, fixture_repo, cfg):
    rd, run = _seed(regie_home, fixture_repo, PLAN)
    run.autonomous = True
    plan_stage(rd, run, cfg, fixture_repo)
    assert run.stage == "tasks"


def test_plan_stage_rejects_non_gwt_criteria_and_retries(regie_home, fixture_repo, cfg):
    bad = {"spec_markdown": "s", "tasks": [{"id": "T1", "title": "t",
           "profile": "builder", "criteria": ["it should work"],
           "planned_tests": ["test_x"]}]}
    rd, run = _seed(regie_home, fixture_repo, bad)
    plan_stage(rd, run, cfg, fixture_repo)   # fake returns same bad plan every attempt
    assert run.stage == "halted" and len(run.planner_attempts) >= 3
    assert "note-plan.md" in [p.name for p in (rd.path / "tasks" / "PLAN").iterdir()]


def test_plan_stage_rejects_unknown_profile(regie_home, fixture_repo, cfg):
    bad = {"spec_markdown": "s", "tasks": [{"id": "T1", "title": "t",
           "profile": "wizard", "criteria": ["Given a When b Then c"],
           "planned_tests": ["test_x"]}]}
    rd, run = _seed(regie_home, fixture_repo, bad)
    plan_stage(rd, run, cfg, fixture_repo)
    assert run.stage == "halted"


def test_plan_stage_rejects_dangling_depends_on(regie_home, fixture_repo, cfg):
    bad = {"spec_markdown": "s", "tasks": [{"id": "T1", "title": "t",
           "profile": "builder", "criteria": ["Given a When b Then c"],
           "planned_tests": ["test_x"], "depends_on": ["T-does-not-exist"]}]}
    rd, run = _seed(regie_home, fixture_repo, bad)
    plan_stage(rd, run, cfg, fixture_repo)
    assert run.stage == "halted"
    note = (rd.path / "tasks" / "PLAN" / "note-plan.md").read_text()
    assert "T-does-not-exist" in note


def test_plan_stage_rejects_duplicate_task_ids(regie_home, fixture_repo, cfg):
    bad = {"spec_markdown": "s", "tasks": [
        {"id": "T1", "title": "t", "profile": "builder",
         "criteria": ["Given a When b Then c"], "planned_tests": ["test_x"]},
        {"id": "T1", "title": "t2", "profile": "builder",
         "criteria": ["Given a When b Then c"], "planned_tests": ["test_y"]}]}
    rd, run = _seed(regie_home, fixture_repo, bad)
    plan_stage(rd, run, cfg, fixture_repo)
    assert run.stage == "halted"
    note = (rd.path / "tasks" / "PLAN" / "note-plan.md").read_text()
    assert "duplicate" in note.lower()


def test_reconcile_marks_orphaned_plan_intent_failed(regie_home, fixture_repo):
    rd = RunDir.create(regie_home, "r1")
    (rd.path / "brief.md").write_text("# brief")
    run = RunState(id="r1", target_repo=str(fixture_repo), branch="regie/r1",
                   stage="plan", worktree_path=str(fixture_repo))
    rd.write_state(run)
    rd.append_intent({"task": "PLAN", "stage": "plan", "attempt": 1,
                      "binding": {"cli": "fake", "model": "m1"}})
    count = reconcile(rd, run, fixture_repo)
    assert count == 1
    assert len(run.planner_attempts) == 1
    assert run.planner_attempts[0].outcome == "failed"
