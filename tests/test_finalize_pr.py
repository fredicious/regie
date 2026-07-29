import json
import os
import stat
import subprocess

import pytest

from regie import pipeline
from regie.agents.base import AgentResult
from regie.config import load_config
from regie.gitops import commit_all, create_run_worktree, fetch_base_sha, git
from regie.models import RunState
from regie.pipeline import finalize_stage, pr_stage
from regie.rundir import RunDir


def _wt_run(regie_home, fixture_repo, remote_repo, tmp_path, commands_extra=""):
    (fixture_repo / "regie.toml").write_text(
        'test_globs = ["tests/**"]\nbinding_strength = ["fake:m1"]\n'
        'eval_trigger_globs = ["src/**"]\n'
        f'[commands]\ntest = "true"\nlint = "true"\n{commands_extra}')
    base = fetch_base_sha(fixture_repo, "main")
    wt = create_run_worktree(fixture_repo, "regie/rf", base, tmp_path / "wtf")
    (wt / "src" / "x.py").write_text("X = 1\n")
    commit_all(wt, "feat(T1): implement")
    rd = RunDir.create(regie_home, "rf")
    run = RunState(id="rf", target_repo=str(fixture_repo), branch="regie/rf",
                   stage="finalize", base_sha=base, worktree_path=str(wt))
    return rd, run, wt


def test_finalize_green_advances_to_pr(regie_home, fixture_repo, remote_repo,
                                       tmp_path, fake_profiles):
    from regie.config import load_config
    rd, run, wt = _wt_run(regie_home, fixture_repo, remote_repo, tmp_path)
    finalize_stage(rd, run, load_config(fixture_repo, fake_profiles), wt)
    assert run.stage == "pr"


def test_finalize_runs_eval_gate_when_triggered(regie_home, fixture_repo,
                                                remote_repo, tmp_path, fake_profiles):
    from regie.config import load_config
    rd, run, wt = _wt_run(regie_home, fixture_repo, remote_repo, tmp_path,
                          commands_extra='eval = "false"')
    finalize_stage(rd, run, load_config(fixture_repo, fake_profiles), wt)
    assert run.stage == "halted" and "eval" in run.halt_reason


def test_finalize_halts_on_rebase_conflict(regie_home, fixture_repo, remote_repo,
                                           tmp_path, fake_profiles):
    from regie.config import load_config
    rd, run, wt = _wt_run(regie_home, fixture_repo, remote_repo, tmp_path)
    # move origin/main with a conflicting change to the same file
    (fixture_repo / "src" / "x.py").write_text("X = 99\n")
    commit_all(fixture_repo, "conflict on main")
    subprocess.run(["git", "-C", str(fixture_repo), "push", "-q", "origin", "main"], check=True)
    finalize_stage(rd, run, load_config(fixture_repo, fake_profiles), wt)
    assert run.stage == "halted" and "rebase" in run.halt_reason.lower()


# ---------------------------------------------------------------------------
# pr_stage
# ---------------------------------------------------------------------------


def _mk_task_commits(wt):
    (wt / "tests" / "test_a.py").parent.mkdir(exist_ok=True)
    (wt / "tests" / "test_a.py").write_text("def test_a(): assert False\n")
    commit_all(wt, "test(T1): red tests")
    (wt / "src" / "a.py").write_text("A = 1\n")
    commit_all(wt, "feat(T1): implement")
    (wt / "tests" / "test_b.py").write_text("def test_b(): assert False\n")
    commit_all(wt, "test(T2): red tests")
    (wt / "src" / "b.py").write_text("B = 2\n")
    commit_all(wt, "feat(T2): implement")


