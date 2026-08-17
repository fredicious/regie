from types import SimpleNamespace

from regie.config import GatePlugin, WorkflowConfig
from regie.models import RunState, TaskSpec, TaskState
from regie.workflow import (
    active_gate_plugins,
    infer_risks,
    plan_preflight,
    resolve_tier,
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


def test_plan_preflight_requires_checkpoint_for_external_dependency():
    task = _task(external_dependencies=["STRIPE_API_KEY"])
    assert "require a checkpoint" in " ".join(plan_preflight([task]))
    task.checkpoint = "Confirm sandbox Stripe credentials"
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
