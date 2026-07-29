import json

from typer.testing import CliRunner

from regie.cli import app
from regie.models import Attempt, Binding, RunState, TaskSpec, TaskState
from regie.pipeline import reconcile
from regie.rundir import RunDir

runner = CliRunner()


def _seed_run(regie_home, fixture_repo, status="pending") -> str:
    rd = RunDir.create(regie_home, "r1")
    run = RunState(id="r1", target_repo=str(fixture_repo), branch="regie/r1",
                   stage="tasks")
    run.tasks["T1"] = TaskState(
        spec=TaskSpec(id="T1", title="t", profile="builder", criteria=["c"]),
        status=status)
    rd.write_state(run)
    return "r1"


def test_status_prints_task_lines(regie_home, fixture_repo):
    _seed_run(regie_home, fixture_repo)
    result = runner.invoke(app, ["status", "r1"])
    assert result.exit_code == 0
    assert "T1" in result.output and "pending" in result.output


def test_reconcile_marks_orphaned_intent_failed_and_cleans_worktree(
        regie_home, fixture_repo):
    _seed_run(regie_home, fixture_repo)
    rd = RunDir.open(regie_home, "r1")
    run = rd.read_state()
    rd.append_intent({"task": "T1", "stage": "build", "attempt": 1,
                      "binding": {"cli": "fake", "model": "m1"}})
    (fixture_repo / "src" / "orphan.py").write_text("dirty\n")  # in-flight edit
    count = reconcile(rd, run, fixture_repo)
    assert count == 1
    assert run.tasks["T1"].attempts["build"][0].outcome == "failed"
    assert not (fixture_repo / "src" / "orphan.py").exists()


def test_resume_after_halt_clears_attempts_and_redispatches(regie_home, fixture_repo,
                                                             fake_profiles):
    (fixture_repo / "regie.toml").write_text(
        'test_globs = ["tests/**"]\nbinding_strength = ["fake:m1"]\n'
        '[commands]\ntest = "true"\nlint = "true"\n')
    rd = RunDir.create(regie_home, "r1")
    run = RunState(id="r1", target_repo=str(fixture_repo), branch="regie/r1",
                   stage="halted", halt_reason="build ladder exhausted on T1")
    task = TaskState(spec=TaskSpec(id="T1", title="t", profile="builder",
                                   criteria=["c"]),
                     stage="build", status="failed")
    stale = Binding(cli="fake", model="m1")
    task.attempts["build"] = [Attempt(binding=stale, outcome="failed") for _ in range(3)]
    run.tasks["T1"] = task
    rd.write_state(run)

    # a fresh ladder should dispatch once and halt again on this single
    # "blocked" response, proving the 3 stale attempts were cleared rather
    # than immediately re-tripping _should_halt.
    (fixture_repo / ".fake_agent.json").write_text(json.dumps(
        {"result": {"outcome": "blocked", "blocked_question": "?"}}))
    result = runner.invoke(app, ["resume", "r1", "--repo", str(fixture_repo),
                                 "--profiles", str(fake_profiles)])
    assert result.exit_code == 1

    final = RunDir.open(regie_home, "r1").read_state()
    assert len(final.tasks["T1"].attempts["build"]) == 1


def test_run_command_executes_tasks_from_tasks_json(regie_home, fixture_repo,
                                                    fake_profiles, tmp_path):
    (fixture_repo / "regie.toml").write_text(
        'test_globs = ["tests/**"]\nbinding_strength = ["fake:m1"]\n'
        '[commands]\ntest = "true"\nlint = "true"\n')
    brief = tmp_path / "brief.md"
    brief.write_text("# Do the thing")
    (tmp_path / "tasks.json").write_text(json.dumps([{
        "id": "T1", "title": "t", "profile": "builder", "criteria": ["c"]}]))
    (fixture_repo / ".fake_agent.json").write_text(json.dumps(
        {"result": {"outcome": "blocked", "blocked_question": "?"}}))
    result = runner.invoke(app, ["run", str(brief), "--repo", str(fixture_repo),
                                 "--profiles", str(fake_profiles)])
    assert result.exit_code == 1  # halted on blocked
    assert "halted" in result.output.lower()
