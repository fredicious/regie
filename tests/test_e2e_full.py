"""Plan B exit criterion: the full pipeline -- brief in, real planner stage,
approve checkpoint, resume through tasks/finalize/pr -- as one flow, with no
--tasks-file escape hatch. A second test proves --autonomous collapses this
into a single `regie run` invocation.

Dispatches are driven by monkeypatching pipeline.run_agent directly (a queue
of canned AgentResults + worktree writes) -- the same technique
test_finalize_pr.py uses for its pr_stage tests -- deliberately NOT the
FakeAdapter subprocess's file-based `.fake_agent_queue` convention used by
test_e2e.py. That queue tracks its position by renaming a scratch file inside
the worktree; the review stage never calls commit_all (only test/build do),
so a review dispatch's rename is left uncommitted. finalize_stage's discard
step (`git checkout -- .` + `git clean -fd`, which deliberately strips
ungated leftovers so they can never enter the squashed PR) then reverts that
rename, resurrecting an "already consumed" queue entry -- so a later dispatch
(pr_stage's scribe) picks up stale content instead of its own. That's a
collision between test-only scaffolding and a genuine pipeline invariant, not
a production bug (real adapters never touch a queue file), so the fix
belongs in the test's dispatch mechanism, matching the precedent already
established in test_finalize_pr.py.
"""
import gc
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from regie import pipeline
from regie.agents.base import AgentResult
from regie.cli import app
from regie.gitops import git
from regie.rundir import RunDir

runner = CliRunner()


def _stub_gh_green(tmp_path, monkeypatch):
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir(parents=True, exist_ok=True)
    gh.write_text("#!/bin/sh\n"
                  'if [ "$1 $2" = "pr create" ]; then echo "https://github.com/x/y/pull/1"; exit 0; fi\n'
                  'if [ "$1 $2" = "pr checks" ]; then echo "SUCCESS"; exit 0; fi\n'
                  'exit 1\n')
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{gh.parent}:{os.environ['PATH']}")


def _dispatch_queue(monkeypatch, worktree, specs):
    """Drive every dispatch in the run (planner, test/build/review per task,
    scribe) with canned outcomes, in order. Each spec may declare `writes` --
    files to materialize in the worktree, standing in for a real agent's
    edits -- applied before the canned AgentResult is returned."""
    queue = list(specs)

    def _fake_run_agent(rundir, task_id, stage, attempt_no, req):
        spec = queue.pop(0)
        for rel, content in spec.get("writes", {}).items():
            path = worktree / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        return AgentResult(**spec["result"])

    monkeypatch.setattr(pipeline, "run_agent", _fake_run_agent)


DIVIDE_STUB = "def divide(a, b):\n    raise NotImplementedError\n"
DIVIDE_IMPL = "def divide(a, b):\n    return a // b\n"
POWER_STUB = "def power(a, b):\n    raise NotImplementedError\n"
POWER_IMPL = "def power(a, b):\n    return a ** b\n"
ADD = "def add(a, b):\n    return a + b\n"

TEST_DIV = ("from src.calc import divide\n\n"
            "def test_div():\n    assert divide(6, 3) == 2\n")
TEST_POW = ("from src.calc import power\n\n"
            "def test_pow():\n    assert power(2, 3) == 8\n")

PLAN = {"spec_markdown": "# Two functions\n\ndivide and power.\n", "tasks": [
    {"id": "T1", "title": "divide", "profile": "builder",
     "criteria": ["Given 6 and 3, When divide is called, Then it returns 2"],
     "planned_tests": ["test_div"], "depends_on": []},
    {"id": "T2", "title": "power", "profile": "builder",
     "criteria": ["Given 2 and 3, When power is called, Then it returns 8"],
     "planned_tests": ["test_pow"], "depends_on": ["T1"]},
]}

SCRIBE = {"result": {"outcome": "done", "structured": {
    "commit_messages": ["feat(T1): implement divide", "feat(T2): implement power"],
    "pr_title": "Add divide and power", "pr_body": "Adds two arithmetic helpers."}}}

# Dispatch order for the whole pipeline:
#   0: planner            -> PLAN (2 tasks)
#   1-3: T1 test/build/review (review flags one minor finding)
#   4-6: T2 test/build/review
#   7: scribe (pr_stage)
QUEUE = [
    {"result": {"outcome": "done", "structured": PLAN}},
    # T1 test stage: red test + NotImplementedError stub.
    {"result": {"outcome": "done"}, "writes": {
        "tests/test_div.py": TEST_DIV,
        "src/calc.py": ADD + "\n" + DIVIDE_STUB}},
    # T1 build stage: implement divide.
    {"result": {"outcome": "done"}, "writes": {
        "src/calc.py": ADD + "\n" + DIVIDE_IMPL}},
    # T1 review stage: one minor finding -- must reach the PR body, never loop.
    {"result": {"outcome": "done", "structured": {"findings": [
        {"severity": "minor", "title": "naming nit", "detail": "consider renaming"}]}}},
    # T2 test stage: red test + NotImplementedError stub, keeping divide.
    {"result": {"outcome": "done"}, "writes": {
        "tests/test_pow.py": TEST_POW,
        "src/calc.py": ADD + "\n" + DIVIDE_IMPL + "\n" + POWER_STUB}},
    # T2 build stage: implement power, keeping divide.
    {"result": {"outcome": "done"}, "writes": {
        "src/calc.py": ADD + "\n" + DIVIDE_IMPL + "\n" + POWER_IMPL}},
    # T2 review stage: clean.
    {"result": {"outcome": "done", "structured": {"findings": []}}},
    # pr_stage's scribe dispatch.
    SCRIBE,
]


