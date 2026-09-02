import json

import pytest

from regie.config import Profile, WorkflowConfig, load_config
from regie.models import Binding, Budgets, RunState
from regie.pipeline import _plan_schema, plan_stage, reconcile
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
    assert "did not converge" in run.halt_reason
    assert "note-plan.md" in [p.name for p in (rd.path / "tasks" / "PLAN").iterdir()]


def test_plan_stage_rejects_unknown_profile(regie_home, fixture_repo, cfg):
    bad = {"spec_markdown": "s", "tasks": [{"id": "T1", "title": "t",
           "profile": "wizard", "criteria": ["Given a When b Then c"],
           "planned_tests": ["test_x"]}]}
    rd, run = _seed(regie_home, fixture_repo, bad)
    plan_stage(rd, run, cfg, fixture_repo)
    assert run.stage == "halted"


def test_plan_schema_constrains_profile_to_loaded_configuration(cfg):
    profile_schema = _plan_schema(cfg)["properties"]["tasks"]["items"][
        "properties"
    ]["profile"]

    assert profile_schema["enum"] == sorted(cfg.profiles)


def test_plan_schema_constrains_risks_and_task_reviewers(cfg):
    properties = _plan_schema(cfg)["properties"]["tasks"]["items"]["properties"]

    assert set(properties["risk_tags"]["items"]["enum"]) == {
        "security", "migration", "api", "ui", "architecture", "external",
    }
    assert "integration-reviewer" not in properties["review_lenses"]["items"]["enum"]
    assert set(properties["review_lenses"]["items"]["enum"]) <= set(cfg.profiles)


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


def _add_product_owner(cfg, tmp_path):
    prompt = tmp_path / "product-owner.md"
    prompt.write_text("You are the bounded Product Owner recovery advisor.")
    cfg.profiles["product-owner"] = Profile(
        name="product-owner",
        bindings=[Binding(cli="fake", model="po")],
        prompt_path=prompt,
        budgets=Budgets(turns=5, wall_minutes=1, stall_minutes=1),
    )


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


def test_plan_contract_repairs_have_budget_separate_from_provider_failover(
        regie_home, fixture_repo, cfg):
    invalid_profile = {
        "spec_markdown": "# Spec",
        "tasks": [{
            "id": "T1",
            "title": "divide",
            "profile": "frontend-svelte",
            "criteria": ["Given 6 and 3, When divided, Then return 2"],
            "planned_tests": ["test_divide_exact"],
        }],
    }
    incomplete = {
        "spec_markdown": "# Spec",
        "tasks": [{
            "id": "T1",
            "title": "divide",
            "profile": "builder",
            "criteria": ["Given 6 and 3, When divided, Then return 2"],
            "planned_tests": [],
        }],
    }
    rd, run = _seed(regie_home, fixture_repo, PLAN)
    (fixture_repo / ".fake_agent.json").unlink()
    _queue(fixture_repo, [
        {"result": {"outcome": "quota"}},
        {"result": {"outcome": "done", "structured": invalid_profile}},
        {"result": {"outcome": "done", "structured": incomplete}},
        {"result": {"outcome": "done", "structured": PLAN}},
    ])

    plan_stage(rd, run, cfg, fixture_repo)

    assert [attempt.outcome for attempt in run.planner_attempts] == [
        "quota", "failed", "failed", "done",
    ]
    assert [attempt.binding.model for attempt in run.planner_attempts] == [
        "m1", "m2", "m2", "m2",
    ]
    assert run.stage == "approve"


