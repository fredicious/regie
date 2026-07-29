import json
from pathlib import Path

from typer.testing import CliRunner

from regie.cli import app
from regie.gitops import commit_all, create_run_worktree, head_sha
from regie.models import Attempt, Binding, RunState, TaskSpec, TaskState
from regie.pipeline import reconcile
from regie.rundir import RunDir

runner = CliRunner()


def _toml(repo: Path) -> None:
    (repo / "regie.toml").write_text(
        'test_globs = ["tests/**"]\nbinding_strength = ["fake:m1"]\n'
        '[commands]\ntest = "true"\nlint = "true"\n')


def _last_run_id(home: Path) -> str:
    return max((home / "runs").iterdir()).name


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
    _toml(fixture_repo)
    base = head_sha(fixture_repo)
    # The worktree is created directly here (rather than via `regie run`) so the
    # test can seed .fake_agent.json into it before invoking `resume`.
    wt = create_run_worktree(fixture_repo, "regie/r1", base,
                             regie_home / "worktrees" / "r1")
    rd = RunDir.create(regie_home, "r1")
    run = RunState(id="r1", target_repo=str(fixture_repo), branch="regie/r1",
                   base_sha=base, worktree_path=str(wt),
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
    # than immediately re-tripping _should_halt. The fake agent reads
    # .fake_agent.json from ITS cwd, which is the worktree.
    (wt / ".fake_agent.json").write_text(json.dumps(
        {"result": {"outcome": "blocked", "blocked_question": "?"}}))
    result = runner.invoke(app, ["resume", "r1", "--repo", str(fixture_repo),
                                 "--profiles", str(fake_profiles)])
    assert result.exit_code == 1

    final = RunDir.open(regie_home, "r1").read_state()
    assert len(final.tasks["T1"].attempts["build"]) == 1


def test_run_command_executes_tasks_from_tasks_json(regie_home, fixture_repo,
                                                    fake_profiles, tmp_path):
    _toml(fixture_repo)
    brief = tmp_path / "brief.md"
    brief.write_text("# Do the thing")
    (tmp_path / "tasks.json").write_text(json.dumps([{
        "id": "T1", "title": "t", "profile": "builder", "criteria": ["c"]}]))
    # The run worktree is checked out from base_sha, so the fake agent's script
    # must be committed to be present in the worktree.
    (fixture_repo / ".fake_agent.json").write_text(json.dumps(
        {"result": {"outcome": "blocked", "blocked_question": "?"}}))
    commit_all(fixture_repo, "chore: fake agent script")
    result = runner.invoke(app, ["run", str(brief), "--repo", str(fixture_repo),
                                 "--profiles", str(fake_profiles)])
    assert result.exit_code == 1  # halted on blocked
    assert "halted" in result.output.lower()


def test_run_missing_brief_friendly_error(regie_home, fixture_repo):
    result = runner.invoke(app, ["run", "/nope/brief.md", "--repo", str(fixture_repo)])
    assert result.exit_code == 2 and "brief" in result.output.lower()


def test_run_duplicate_id_friendly_error(regie_home, fixture_repo, fake_profiles, tmp_path):
    brief = tmp_path / "dup.md"
    brief.write_text("x")
    (tmp_path / "tasks.json").write_text("[]")
    _toml(fixture_repo)
    runner.invoke(app, ["run", str(brief), "--repo", str(fixture_repo),
                        "--profiles", str(fake_profiles)])
    result = runner.invoke(app, ["run", str(brief), "--repo", str(fixture_repo),
                                 "--profiles", str(fake_profiles)])
    assert result.exit_code == 2 and "already exists" in result.output


def test_run_executes_in_worktree_not_checkout(regie_home, fixture_repo, fake_profiles,
                                               tmp_path):
    brief = tmp_path / "wtrun.md"
    brief.write_text("x")
    (tmp_path / "tasks.json").write_text(json.dumps([{"id": "T1", "title": "t",
        "profile": "builder", "criteria": ["c"]}]))
    _toml(fixture_repo)
    (fixture_repo / ".fake_agent.json").write_text(json.dumps(
        {"result": {"outcome": "blocked", "blocked_question": "?"}}))
    commit_all(fixture_repo, "chore: fake agent script")
    result = runner.invoke(app, ["run", str(brief), "--repo", str(fixture_repo),
                                 "--profiles", str(fake_profiles)])
    assert result.exit_code == 1, result.output
    state = RunDir.open(regie_home, _last_run_id(regie_home)).read_state()
    assert state.worktree_path and state.worktree_path != str(fixture_repo)
    # the fake agent reads .fake_agent.json from ITS cwd — the worktree:
    # blocked halt proves the dispatch happened in the worktree only if the file
    # exists there; base commit contains no .fake_agent.json, so instead assert
    # the worktree exists and is a git worktree of fixture_repo
    assert (Path(state.worktree_path) / ".git").exists()


def test_clean_removes_worktree_and_branch(regie_home, fixture_repo, fake_profiles,
                                           tmp_path):
    brief = tmp_path / "cleanme.md"
    brief.write_text("x")
    (tmp_path / "tasks.json").write_text(json.dumps([{"id": "T1", "title": "t",
        "profile": "builder", "criteria": ["c"]}]))
    _toml(fixture_repo)
    (fixture_repo / ".fake_agent.json").write_text(json.dumps(
        {"result": {"outcome": "blocked", "blocked_question": "?"}}))
    commit_all(fixture_repo, "chore: fake agent script")
    runner.invoke(app, ["run", str(brief), "--repo", str(fixture_repo),
                        "--profiles", str(fake_profiles)])

    rid = _last_run_id(regie_home)
    result = runner.invoke(app, ["clean", rid, "--repo", str(fixture_repo)])
    assert result.exit_code == 0
    assert not Path(RunDir.open(regie_home, rid).read_state().worktree_path).exists()
