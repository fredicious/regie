import json

import pytest

from regie.config import load_config
from regie.models import RunState
from regie.pipeline import plan_stage
from regie.rundir import RunDir

PLAN = {"spec_markdown": "# Spec\n...", "tasks": [
    {"id": "T1", "title": "divide", "profile": "builder",
     "criteria": ["Given 6 and 3, When divide, Then 2"],
     "planned_tests": ["test_divide_exact"], "depends_on": []}]}


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