def test_product_owner_directs_one_final_plan_recovery(
        regie_home, fixture_repo, cfg, tmp_path):
    bad = {
        "spec_markdown": "# Spec",
        "tasks": [{
            "id": "T1", "title": "divide", "profile": "builder",
            "criteria": ["Given 6 and 3, When divided, Then return 2"],
            "planned_tests": [],
        }],
    }
    decision = {
        "action": "revise",
        "summary": "Keep the scope and restore the missing test contract.",
        "directives": ["Add a named planned test for the divide criterion."],
        "accepted_findings": ["The behavior remains in scope."],
        "rejected_findings": [],
        "human_question": None,
    }
    rd, run = _seed(regie_home, fixture_repo, PLAN)
    (fixture_repo / ".fake_agent.json").unlink()
    _add_product_owner(cfg, tmp_path)
    _queue(fixture_repo, [
        {"result": {"outcome": "done", "structured": bad}},
        {"result": {"outcome": "done", "structured": bad}},
        {"result": {"outcome": "done", "structured": bad}},
        {"result": {"outcome": "done", "structured": decision}},
        {"result": {"outcome": "done", "structured": PLAN}},
    ])

    plan_stage(rd, run, cfg, fixture_repo)

    assert run.stage == "approve"
    assert len(run.planner_attempts) == 4
    assert len(run.product_owner_attempts) == 1
    assert run.product_owner_decision is not None
    assert run.product_owner_decision.action == "revise"
    assert "Product Owner recovery directives" in (
        rd.task_dir("PLAN") / "note-plan.md"
    ).read_text()
    assert (rd.path / "product-owner-decision.json").is_file()
    assert (rd.path / "product-owner-decision.md").is_file()


def test_product_owner_cannot_accept_deterministically_invalid_plan(
        regie_home, fixture_repo, cfg, tmp_path):
    bad = {
        "spec_markdown": "# Spec",
        "tasks": [{
            "id": "T1", "title": "divide", "profile": "builder",
            "criteria": ["Given 6 and 3, When divided, Then return 2"],
            "planned_tests": [],
        }],
    }
    forbidden_accept = {
        "action": "accept",
        "summary": "Proceed despite the missing mechanical contract.",
        "directives": [],
        "accepted_findings": [],
        "rejected_findings": ["planned_tests must be non-empty"],
        "human_question": None,
    }
    rd, run = _seed(regie_home, fixture_repo, PLAN)
    (fixture_repo / ".fake_agent.json").unlink()
    _add_product_owner(cfg, tmp_path)
    _queue(fixture_repo, [
        {"result": {"outcome": "done", "structured": bad}},
        {"result": {"outcome": "done", "structured": bad}},
        {"result": {"outcome": "done", "structured": bad}},
        {"result": {"outcome": "done", "structured": forbidden_accept}},
    ])

    plan_stage(rd, run, cfg, fixture_repo)

    assert run.stage == "halted"
    assert "cannot accept" in (run.halt_reason or "")
    assert not (rd.path / "spec" / "spec.md").exists()


def test_product_owner_may_accept_scope_review_findings(
        regie_home, fixture_repo, cfg, tmp_path, monkeypatch):
    accept = {
        "action": "accept",
        "summary": "The remaining suggestion is outside the requested scope.",
        "directives": [],
        "accepted_findings": [],
        "rejected_findings": ["Add an unrelated reporting endpoint."],
        "human_question": None,
    }
    rd, run = _seed(regie_home, fixture_repo, PLAN)
    (fixture_repo / ".fake_agent.json").unlink()
    _add_product_owner(cfg, tmp_path)
    monkeypatch.setattr(
        "regie.pipeline._run_plan_reviews",
        lambda *_args, **_kwargs: ["plan-scope: unrelated optional expansion"],
    )
    _queue(fixture_repo, [
        {"result": {"outcome": "done", "structured": PLAN}},
        {"result": {"outcome": "done", "structured": accept}},
    ])

    plan_stage(rd, run, cfg, fixture_repo)

    assert run.stage == "approve"
    assert run.product_owner_decision is not None
    assert run.product_owner_decision.action == "accept"
    assert len(run.planner_attempts) == 1
    assert len(run.product_owner_attempts) == 1
    assert (rd.path / "spec" / "spec.md").is_file()