def _pr_wt_run(regie_home, fixture_repo, tmp_path):
    (fixture_repo / "regie.toml").write_text(
        'test_globs = ["tests/**"]\nbinding_strength = ["fake:m1"]\n'
        '[commands]\ntest = "true"\nlint = "true"\n')
    base = fetch_base_sha(fixture_repo, "main")
    wt = create_run_worktree(fixture_repo, "regie/rp", base, tmp_path / "wtp")
    _mk_task_commits(wt)
    rd = RunDir.create(regie_home, "rp")
    (rd.path / "spec").mkdir()
    (rd.path / "spec" / "spec.md").write_text("# My Feature\n\nSome details.\n")
    run = RunState(id="rp", target_repo=str(fixture_repo), branch="regie/rp",
                   stage="pr", base_sha=base, base_branch="main", worktree_path=str(wt))
    return rd, run, wt


def _agent_queue(monkeypatch, worktree, specs):
    """Drive pr_stage's sequential scribe/debugger/reviewer dispatches with
    canned outcomes. Monkeypatches pipeline.run_agent directly rather than
    routing through the FakeAdapter subprocess: that adapter tracks its own
    queue position via a scratch file inside the worktree, which collides
    with pr_stage's git-cleanliness invariants (rebuild_history refuses a
    dirty tree, and discarding scratch between scribe and the squash would
    wipe not-yet-consumed queue entries). Each spec may declare `writes` --
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


def _stub_gh(tmp_path, monkeypatch, check_sequence):
    """gh stub: `pr create` returns a fixed URL; `pr checks --json ...` cycles
    through check_sequence (advancing a counter file per call, repeating the
    last entry once exhausted); a plain `pr checks` (no --json, used by
    ci_failures) always returns a fixed failure-detail string without
    consuming the sequence."""
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir(parents=True, exist_ok=True)
    counter = tmp_path / "gh-counter"
    counter.write_text("0")
    seq_file = tmp_path / "gh-seq.txt"
    seq_file.write_text("\n".join(check_sequence) + "\n")
    gh.write_text(f"""#!/bin/sh
if [ "$1 $2" = "pr create" ]; then echo "https://github.com/x/y/pull/1"; exit 0; fi
if [ "$1 $2" = "pr checks" ]; then
  case "$*" in
    *--json*)
      n=$(cat {counter})
      line=$(sed -n "$((n+1))p" {seq_file})
      if [ -z "$line" ]; then line=$(tail -n1 {seq_file}); fi
      echo $((n+1)) > {counter}
      echo "$line"
      ;;
    *)
      echo "CI failure details for debugging"
      ;;
  esac
  exit 0
