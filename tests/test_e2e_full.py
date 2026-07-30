"""Plan B exit criterion: the WHOLE pipeline — brief in, planner, approve
checkpoint (or --autonomous), tasks, finalize, PR with CI green — as one flow,
driven entirely by the fake adapter and a stubbed gh. No --tasks-file."""
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
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir(parents=True, exist_ok=True)
    gh.write_text("#!/bin/sh\n"
                  'if [ "$1 $2" = "pr create" ]; then echo "https://github.com/x/y/pull/9"; exit 0; fi\n'
                  'if [ "$1 $2" = "pr checks" ]; then echo "SUCCESS"; exit 0; fi\n'
                  'exit 1\n')
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{gh.parent}:{os.environ['PATH']}")


PLAN = {"spec_markdown": "# Spec: two functions\n\nDivide and power.",
        "tasks": [
            {"id": "T1", "title": "divide", "profile": "builder",
             "criteria": ["Given 6 and 3, When divide, Then 2"],
             "planned_tests": ["test_div"], "depends_on": []},
            {"id": "T2", "title": "power", "profile": "builder",
             "criteria": ["Given 2 and 3, When power, Then 8"],
             "planned_tests": ["test_pow"], "depends_on": ["T1"]},
        ]}

DIVIDE_STUB = "def divide(a, b):\n    raise NotImplementedError\n"
DIVIDE_IMPL = "def divide(a, b):\n    return a // b\n"
POWER_STUB = "def power(a, b):\n    raise NotImplementedError\n"
POWER_IMPL = "def power(a, b):\n    return a ** b\n"
ADD = "def add(a, b):\n    return a + b\n"
TEST_DIV = ("from src.calc import divide\n\n"
            "def test_div():\n    assert divide(6, 3) == 2\n")
TEST_POW = ("from src.calc import power\n\n"
            "def test_pow():\n    assert power(2, 3) == 8\n")

QUEUE = [
    # 0: planner
    {"result": {"outcome": "done", "structured": PLAN}},
    # 1: T1 test stage (red + stub)
    {"result": {"outcome": "done"}, "writes": {
        "tests/test_div.py": TEST_DIV,
        "src/calc.py": ADD + "\n" + DIVIDE_STUB}},
    # 2: T1 build stage
    {"result": {"outcome": "done"}, "writes": {
        "src/calc.py": ADD + "\n" + DIVIDE_IMPL}},
    # 3: T1 review — one MINOR finding (must reach the PR body, never loop)
    {"result": {"outcome": "done", "structured": {"findings": [
        {"severity": "minor", "title": "naming nit", "detail": "cosmetic"}]}}},
    # 4: T2 test stage
    {"result": {"outcome": "done"}, "writes": {
        "tests/test_pow.py": TEST_POW,
        "src/calc.py": ADD + "\n" + DIVIDE_IMPL + "\n" + POWER_STUB}},
    # 5: T2 build stage
    {"result": {"outcome": "done"}, "writes": {
        "src/calc.py": ADD + "\n" + DIVIDE_IMPL + "\n" + POWER_IMPL}},
    # 6: T2 review — clean
    {"result": {"outcome": "done", "structured": {"findings": []}}},
    # NOTE: no scribe entry. finalize_stage's discard (`git checkout -- .`)
    # restores the consumed (tracked) queue entries, so the pr-stage scribe
    # dispatch re-reads entry 0 (the planner result), which lacks
    # commit_messages — routing pr_stage onto its deterministic fallback.
    # That is fake-adapter test mechanics, not production behavior; the
    # scribe-success path is unit-covered in tests/test_finalize_pr.py. This
    # e2e therefore asserts the fallback messages (group defaults).
]


def _setup(fixture_repo, tmp_path):
    (fixture_repo / "regie.toml").write_text(
        'test_globs = ["tests/**"]\nbinding_strength = ["fake:m1"]\n'
        '[commands]\ntest = "python -m pytest tests -q"\nlint = "true"\n')
    qdir = fixture_repo / ".fake_agent_queue"
    qdir.mkdir(exist_ok=True)
    for i, entry in enumerate(QUEUE):
        (qdir / f"{i}.json").write_text(json.dumps(entry))
    commit_all(fixture_repo, "chore: fake agent queue")
    subprocess.run(["git", "-C", str(fixture_repo), "push", "-q", "origin", "main"],
                   check=True)
    brief = tmp_path / "two-functions.md"
    brief.write_text("# Two functions\n\nAdd divide and power to calc.")
    return brief


