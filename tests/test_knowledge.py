import json

from regie.knowledge import approve_candidates, prime, propose_learnings
from regie.models import Attempt, Binding, RunState, TaskSpec, TaskState
from regie.rundir import RunDir


def test_reflection_candidates_require_explicit_promotion(regie_home, fixture_repo):
    rundir = RunDir.create(regie_home, "r1")
    (rundir.path / "decisions.md").write_text(
        "- API handlers must validate payloads before calling services\n")
    task = TaskSpec(id="T1", title="API validation", profile="builder",
                    criteria=["Given bad input When called Then reject"],
                    file_scope=["src/api.py"])
    state = TaskState(spec=task)
    state.attempts["build"].append(Attempt(
        binding=Binding(cli="fake", model="m"), outcome="failed",
        failure_kind="repeated-gate", failure_signature="gate:123"))
    run = RunState(id="r1", target_repo=str(fixture_repo), branch="regie/r1",
                   tasks={"T1": state})

    candidates = propose_learnings(rundir, run)
    assert len(candidates) == 2
    store_root = regie_home / "knowledge"
    assert not store_root.exists()

    assert approve_candidates(rundir, fixture_repo) == 2
    assert approve_candidates(rundir, fixture_repo) == 0
    selected = prime(rundir, fixture_repo, task, "implementation")
    assert any("validate payloads" in item.fact for item in selected)
    assert json.loads((rundir.path / "knowledge-candidates.json").read_text())
