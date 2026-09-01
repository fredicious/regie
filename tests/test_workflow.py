from types import SimpleNamespace

from regie.config import GatePlugin, WorkflowConfig
from regie.models import RunState, TaskSpec, TaskState
from regie.workflow import (
    active_gate_plugins,
    infer_risks,
    plan_preflight,
    resolve_tier,
    route_brief,
    scopes_overlap,
)


def _task(task_id="T1", **kwargs):
    return TaskSpec(
        id=task_id, title=kwargs.pop("title", "Add endpoint"), profile="builder",
        criteria=kwargs.pop("criteria", ["Given input When called Then output"]),
        planned_tests=kwargs.pop("planned_tests", ["test_output"]),
        file_scope=kwargs.pop("file_scope", [f"src/{task_id}.py"]),
        checklist=kwargs.pop("checklist", ["error path"]), **kwargs)


def test_risk_inference_and_auto_tier_are_selective():
    task = _task(title="Add authenticated API endpoint",
                 file_scope=["src/api/login.py"])
    run = RunState(id="r", target_repo="x", branch="regie/r")
    run.tasks = {task.id: TaskState(spec=task)}
    cfg = SimpleNamespace(workflow=WorkflowConfig())

    assert {"security", "api"} <= set(infer_risks(task))
    assert resolve_tier(run, cfg) == "critical"


def test_auto_tier_keeps_one_low_risk_task_fast():
    task = _task(title="Rename calculator variable")
    run = RunState(id="r", target_repo="x", branch="regie/r",
                   tasks={task.id: TaskState(spec=task)})
    assert resolve_tier(run, SimpleNamespace(workflow=WorkflowConfig())) == "fast"


def test_brief_router_defaults_to_direct_and_respects_risk_and_policy():
    cfg = SimpleNamespace(workflow=WorkflowConfig())
    assert route_brief("Fix multi-row selection in the checklist", "auto", cfg) == (
        "direct", "no material risk signal requires an upfront plan")

    route, reason = route_brief(
        "Migrate the database schema and backfill existing rows", "auto", cfg)
    assert route == "planned"
    assert "migration" in reason

    assert route_brief("Tiny change", "critical", cfg)[0] == "planned"
    assert route_brief("Database migration", "fast", cfg)[0] == "direct"


def test_brief_router_can_disable_direct_execution():
    cfg = SimpleNamespace(workflow=WorkflowConfig(direct_execution=False))
    route, reason = route_brief("Rename one local variable", "auto", cfg)
    assert route == "planned"
    assert "disabled" in reason


def test_plan_preflight_requires_checkpoint_for_external_dependency():
    task = _task(external_dependencies=["STRIPE_API_KEY"])
    assert "require a checkpoint" in " ".join(plan_preflight([task]))
    task.checkpoint = "Confirm sandbox Stripe credentials"
    assert plan_preflight([task]) == []


def test_plan_preflight_rejects_unnecessary_checkpoint():
    task = _task(
        title="Migrate local persistence to a versioned envelope",
        risk_tags=["migration"],
        checkpoint="Acknowledge the local schema change",
    )

    assert "no external, destructive, or irreversible authority" in " ".join(
        plan_preflight([task])
    )

    task.criteria.append(
        "Given production data When the irreversible migration runs Then it is updated"
    )
    assert plan_preflight([task]) == []


def test_scope_overlap_blocks_parallel_globs_and_parent_paths():
    one = _task("T1", file_scope=["src/api/**"])
    two = _task("T2", file_scope=["src/api/routes.py"])
    assert scopes_overlap([one, two])
    two.file_scope = ["web/app.tsx"]
    assert not scopes_overlap([one, two])


def test_gate_plugins_filter_by_stage_tier_and_changed_path():
    plugin = GatePlugin(name="visual", command="playwright test",
                        trigger_globs=["**/*.tsx"], stages=["finalize"])
    cfg = SimpleNamespace(gate_plugins=[plugin])
    assert active_gate_plugins(cfg, "finalize", "standard", ["web/app.tsx"]) == [plugin]
    assert active_gate_plugins(cfg, "build", "standard", ["web/app.tsx"]) == []
    assert active_gate_plugins(cfg, "finalize", "fast", ["web/app.tsx"]) == []


def test_task_layers_are_stable():
    run = RunState(id="r", target_repo="x", branch="regie/r")
    run.tasks = {
        "T3": TaskState(spec=_task("T3", depends_on=["T1", "T2"])),
        "T2": TaskState(spec=_task("T2")),
        "T1": TaskState(spec=_task("T1")),
    }
    assert run.task_layers() == [["T1", "T2"], ["T3"]]