def _assert_done_state(regie_home, run_id):
    rundir = RunDir.open(regie_home, run_id)
    state = rundir.read_state()
    assert state.stage == "done"
    assert state.pr_url == "https://github.com/x/y/pull/9"
    assert all(t.status == "done" for t in state.tasks.values())
    # planner artifacts
    assert (rundir.path / "spec" / "spec.md").read_text().startswith("# Spec")
    # minors reached the PR body
    body = (rundir.path / "pr-body.md").read_text()
    assert "Review notes" in body and "naming nit" in body
    # squashed history: exactly one commit per task, scribe messages used
    log = subprocess.run(
        ["git", "-C", state.worktree_path, "log", "--format=%s",
         f"{state.base_sha}..HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip().splitlines()
    # Scribe fallback messages (see QUEUE note): one commit per task, group
    # default subjects, newest first.
    assert log == ["feat(T2): implement", "feat(T1): implement"]
    # backup ref from the rewrite
    subprocess.run(["git", "-C", state.worktree_path, "rev-parse",
                    f"refs/regie/backup/{run_id}"], check=True,
                   capture_output=True)
    # No UNGATED scaffolding in any commit. Consumed-queue renames from
    # entries 0-5 land inside gated task commits (tracked test fixtures being
    # renamed — unavoidable committed-queue noise, same convention as
    # tests/test_e2e.py). The invariants that matter: the FINAL dispatch's
    # leftover (entry 6's rename, ungated at finalize time) was discarded,
    # and no adapter schema scratch was ever committed.
    files = subprocess.run(
        ["git", "-C", state.worktree_path, "log", "--name-only",
         f"{state.base_sha}..HEAD"], capture_output=True, text=True, check=True
    ).stdout
    assert "6.json.done" not in files and ".regie_schema" not in files
    return state


def test_full_pipeline_with_approve_checkpoint(regie_home, fixture_repo,
                                               remote_repo, fake_profiles,
                                               tmp_path, monkeypatch):
    from regie import pipeline
    monkeypatch.setattr(pipeline, "CI_POLL_SECONDS", 0)
    _stub_gh_green(tmp_path, monkeypatch)
    brief = _setup(fixture_repo, tmp_path)

    result = runner.invoke(app, ["run", str(brief), "--repo", str(fixture_repo),
                                 "--profiles", str(fake_profiles)])
    assert result.exit_code == 0, result.output
    assert "regie approve" in result.output
    run_id = max((regie_home / "runs").iterdir()).name
    assert RunDir.open(regie_home, run_id).read_state().stage == "approve"

    result = runner.invoke(app, ["approve", run_id])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["resume", run_id, "--repo", str(fixture_repo),
                                 "--profiles", str(fake_profiles)])
    assert result.exit_code == 0, result.output
    state = _assert_done_state(regie_home, run_id)
    # remote branch exists with the squashed commits
    remote_heads = subprocess.run(
        ["git", "-C", str(remote_repo), "branch", "--format=%(refname:short)"],
        capture_output=True, text=True, check=True).stdout
    assert state.branch in remote_heads


def test_full_pipeline_autonomous(regie_home, fixture_repo, remote_repo,
                                  fake_profiles, tmp_path, monkeypatch):
    from regie import pipeline
    monkeypatch.setattr(pipeline, "CI_POLL_SECONDS", 0)
    _stub_gh_green(tmp_path, monkeypatch)
    brief = _setup(fixture_repo, tmp_path)

    result = runner.invoke(app, ["run", str(brief), "--repo", str(fixture_repo),
                                 "--profiles", str(fake_profiles), "--autonomous"])
    assert result.exit_code == 0, result.output
    run_id = max((regie_home / "runs").iterdir()).name
    _assert_done_state(regie_home, run_id)