def test_product_owner_may_reject_advisory_completeness_finding(
        regie_home, fixture_repo, cfg, tmp_path, monkeypatch):
    accept = {
        "action": "accept",
        "summary": "Ignore a missing required behavior.",
        "directives": [],
        "accepted_findings": [],
        "rejected_findings": ["A requested error path is missing."],
        "human_question": None,
    }
    rd, run = _seed(regie_home, fixture_repo, PLAN)
    (fixture_repo / ".fake_agent.json").unlink()
    _add_product_owner(cfg, tmp_path)
    monkeypatch.setattr(
        "regie.pipeline._run_plan_reviews",
        lambda *_args, **_kwargs: ["plan-completeness: required error path missing"],
    )
    _queue(fixture_repo, [
        {"result": {"outcome": "done", "structured": PLAN}},
        {"result": {"outcome": "done", "structured": accept}},
    ])

    plan_stage(rd, run, cfg, fixture_repo)

    assert run.stage == "approve"
    assert run.product_owner_decision is not None
    assert run.product_owner_decision.rejected_findings


def test_product_owner_accept_requires_explicit_advisory_rejections(
        regie_home, fixture_repo, cfg, tmp_path, monkeypatch):
    incomplete_accept = {
        "action": "accept",
        "summary": "Proceed without recording why the finding is optional.",
        "directives": [],
        "accepted_findings": [],
        "rejected_findings": [],
        "human_question": None,
    }
    rd, run = _seed(regie_home, fixture_repo, PLAN)
    (fixture_repo / ".fake_agent.json").unlink()
    _add_product_owner(cfg, tmp_path)
    monkeypatch.setattr(
        "regie.pipeline._run_plan_reviews",
        lambda *_args, **_kwargs: ["plan-completeness: required error path missing"],
    )
    _queue(fixture_repo, [
        {"result": {"outcome": "done", "structured": PLAN}},
        {"result": {"outcome": "done", "structured": incomplete_accept}},
    ])

    plan_stage(rd, run, cfg, fixture_repo)

    assert run.stage == "halted"
    assert "without explicitly rejecting" in (run.halt_reason or "")


def test_product_owner_cannot_waive_reviewer_unavailability(
        regie_home, fixture_repo, cfg, tmp_path, monkeypatch):
    accept = {
        "action": "accept",
        "summary": "Proceed without the configured review.",
        "directives": [],
        "accepted_findings": [],
        "rejected_findings": ["The reviewer was unavailable."],
        "human_question": None,
    }
    rd, run = _seed(regie_home, fixture_repo, PLAN)
    (fixture_repo / ".fake_agent.json").unlink()
    _add_product_owner(cfg, tmp_path)
    monkeypatch.setattr(
        "regie.pipeline._run_plan_reviews",
        lambda *_args, **_kwargs: [
            "architecture-design-reviewer: reviewer unavailable (quota)"
        ],
    )
    _queue(fixture_repo, [
        {"result": {"outcome": "done", "structured": PLAN}},
        {"result": {"outcome": "done", "structured": accept}},
    ])

    plan_stage(rd, run, cfg, fixture_repo)

    assert run.stage == "halted"
    assert "cannot waive configured reviewer" in (run.halt_reason or "")


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
            self.workflow = WorkflowConfig(
                knowledge=False, plan_reviews=False, design_reviews=False)

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


def test_reconcile_marks_orphaned_product_owner_intent_failed(
        regie_home, fixture_repo):
    rd = RunDir.create(regie_home, "r1")
    run = RunState(id="r1", target_repo=str(fixture_repo), branch="regie/r1",
                   stage="plan", worktree_path=str(fixture_repo))
    rd.write_state(run)
    rd.append_intent({
        "task": "PRODUCT-OWNER",
        "stage": "product-owner",
        "attempt": 1,
        "binding": {"cli": "fake", "model": "po"},
    })

    count = reconcile(rd, run, fixture_repo)

    assert count == 1
    assert len(run.product_owner_attempts) == 1
    assert run.product_owner_attempts[0].outcome == "failed"
