from types import SimpleNamespace

from regie.gitops import commit_all, git, head_sha
from regie.models import RunState, TaskSpec, TaskState
from regie.pipeline import PipelineContext, _run_parallel_batch
from regie.rundir import RunDir


def test_parallel_batch_isolates_then_integrates_tasks(
        regie_home, fixture_repo, monkeypatch):
    rundir = RunDir.create(regie_home, "r1")
    run = RunState(id="r1", target_repo=str(fixture_repo), branch="regie/r1",
                   base_sha=head_sha(fixture_repo), stage="tasks")
    for task_id in ("T1", "T2"):
        spec = TaskSpec(
            id=task_id, title=task_id, profile="builder",
            criteria=["Given input When run Then output"],
            file_scope=[f"src/{task_id}.py"])
        run.tasks[task_id] = TaskState(spec=spec)

    def fake_run_task(_rd, state, task_id, _cfg, repo, _ctx):
        (repo / "src" / f"{task_id}.py").write_text(f"VALUE = '{task_id}'\n")
        commit_all(repo, f"feat({task_id}): parallel")
        state.tasks[task_id].status = "done"

    monkeypatch.setattr("regie.pipeline.run_task", fake_run_task)
    cfg = SimpleNamespace(workflow=SimpleNamespace(max_parallel_tasks=2))
    _run_parallel_batch(
        rundir, run, cfg, fixture_repo, PipelineContext(), ["T1", "T2"])

    assert (fixture_repo / "src" / "T1.py").exists()
    assert (fixture_repo / "src" / "T2.py").exists()
    assert len(git(fixture_repo, "log", "--format=%s", f"{run.base_sha}..HEAD").splitlines()) == 2
    assert all(task.status == "done" for task in run.tasks.values())
