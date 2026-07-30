import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from regie.cli import app
from regie.gitops import commit_all, create_run_worktree, head_sha
from regie.models import Attempt, Binding, GateResult, RunState, TaskSpec, TaskState
from regie.pipeline import reconcile
from regie.rundir import RunDir

runner = CliRunner()


def _toml(repo: Path) -> None:
    (repo / "regie.toml").write_text(
        'test_globs = ["tests/**"]\n'
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
                                 "--profiles", str(fake_profiles),
                                 "--tasks-file", str(tmp_path / "tasks.json")])
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
                        "--profiles", str(fake_profiles),
                        "--tasks-file", str(tmp_path / "tasks.json")])
    result = runner.invoke(app, ["run", str(brief), "--repo", str(fixture_repo),
                                 "--profiles", str(fake_profiles),
                                 "--tasks-file", str(tmp_path / "tasks.json")])
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
                                 "--profiles", str(fake_profiles),
                                 "--tasks-file", str(tmp_path / "tasks.json")])
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
                        "--profiles", str(fake_profiles),
                        "--tasks-file", str(tmp_path / "tasks.json")])

    rid = _last_run_id(regie_home)
    result = runner.invoke(app, ["clean", rid, "--repo", str(fixture_repo)])
    assert result.exit_code == 0
    assert not Path(RunDir.open(regie_home, rid).read_state().worktree_path).exists()


def test_approve_flips_stage_to_tasks(regie_home, fixture_repo):
    rd = RunDir.create(regie_home, "r1")
    run = RunState(id="r1", target_repo=str(fixture_repo), branch="regie/r1",
                   stage="approve")
    rd.write_state(run)
    result = runner.invoke(app, ["approve", "r1"])
    assert result.exit_code == 0
    assert RunDir.open(regie_home, "r1").read_state().stage == "tasks"


def test_approve_on_non_approve_run_is_an_error(regie_home, fixture_repo):
    _seed_run(regie_home, fixture_repo)  # stage="tasks"
    result = runner.invoke(app, ["approve", "r1"])
    assert result.exit_code == 2


def test_approve_on_nonexistent_run_is_a_friendly_error(regie_home):
    result = runner.invoke(app, ["approve", "no-such-run"])
    assert result.exit_code == 2
    assert "not found" in result.output.lower()


@pytest.mark.parametrize("command", ["resume", "status", "clean"])
def test_open_rundir_friendly_error_across_commands(regie_home, fixture_repo,
                                                     fake_profiles, command):
    _toml(fixture_repo)
    args = [command, "no-such-run"]
    if command in ("resume", "clean"):
        args += ["--repo", str(fixture_repo)]
    if command == "resume":
        args += ["--profiles", str(fake_profiles)]
    result = runner.invoke(app, args)
    assert result.exit_code == 2
    assert "not found" in result.output.lower()


def test_run_guard_refused_leaves_no_run_dir(regie_home, fixture_repo, fake_profiles, tmp_path):
    from datetime import UTC, datetime

    from regie.cli import _repo_marker_path

    # Simulate another live run against the same target repo: create its run
    # dir, hold its lock (standing in for a live process), and mark it live
    # in the repo-marker file the guard consults.
    other = RunDir.create(regie_home, "other-run")
    other.acquire_lock()
    marker = _repo_marker_path(regie_home, fixture_repo)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("other-run")

    _toml(fixture_repo)
    brief = tmp_path / "guarded.md"
    brief.write_text("x")
    result = runner.invoke(app, ["run", str(brief), "--repo", str(fixture_repo),
                                 "--profiles", str(fake_profiles)])

    assert result.exit_code == 2
    assert "another run is live" in result.output
    run_id = f"{datetime.now(tz=UTC).date().isoformat()}-guarded"
    assert not (regie_home / "runs" / run_id).exists()