def _predict_run_id(brief: Path) -> str:
    # Mirrors cli.run()'s own run_id derivation exactly, so the test can
    # predict the worktree path before invoking the CLI at all.
    return f"{datetime.now(tz=UTC).date().isoformat()}-{brief.stem}"


def _setup(fixture_repo, tmp_path):
    (fixture_repo / "regie.toml").write_text(
        'test_globs = ["tests/**"]\nbinding_strength = ["fake:m1"]\n'
        '[commands]\ntest = "python -m pytest tests -q"\nlint = "true"\n')
    brief = tmp_path / "feature.md"
    brief.write_text("# two functions\n\ndivide and power.\n")
    return brief


def _assert_full_pipeline(regie_home, run_id):
    rd = RunDir.open(regie_home, run_id)
    state = rd.read_state()
    assert state.stage == "done"
    assert state.pr_url == "https://github.com/x/y/pull/1"
    assert all(t.status == "done" for t in state.tasks.values())

    wt = Path(state.worktree_path)
    log_shas = git(wt, "log", "--format=%H", f"{state.base_sha}..HEAD").split()
    assert len(log_shas) == 3  # one commit per task + the spec commit

    subjects = git(wt, "log", "--format=%s", f"{state.base_sha}..HEAD").splitlines()
    assert subjects[0] == f"docs(spec): {run_id}"
    assert (wt / "specs" / f"{run_id}.md").exists()

    assert git(wt, "rev-parse", f"refs/regie/backup/{run_id}")

    assert (rd.path / "spec" / "spec.md").exists()
    body = (rd.path / "pr-body.md").read_text()
    assert "Review notes" in body
    assert "naming nit" in body

    log = git(wt, "log", "--name-only", f"{state.base_sha}..HEAD")
    for path in log.splitlines():
        assert ".fake_agent" not in path
        assert ".regie_schema" not in path

    return state


def test_full_pipeline_with_approve_checkpoint(regie_home, fixture_repo, remote_repo,
                                               fake_profiles, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "CI_POLL_SECONDS", 0)
    _stub_gh_green(tmp_path, monkeypatch)
    brief = _setup(fixture_repo, tmp_path)
    run_id = _predict_run_id(brief)
    worktree = regie_home / "worktrees" / run_id
    _dispatch_queue(monkeypatch, worktree, QUEUE)

    result = runner.invoke(app, ["run", str(brief), "--repo", str(fixture_repo),
                                 "--profiles", str(fake_profiles)])
    assert result.exit_code == 0, result.output
    assert "run `regie approve" in result.output
    assert (regie_home / "runs" / run_id).is_dir()
    assert RunDir.open(regie_home, run_id).read_state().stage == "approve"

    result = runner.invoke(app, ["approve", run_id])
    assert result.exit_code == 0, result.output

    # Drop references to the completed `run` invocation before resume
    # acquires the run's lock -- matches test_e2e.py's precedent for
    # releasing the flock deterministically instead of relying on CPython
    # refcounting timing across CliRunner invocations.
    del result
    gc.collect()

    result = runner.invoke(app, ["resume", run_id, "--repo", str(fixture_repo),
                                 "--profiles", str(fake_profiles)])
    assert result.exit_code == 0, result.output

    state = _assert_full_pipeline(regie_home, run_id)
    remote_heads = git(remote_repo, "branch", "--format=%(refname:short)")
    assert state.branch in remote_heads


def test_full_pipeline_autonomous_skips_approve(regie_home, fixture_repo, remote_repo,
                                                fake_profiles, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "CI_POLL_SECONDS", 0)
    _stub_gh_green(tmp_path, monkeypatch)
    brief = _setup(fixture_repo, tmp_path)
    run_id = _predict_run_id(brief)
    worktree = regie_home / "worktrees" / run_id
    _dispatch_queue(monkeypatch, worktree, QUEUE)

    result = runner.invoke(app, ["run", str(brief), "--repo", str(fixture_repo),
                                 "--profiles", str(fake_profiles), "--autonomous"])
    assert result.exit_code == 0, result.output
    assert "run `regie approve" not in result.output

    _assert_full_pipeline(regie_home, run_id)
