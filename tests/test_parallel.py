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


def _two_task_run(regie_home, fixture_repo) -> tuple[RunDir, RunState]:
    rundir = RunDir.create(regie_home, "r1")
    run = RunState(id="r1", target_repo=str(fixture_repo), branch="regie/r1",
                   base_sha=head_sha(fixture_repo), stage="tasks")
    for task_id in ("T1", "T2"):
        run.tasks[task_id] = TaskState(spec=TaskSpec(
            id=task_id, title=task_id, profile="builder",
            criteria=["Given input When run Then output"],
            file_scope=[f"src/{task_id}.py"]))
    return rundir, run


def _run_batch(rundir, run, fixture_repo, monkeypatch, fake_run_task) -> None:
    monkeypatch.setattr("regie.pipeline.run_task", fake_run_task)
    cfg = SimpleNamespace(workflow=SimpleNamespace(max_parallel_tasks=2))
    _run_parallel_batch(
        rundir, run, cfg, fixture_repo, PipelineContext(), ["T1", "T2"])


def _halt_task(state, task_id) -> None:
    state.tasks[task_id].status = "failed"
    state.stage = "halted"
    state.halt_reason = f"build ladder exhausted on {task_id}"


def test_parallel_batch_keeps_finished_work_when_a_sibling_halts(
        regie_home, fixture_repo, monkeypatch):
    rundir, run = _two_task_run(regie_home, fixture_repo)

    def fake_run_task(_rd, state, task_id, _cfg, repo, _ctx):
        if task_id == "T1":
            (repo / "src" / "T1.py").write_text("VALUE = 1\n")
            commit_all(repo, "feat(T1): done")
            state.tasks["T1"].status = "done"
        else:
            _halt_task(state, "T2")

    _run_batch(rundir, run, fixture_repo, monkeypatch, fake_run_task)

    # Resume never re-runs a done task, so its commits must already be on
    # the run branch rather than on a task branch the batch deletes.
    assert run.stage == "halted"
    assert run.tasks["T1"].status == "done"
    assert (fixture_repo / "src" / "T1.py").read_text() == "VALUE = 1\n"
    assert git(fixture_repo, "log", "--format=%s",
               f"{run.base_sha}..HEAD").splitlines() == ["feat(T1): done"]


def test_parallel_batch_restarts_task_whose_worktree_is_discarded(
        regie_home, fixture_repo, monkeypatch):
    rundir, run = _two_task_run(regie_home, fixture_repo)

    def fake_run_task(_rd, state, task_id, _cfg, repo, _ctx):
        task = state.tasks[task_id]
        task.start_sha = head_sha(repo)
        (repo / "tests").mkdir(exist_ok=True)
        (repo / "tests" / f"test_{task_id}.py").write_text("def test(): assert False\n")
        commit_all(repo, f"test({task_id}): red")
        task.stage = "build"
        if task_id == "T1":
            task.status = "done"
        else:
            _halt_task(state, "T2")

    _run_batch(rundir, run, fixture_repo, monkeypatch, fake_run_task)

    # T2's red-test commit died with its worktree: resuming at "build" would
    # implement against tests that no longer exist.
    t2 = run.tasks["T2"]
    assert (t2.stage, t2.start_sha) == ("test", "")
    assert not (fixture_repo / "tests" / "test_T2.py").exists()


def test_parallel_batch_conflict_rolls_back_and_restarts_task(
        regie_home, fixture_repo, monkeypatch):
    rundir, run = _two_task_run(regie_home, fixture_repo)

    def fake_run_task(_rd, state, task_id, _cfg, repo, _ctx):
        if task_id == "T1":
            (repo / "src" / "shared.py").write_text("OWNER = 'T1'\n")
            commit_all(repo, "feat(T1): shared")
        else:
            (repo / "src" / "T2.py").write_text("VALUE = 2\n")
            commit_all(repo, "feat(T2): own file")
            (repo / "src" / "shared.py").write_text("OWNER = 'T2'\n")
            commit_all(repo, "feat(T2): shared")
        state.tasks[task_id].stage = "review"
        state.tasks[task_id].status = "done"

    _run_batch(rundir, run, fixture_repo, monkeypatch, fake_run_task)

    assert run.stage == "halted"
    assert "parallel integration conflict for T2" in run.halt_reason
    # T2's first commit applied before the conflict; it must not linger on
    # the run branch, and T2 must be re-run rather than trusted as done.
    assert git(fixture_repo, "log", "--format=%s",
               f"{run.base_sha}..HEAD").splitlines() == ["feat(T1): shared"]
    assert not (fixture_repo / "src" / "T2.py").exists()
    t2 = run.tasks["T2"]
    assert t2.status != "done"
    assert t2.stage == "test"