def test_resume_after_halt_resets_escape_hatch(regie_home, fixture_repo, fake_profiles):
    _toml(fixture_repo)
    rd = RunDir.create(regie_home, "resc")
    run = RunState(id="resc", target_repo=str(fixture_repo), branch="regie/resc",
                   stage="halted", halt_reason="build ladder exhausted on T1",
                   worktree_path=str(fixture_repo))
    run.tasks["T1"] = TaskState(
        spec=TaskSpec(id="T1", title="t", profile="builder", criteria=["c"]),
        status="failed", escaped=True)
    rd.write_state(run)
    (fixture_repo / ".fake_agent.json").write_text(json.dumps(
        {"result": {"outcome": "blocked", "blocked_question": "?"}}))
    runner.invoke(app, ["resume", "resc", "--repo", str(fixture_repo),
                        "--profiles", str(fake_profiles)])
    state = rd.read_state()
    assert state.tasks["T1"].escaped is False


def test_reconcile_respects_reset_marker(regie_home, fixture_repo):
    rd = RunDir.create(regie_home, "rmark")
    run = RunState(id="rmark", target_repo=str(fixture_repo), branch="regie/rmark")
    run.tasks["T1"] = TaskState(
        spec=TaskSpec(id="T1", title="t", profile="builder", criteria=["c"]))
    rd.write_state(run)
    for n in range(3):
        rd.append_intent({"task": "T1", "stage": "build", "attempt": n + 1,
                          "binding": {"cli": "fake", "model": "m1"}})
    rd.append_intent({"task": "T1", "reset": True})
    rd.append_intent({"task": "T1", "stage": "build", "attempt": 1,
                      "binding": {"cli": "fake", "model": "m1"}})
    count = reconcile(rd, run, fixture_repo)
    # only the single post-marker intent is an orphan; the three pre-marker
    # intents are dead history
    assert count == 1
    assert len(run.tasks["T1"].attempts["build"]) == 1


def _seed_artifact_run(regie_home, rid="art1"):
    rd = RunDir.create(regie_home, rid)
    (rd.path / "spec").mkdir()
    (rd.path / "spec" / "spec.md").write_text("# The Spec\nbody")
    run = RunState(id=rid, target_repo="/x", branch=f"regie/{rid}", stage="halted",
                   halt_reason="build ladder exhausted on T1")
    run.tasks["T1"] = TaskState(
        spec=TaskSpec(id="T1", title="t", profile="builder", criteria=["c"]),
        status="failed", stage="build")
    run.tasks["T1"].attempts["build"].append(Attempt(
        binding=Binding(cli="fake", model="m1"), outcome="failed",
        gate_results=[GateResult(name="diff-guard", passed=False,
                                 detail="test files modified: tests/x.py")]))
    rd.write_state(run)
    (rd.task_dir("T1") / "note-build.md").write_text("Previous attempt failed gates:\n- diff-guard: ...")
    (rd.task_dir("T1") / "attempt-1.out").write_text("transcript")
    return rd


def test_spec_command_prints_spec(regie_home):
    _seed_artifact_run(regie_home)
    result = runner.invoke(app, ["spec", "art1"])
    assert result.exit_code == 0 and "# The Spec" in result.output


def test_spec_command_missing_spec_friendly(regie_home):
    rd = RunDir.create(regie_home, "nospec")
    rd.write_state(RunState(id="nospec", target_repo="/x", branch="b"))
    result = runner.invoke(app, ["spec", "nospec"])
    assert result.exit_code == 2 and "no spec" in result.output.lower()


def test_open_command_prints_paths(regie_home):
    _seed_artifact_run(regie_home)
    result = runner.invoke(app, ["open", "art1"])
    assert result.exit_code == 0
    assert "spec.md" in result.output and "state.json" in result.output


def test_doctor_summarizes_halt_and_suggests(regie_home):
    _seed_artifact_run(regie_home)
    result = runner.invoke(app, ["doctor", "art1"])
    assert result.exit_code == 0
    out = result.output
    assert "build ladder exhausted" in out
    assert "diff-guard" in out                  # failing gate named
    assert "note-build.md" in out               # evidence pointer
    assert "regie resume" in out                # suggested action


def test_doctor_on_healthy_run(regie_home):
    rd = RunDir.create(regie_home, "ok1")
    rd.write_state(RunState(id="ok1", target_repo="/x", branch="b", stage="done",
                            pr_url="https://x/pr/1"))
    result = runner.invoke(app, ["doctor", "ok1"])
    assert result.exit_code == 0 and "done" in result.output