fi
echo "unsupported: $*" 1>&2
exit 1
""")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{gh.parent}:{os.environ['PATH']}")


SCRIBE_OK = {"result": {"outcome": "done", "structured": {
    "commit_messages": ["feat(t1): task one", "feat(t2): task two"],
    "pr_title": "Scribe title", "pr_body": "Scribe body text"}}}
SCRIBE_BAD = {"result": {"outcome": "error"}}
DEBUG_FIX_OK = {"result": {"outcome": "done"}, "writes": {"src/a.py": "A = 2\n"}}
DEBUG_BLOCKED = {"result": {"outcome": "blocked", "blocked_question": "cannot repro"}}
REVIEW_CLEAN = {"result": {"outcome": "done", "structured": {"findings": []}}}


@pytest.fixture(autouse=True)
def _fast_ci(monkeypatch):
    from regie import pipeline
    monkeypatch.setattr(pipeline, "CI_POLL_SECONDS", 0)
    monkeypatch.setattr(pipeline, "CI_WALL_MINUTES", 0.02)


def test_pr_stage_green_path(regie_home, fixture_repo, remote_repo, tmp_path,
                             fake_profiles, monkeypatch):
    rd, run, wt = _pr_wt_run(regie_home, fixture_repo, tmp_path)
    _agent_queue(monkeypatch, wt, [SCRIBE_OK])
    _stub_gh(tmp_path, monkeypatch, ["SUCCESS"])
    minor = json.dumps([{"severity": "minor", "title": "nit", "detail": "consider renaming"}])
    (rd.task_dir("T1") / "minor-findings.json").write_text(minor)

    cfg = load_config(fixture_repo, fake_profiles)
    pr_stage(rd, run, cfg, wt)

    assert run.stage == "done"
    assert run.pr_url == "https://github.com/x/y/pull/1"
    log = git(wt, "log", "--format=%H", f"{run.base_sha}..HEAD").split()
    assert len(log) == 2
    assert git(wt, "rev-parse", f"refs/regie/backup/{run.id}")
    body = (rd.path / "pr-body.md").read_text()
    assert "Review notes" in body
    changed = set()
    for sha in log:
        changed |= set(git(wt, "diff-tree", "--no-commit-id", "--name-only",
                           "-r", sha).splitlines())
    assert changed <= {"src/a.py", "src/b.py", "tests/test_a.py", "tests/test_b.py"}


def test_pr_stage_scribe_failure_falls_back(regie_home, fixture_repo, remote_repo,
                                            tmp_path, fake_profiles, monkeypatch):
    rd, run, wt = _pr_wt_run(regie_home, fixture_repo, tmp_path)
    _agent_queue(monkeypatch, wt, [SCRIBE_BAD])
    _stub_gh(tmp_path, monkeypatch, ["SUCCESS"])

    cfg = load_config(fixture_repo, fake_profiles)
    pr_stage(rd, run, cfg, wt)

    assert run.stage == "done"
    subjects = git(wt, "log", "--reverse", "--format=%s", f"{run.base_sha}..HEAD").splitlines()
    assert subjects == ["feat(T1): implement", "feat(T2): implement"]


def test_pr_stage_red_then_green_one_debugger_round(regie_home, fixture_repo, remote_repo,
                                                    tmp_path, fake_profiles, monkeypatch):
    rd, run, wt = _pr_wt_run(regie_home, fixture_repo, tmp_path)
    _agent_queue(monkeypatch, wt, [SCRIBE_OK, DEBUG_FIX_OK, REVIEW_CLEAN])
    _stub_gh(tmp_path, monkeypatch, ["FAILURE", "SUCCESS"])

    cfg = load_config(fixture_repo, fake_profiles)
    pr_stage(rd, run, cfg, wt)

    assert run.stage == "done"
    assert (rd.path / "tasks" / "DEBUG-1").is_dir()
    subjects = git(wt, "log", "--format=%s", f"{run.base_sha}..HEAD")
    assert "fix(ci): debugger round 1" in subjects
    # The remote tip must reflect the fix commit -- i.e. a second push
    # happened after the debugger round, not just the initial squash push.
    local_head = git(wt, "rev-parse", "HEAD").strip()
    remote_tip = subprocess.run(["git", "-C", str(remote_repo), "rev-parse", "regie/rp"],
                                capture_output=True, text=True, check=True).stdout.strip()
    assert remote_tip == local_head


def test_pr_stage_red_persists_halts_after_max_rounds(regie_home, fixture_repo, remote_repo,
                                                       tmp_path, fake_profiles, monkeypatch):
    rd, run, wt = _pr_wt_run(regie_home, fixture_repo, tmp_path)
    _agent_queue(monkeypatch, wt, [SCRIBE_OK, DEBUG_BLOCKED, DEBUG_BLOCKED])
    _stub_gh(tmp_path, monkeypatch, ["FAILURE", "FAILURE", "FAILURE"])

    cfg = load_config(fixture_repo, fake_profiles)
    pr_stage(rd, run, cfg, wt)

    assert run.stage == "halted"
    assert "CI red after 2 debugger rounds" in run.halt_reason


def test_pr_stage_no_groups_halts(regie_home, fixture_repo, remote_repo, tmp_path,
                                  fake_profiles):
    rd, run, wt = _pr_wt_run(regie_home, fixture_repo, tmp_path)
    # squash away the two groups first so base..HEAD is empty
    git(wt, "reset", "--hard", run.base_sha)
    cfg = load_config(fixture_repo, fake_profiles)
    pr_stage(rd, run, cfg, wt)
    assert run.stage == "halted" and "nothing to submit" in run.halt_reason
