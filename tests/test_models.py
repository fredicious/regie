import pydantic
import pytest
from regie.models import Attempt, Binding, CycleError, RunState, TaskSpec, TaskState


def _task(tid: str, deps: list[str]) -> TaskState:
    return TaskState(spec=TaskSpec(id=tid, title=tid, profile="builder", criteria=["c"], depends_on=deps))


def test_run_state_round_trips_through_json():
    run = RunState(id="r1", target_repo="/tmp/x", branch="regie/r1")
    run.tasks["T1"] = _task("T1", [])
    run.tasks["T1"].attempts["build"].append(Attempt(binding=Binding(cli="fake", model="m1")))
    restored = RunState.model_validate_json(run.model_dump_json())
    assert restored.tasks["T1"].attempts["build"][0].binding.model == "m1"


def test_ordered_task_ids_is_topological_and_stable():
    run = RunState(id="r1", target_repo="/tmp/x", branch="regie/r1")
    run.tasks = {"T2": _task("T2", ["T1"]), "T1": _task("T1", []), "T3": _task("T3", ["T1"])}
    assert run.ordered_task_ids() == ["T1", "T2", "T3"]


def test_ordered_task_ids_raises_on_cycle():
    run = RunState(id="r1", target_repo="/tmp/x", branch="regie/r1")
    run.tasks = {"T1": _task("T1", ["T2"]), "T2": _task("T2", ["T1"])}
    with pytest.raises(CycleError):
        run.ordered_task_ids()


def test_finding_severity_is_constrained():
    from regie.models import Finding
    with pytest.raises(pydantic.ValidationError):
        Finding(severity="nitpick", title="x")
