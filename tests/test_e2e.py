"""Plan A exit criterion: a two-task run completes through the fake adapter,
and a simulated crash mid-run resumes to completion."""
import gc
import json

from typer.testing import CliRunner

from regie.cli import app
from regie.rundir import RunDir

runner = CliRunner()

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


def _setup(regie_home, fixture_repo, fake_profiles, tmp_path):
    (fixture_repo / "regie.toml").write_text(
        'test_globs = ["tests/**"]\nbinding_strength = ["fake:m1"]\n'
        '[commands]\ntest = "python -m pytest tests -q"\nlint = "true"\n')
    brief = tmp_path / "feature.md"
    brief.write_text("# two functions")
    (tmp_path / "tasks.json").write_text(json.dumps(TASKS))
    _queue(fixture_repo, QUEUE)
    return brief


def test_full_run_reaches_finalize(regie_home, fixture_repo, fake_profiles, tmp_path):
    brief = _setup(regie_home, fixture_repo, fake_profiles, tmp_path)
    result = runner.invoke(app, ["run", str(brief), "--repo", str(fixture_repo),
                                 "--profiles", str(fake_profiles)])
    assert result.exit_code == 0, result.output
    run_id = next(l for l in result.output.splitlines() if "→" in l).split()[1]
    state = RunDir.open(regie_home, run_id).read_state()
    assert state.stage == "finalize"
    assert all(t.status == "done" for t in state.tasks.values())


def test_crash_then_resume_completes(regie_home, fixture_repo, fake_profiles,
                                     tmp_path, monkeypatch):
    brief = _setup(regie_home, fixture_repo, fake_profiles, tmp_path)
    # Crash injection: on the SECOND dispatch, do what the real dispatch.run_agent
    # does first -- write the WAL intent -- then crash before an Attempt is ever
    # recorded in state, and before the fake script runs (so an uncommitted edit
    # is left behind, simulating a killed agent's in-flight write). This is the
    # exact scenario the WAL/reconcile machinery exists for: intent logged,
    # no corresponding attempt, worktree dirty.
    from regie import pipeline
    real = pipeline.run_agent
    calls = {"n": 0}
    dirty = fixture_repo / "src" / "dirty.py"

    def crashing(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            rundir, task_id, stage, attempt_no, req = args
            dirty.write_text("dirty\n")  # in-flight edit the crash never cleaned up
            rundir.append_intent({"task": task_id, "stage": stage,
                                  "attempt": attempt_no,
                                  "binding": req.binding.model_dump()})
            raise KeyboardInterrupt  # simulated hard crash after WAL write
        return real(*args, **kwargs)

    monkeypatch.setattr(pipeline, "run_agent", crashing)
    result = runner.invoke(app, ["run", str(brief), "--repo", str(fixture_repo),
                                 "--profiles", str(fake_profiles)])
    assert result.exit_code != 0  # crashed
    assert dirty.exists()  # the in-flight edit landed before the crash

    # In a real crash the OS closes the run.lock fd (and its flock) when the
    # process dies. Here the crash is simulated in-process, and click's
    # captured traceback keeps the crashed frame -- and its RunDir.lock fd --
    # alive via a reference cycle. Drop it and collect to release the lock,
    # matching what process death gives us for free.
    del result
    gc.collect()

    # The crashed dispatch never reached the fake script, so queue entries
    # 1..5 are untouched -- but re-create them from the reconciled position
    # anyway so resume doesn't depend on that timing detail. No extra queue
    # entry is needed: reconcile's synthetic failed attempt satisfies the
    # ladder's retry slot without itself consuming a dispatch, so the next
    # real dispatch still consumes the same (T1 build) entry as before.
    _queue(fixture_repo, QUEUE[1:], start=1)

    monkeypatch.setattr(pipeline, "run_agent", real)
    run_id = max((regie_home / "runs").iterdir()).name
    result = runner.invoke(app, ["resume", run_id, "--repo", str(fixture_repo),
                                 "--profiles", str(fake_profiles)])
    assert result.exit_code == 0, result.output
    assert "reconciled 1 orphaned attempt" in result.output

    state = RunDir.open(regie_home, run_id).read_state()
    assert not dirty.exists()  # reconcile's git clean discarded the crash's edit
    assert state.tasks["T1"].attempts["build"][0].outcome == "failed"  # orphan repair
    assert all(t.status == "done" for t in state.tasks.values())
