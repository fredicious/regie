"""Plan A exit criterion: a two-task run completes through the fake adapter,
and a simulated crash mid-run resumes to completion."""
import gc
import json
import os
import stat
import subprocess

from typer.testing import CliRunner

from regie.cli import app
from regie.gitops import commit_all
from regie.rundir import RunDir

runner = CliRunner()


def _stub_gh_green(tmp_path, monkeypatch):
    """A `gh` stub that always reports CI green, so the PR stage (Task 9)
    reaches "done" without needing real GitHub access."""
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir(parents=True, exist_ok=True)
    gh.write_text("#!/bin/sh\n"
                  'if [ "$1 $2" = "pr create" ]; then echo "https://github.com/x/y/pull/1"; exit 0; fi\n'
                  'if [ "$1 $2" = "pr checks" ]; then echo "SUCCESS"; exit 0; fi\n'
                  'exit 1\n')
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{gh.parent}:{os.environ['PATH']}")

TASKS = [
    {"id": "T1", "title": "divide", "profile": "builder",
     "criteria": ["Given 6,3 When divide Then 2"]},
    {"id": "T2", "title": "power", "profile": "builder",
     "criteria": ["Given 2,3 When power Then 8"], "depends_on": ["T1"]},
]

# Real pytest is the test command, so the red gate genuinely sees red. Each
# dispatch is scripted via the queue convention (fake.py: consumes
# .fake_agent_queue/0.json, 1.json, ... in order). Order: T1 red-test write ->
# T1 build write -> T1 clean review -> T2 red-test write -> T2 build write ->
# T2 clean review. T2's red-test/build writes must keep T1's already-committed
# `divide` implementation (calc.py is fully overwritten each dispatch, and the
# real test suite runs across all of tests/), so calc.py content is unioned
# across both tasks from T2 onward.

DIVIDE_STUB = "def divide(a, b):\n    raise NotImplementedError\n"
DIVIDE_IMPL = "def divide(a, b):\n    return a // b\n"
POWER_STUB = "def power(a, b):\n    raise NotImplementedError\n"
POWER_IMPL = "def power(a, b):\n    return a ** b\n"
ADD = "def add(a, b):\n    return a + b\n"

TEST_DIV = ("from src.calc import divide\n\n"
            "def test_div():\n    assert divide(6, 3) == 2\n")
TEST_POW = ("from src.calc import power\n\n"
            "def test_pow():\n    assert power(2, 3) == 8\n")

CLEAN_REVIEW = {"result": {"outcome": "done", "structured": {"findings": []}}}
SCRIBE_ENTRY = {"result": {"outcome": "done", "structured": {
    "commit_messages": ["feat(calc): divide", "feat(calc): power"],
    "pr_title": "feat: two functions", "pr_body": "adds divide and power"}}}

QUEUE = [
    # T1 test stage: red test + NotImplementedError stub.
    {"result": {"outcome": "done"}, "writes": {
        "tests/test_div.py": TEST_DIV,
        "src/calc.py": ADD + "\n" + DIVIDE_STUB}},
    # T1 build stage: implement divide.
    {"result": {"outcome": "done"}, "writes": {
        "src/calc.py": ADD + "\n" + DIVIDE_IMPL}},
    # T1 review stage: clean.
    CLEAN_REVIEW,
    # T2 test stage: red test + NotImplementedError stub, keeping divide.
    {"result": {"outcome": "done"}, "writes": {
        "tests/test_pow.py": TEST_POW,
        "src/calc.py": ADD + "\n" + DIVIDE_IMPL + "\n" + POWER_STUB}},
    # T2 build stage: implement power, keeping divide.
    {"result": {"outcome": "done"}, "writes": {
        "src/calc.py": ADD + "\n" + DIVIDE_IMPL + "\n" + POWER_IMPL}},
    # T2 review stage: clean.
    CLEAN_REVIEW,
]


def _queue(fixture_repo, entries, start=0):
    qdir = fixture_repo / ".fake_agent_queue"
    qdir.mkdir(exist_ok=True)
    for i, entry in enumerate(entries):
        (qdir / f"{start + i}.json").write_text(json.dumps(entry))


def _dispatch_queue(monkeypatch, entries):
    """Drive dispatches via monkeypatched pipeline.run_agent (the file-based
    FakeAdapter queue is tracked content whose consumption-renames get
    restored by the pipeline's legitimate discard steps, replaying stale
    entries). Writes target req.cwd — always the run worktree."""
    from regie import pipeline as _pl
    queue = list(entries)

    def _fake(rundir, task_id, stage, attempt_no, req):
        rundir.append_intent({"task": task_id, "stage": stage,
                              "attempt": attempt_no,
                              "binding": req.binding.model_dump()})
        spec = queue.pop(0)
        for rel, content in spec.get("writes", {}).items():
            path = req.cwd / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        rundir.append_event({"kind": "attempt", "task": task_id, "stage": stage,
                             "attempt": attempt_no,
                             "outcome": spec["result"].get("outcome", "done"),
                             "turns": spec["result"].get("turns", 0),
                             "usage": {}})
        from regie.agents.base import AgentResult
        return AgentResult(**spec["result"])

    monkeypatch.setattr(_pl, "run_agent", _fake)
    return queue


