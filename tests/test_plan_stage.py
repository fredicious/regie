import json

import pytest

from regie.config import Profile, load_config
from regie.models import Binding, Budgets, RunState
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


def _queue(fixture_repo, entries):
    qdir = fixture_repo / ".fake_agent_queue"
    qdir.mkdir(exist_ok=True)
    for i, entry in enumerate(entries):
        (qdir / f"{i}.json").write_text(json.dumps(entry))


def test_ac14_planner_quota_advances_to_second_binding(regie_home, fixture_repo, cfg):
    """Planner profile is bindings: [fake:m1, fake:m2] (fake_profiles fixture).
    A quota outcome on the first plan attempt (fake:m1) must dispatch the next
    plan attempt directly on fake:m2, and that attempt still succeeds."""
    rd, run = _seed(regie_home, fixture_repo, PLAN)
    (fixture_repo / ".fake_agent.json").unlink()
    _queue(fixture_repo, [
        {"result": {"outcome": "quota"}},
        {"result": {"outcome": "done", "structured": PLAN}},
    ])
    plan_stage(rd, run, cfg, fixture_repo)

    assert [(a.binding, a.outcome) for a in run.planner_attempts] == [
        (Binding(cli="fake", model="m1"), "quota"),
        (Binding(cli="fake", model="m2"), "done"),
    ]
    assert run.stage == "approve"


def test_ac14_planner_quota_halts_naming_exhausted_binding(regie_home, fixture_repo, tmp_path):
    """A one-element planner bindings list means a quota outcome has nowhere
    to advance to: the run must halt naming the exhausted binding, and never
    dispatch again."""
    prompt = tmp_path / "planner.md"
    prompt.write_text("You are planner.")
    planner = Profile(name="planner", bindings=[Binding(cli="fake", model="m1")],
                      prompt_path=prompt, budgets=Budgets())

    class _OneBindingCfg:
        def __init__(self):
            self.profiles = {"planner": planner}

    rd, run = _seed(regie_home, fixture_repo, PLAN)
    (fixture_repo / ".fake_agent.json").write_text(json.dumps(
        {"result": {"outcome": "quota"}}))
    plan_stage(rd, run, _OneBindingCfg(), fixture_repo)

    assert run.stage == "halted"
    assert "quota" in run.halt_reason
    assert "fake:m1" in run.halt_reason
    assert len(run.planner_attempts) == 1


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