def _setup(regie_home, fixture_repo, fake_profiles, tmp_path, remote_repo=None):
    (fixture_repo / "regie.toml").write_text(
        'test_globs = ["tests/**"]\nbinding_strength = ["fake:m1"]\n'
        '[commands]\ntest = "python -m pytest tests -q"\nlint = "true"\n')
    brief = tmp_path / "feature.md"
    brief.write_text("# two functions")
    (tmp_path / "tasks.json").write_text(json.dumps(TASKS))
    commit_all(fixture_repo, "chore: pin regie.toml")
    if remote_repo is not None:
        # finalize_stage fetches/rebases onto origin/main, so origin must have
        # this commit too -- otherwise the run's base_sha (resolved from
        # origin/main) would predate the fake-agent-queue commit.
        subprocess.run(["git", "-C", str(fixture_repo), "push", "-q", "origin", "main"],
                      check=True)
    return brief


def test_full_run_reaches_pr(regie_home, fixture_repo, remote_repo, fake_profiles,
                             tmp_path, monkeypatch):
    from regie import pipeline as _pl
    monkeypatch.setattr(_pl, "CI_POLL_SECONDS", 0)
    _stub_gh_green(tmp_path, monkeypatch)
    brief = _setup(regie_home, fixture_repo, fake_profiles, tmp_path, remote_repo)
    _dispatch_queue(monkeypatch, QUEUE + [SCRIBE_ENTRY])
    result = runner.invoke(app, ["run", str(brief), "--repo", str(fixture_repo),
                                 "--profiles", str(fake_profiles),
                                 "--tasks-file", str(tmp_path / "tasks.json")])
    assert result.exit_code == 0, result.output
    run_id = max((regie_home / "runs").iterdir()).name
    state = RunDir.open(regie_home, run_id).read_state()
    assert state.stage == "done"
    assert state.pr_url == "https://github.com/x/y/pull/1"
    assert all(t.status == "done" for t in state.tasks.values())

    log = subprocess.run(
        ["git", "-C", state.worktree_path, "log", "--name-only",
         f"{state.base_sha}..HEAD"], capture_output=True, text=True, check=True
    ).stdout
    assert ".regie_schema.json" not in log
    # No spec commit here: the --tasks-file path skips the planner, so there
    # is no spec to publish (spec-in-PR is covered by test_e2e_full).
    assert "specs/" not in log


def test_crash_then_resume_completes(regie_home, fixture_repo, remote_repo,
                                     fake_profiles, tmp_path, monkeypatch):
    _stub_gh_green(tmp_path, monkeypatch)
    brief = _setup(regie_home, fixture_repo, fake_profiles, tmp_path, remote_repo)
    # Crash injection: on the SECOND dispatch, do what the real dispatch.run_agent
    # does first -- write the WAL intent -- then crash before an Attempt is ever
    # recorded in state, and before the fake script runs (so an uncommitted edit
    # is left behind, simulating a killed agent's in-flight write). This is the
    # exact scenario the WAL/reconcile machinery exists for: intent logged,
    # no corresponding attempt, worktree dirty.
    from regie import pipeline
    _dispatch_queue(monkeypatch, QUEUE + [SCRIBE_ENTRY])
    monkeypatch.setattr(pipeline, "CI_POLL_SECONDS", 0)
    real = pipeline.run_agent  # the queue-driven fake installed above
    calls = {"n": 0, "dirty": None}

    def crashing(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            rundir, task_id, stage, attempt_no, req = args
            # req.cwd is the run's worktree (dispatch always runs there, never
            # the user's checkout) -- the in-flight edit lands there too.
            dirty = req.cwd / "src" / "dirty.py"
            calls["dirty"] = dirty
            dirty.write_text("dirty\n")  # in-flight edit the crash never cleaned up
            rundir.append_intent({"task": task_id, "stage": stage,
                                  "attempt": attempt_no,
                                  "binding": req.binding.model_dump()})
            raise KeyboardInterrupt  # simulated hard crash after WAL write
        return real(*args, **kwargs)

    monkeypatch.setattr(pipeline, "run_agent", crashing)
    result = runner.invoke(app, ["run", str(brief), "--repo", str(fixture_repo),
                                 "--profiles", str(fake_profiles),
                                 "--tasks-file", str(tmp_path / "tasks.json")])
    assert result.exit_code != 0  # crashed
    dirty = calls["dirty"]
    assert dirty.exists()  # the in-flight edit landed before the crash

    # In a real crash the OS closes the run.lock fd (and its flock) when the
    # process dies. Here the crash is simulated in-process, and click's
    # captured traceback keeps the crashed frame -- and its RunDir.lock fd --
    # alive via a reference cycle. Drop it and collect to release the lock,
    # matching what process death gives us for free.
    del result
    gc.collect()

    # The crash fired before entry 1 was consumed, so the in-memory dispatch
    # queue resumes exactly where it stopped; reconcile's synthetic failed
    # attempt fills the ladder slot without consuming a dispatch.
    run_id = max((regie_home / "runs").iterdir()).name

    monkeypatch.setattr(pipeline, "run_agent", real)
    result = runner.invoke(app, ["resume", run_id, "--repo", str(fixture_repo),
                                 "--profiles", str(fake_profiles)])
    assert result.exit_code == 0, result.output
    assert "reconciled 1 orphaned attempt" in result.output

    state = RunDir.open(regie_home, run_id).read_state()
    assert not dirty.exists()  # reconcile's git clean discarded the crash's edit
    assert state.tasks["T1"].attempts["build"][0].outcome == "failed"  # orphan repair
    assert all(t.status == "done" for t in state.tasks.values())
