import json
import os
import stat
import subprocess
from pathlib import Path

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
        'test_globs = ["tests/**"]\n'
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


def test_finalize_can_complete_locally_without_submitting_pr(
        regie_home, fixture_repo, remote_repo, tmp_path, fake_profiles):
    rd, run, wt = _wt_run(regie_home, fixture_repo, remote_repo, tmp_path)
    (fixture_repo / "regie.toml").write_text(
        'test_globs = ["tests/**"]\n'
        '[workflow]\nreflection = false\nsubmit_pr = false\n'
        '[commands]\ntest = "true"\nlint = "true"\n')

    finalize_stage(rd, run, load_config(fixture_repo, fake_profiles), wt)

    assert run.stage == "done"
    assert run.pr_url == ""
    assert run.pushed is False


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
        'test_globs = ["tests/**"]\n'
        '[workflow]\nreflection = false\n'
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


DEBUG_FIX_OK = {"result": {"outcome": "done"}, "writes": {"src/a.py": "A = 2\n"}}
DEBUG_BLOCKED = {"result": {"outcome": "blocked", "blocked_question": "cannot repro"}}
DEBUG_QUOTA = {"result": {"outcome": "quota"}}
REVIEW_CLEAN = {"result": {"outcome": "done", "structured": {"findings": []}}}


@pytest.fixture(autouse=True)
def _fast_ci(monkeypatch):
    from regie import pipeline
    monkeypatch.setattr(pipeline, "CI_POLL_SECONDS", 0)
    monkeypatch.setattr(pipeline, "CI_WALL_MINUTES", 0.02)


def test_pr_stage_green_path(regie_home, fixture_repo, remote_repo, tmp_path,
                             fake_profiles, monkeypatch):
    rd, run, wt = _pr_wt_run(regie_home, fixture_repo, tmp_path)
    _stub_gh(tmp_path, monkeypatch, ["SUCCESS"])
    minor = json.dumps([{"severity": "minor", "title": "nit", "detail": "consider renaming"}])
    (rd.task_dir("T1") / "minor-findings.json").write_text(minor)

    cfg = load_config(fixture_repo, fake_profiles)
    pr_stage(rd, run, cfg, wt)

    assert run.stage == "done"
    assert run.pr_url == "https://github.com/x/y/pull/1"
    log = git(wt, "log", "--format=%H", f"{run.base_sha}..HEAD").split()
    assert len(log) == 3  # two task commits + the spec commit
    assert git(wt, "rev-parse", f"refs/regie/backup/{run.id}")
    body = (rd.path / "pr-body.md").read_text()
    assert "Review notes" in body
    # Every squashed commit carries the Régie co-author trailer, while the
    # author identity stays the developer's (or the fallback in this
    # config-less fixture) — never "regie" as author.
    for sha in log:
        full = git(wt, "log", "-1", "--format=%B", sha)
        assert "Co-authored-by: Régie" in full
    changed = set()
    for sha in log:
        changed |= set(git(wt, "diff-tree", "--no-commit-id", "--name-only",
                           "-r", sha).splitlines())
    assert changed <= {"src/a.py", "src/b.py", "tests/test_a.py",
                       "tests/test_b.py", "specs/rp.md"}


def test_pr_stage_uses_deterministic_pr_copy(regie_home, fixture_repo, remote_repo,
                                             tmp_path, fake_profiles, monkeypatch):
    rd, run, wt = _pr_wt_run(regie_home, fixture_repo, tmp_path)
    _stub_gh(tmp_path, monkeypatch, ["SUCCESS"])

    cfg = load_config(fixture_repo, fake_profiles)
    pr_stage(rd, run, cfg, wt)

    assert run.stage == "done"
    subjects = git(wt, "log", "--reverse", "--format=%s", f"{run.base_sha}..HEAD").splitlines()
    assert subjects == ["feat(T1): implement", "feat(T2): implement", "docs(spec): rp"]


def test_pr_stage_red_then_green_one_debugger_round(regie_home, fixture_repo, remote_repo,
                                                    tmp_path, fake_profiles, monkeypatch):
    rd, run, wt = _pr_wt_run(regie_home, fixture_repo, tmp_path)
    _agent_queue(monkeypatch, wt, [DEBUG_FIX_OK, REVIEW_CLEAN])
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
    _agent_queue(monkeypatch, wt, [DEBUG_BLOCKED, DEBUG_BLOCKED])
    _stub_gh(tmp_path, monkeypatch, ["FAILURE", "FAILURE", "FAILURE"])

    cfg = load_config(fixture_repo, fake_profiles)
    pr_stage(rd, run, cfg, wt)

    assert run.stage == "halted"
    assert "CI red after 2 debugger rounds" in run.halt_reason


def test_debugger_round_dispatch_failure_discards_scratch(regie_home, fixture_repo,
                                                           remote_repo, tmp_path,
                                                           fake_profiles, monkeypatch):
    rd, run, wt = _pr_wt_run(regie_home, fixture_repo, tmp_path)
    cfg = load_config(fixture_repo, fake_profiles)
    _agent_queue(monkeypatch, wt, [
        {"result": {"outcome": "error"}, "writes": {"src/scratch.py": "x = 1\n"}}])

    ok = pipeline._debugger_round(rd, run, cfg, wt, 1, "failure detail")

    assert ok is False
    assert git(wt, "status", "--porcelain").strip() == ""


def test_debugger_round_review_rejection_rolls_back_fix_commit(regie_home, fixture_repo,
                                                                remote_repo, tmp_path,
                                                                fake_profiles, monkeypatch):
    rd, run, wt = _pr_wt_run(regie_home, fixture_repo, tmp_path)
    cfg = load_config(fixture_repo, fake_profiles)
    pre_round_sha = git(wt, "rev-parse", "HEAD").strip()
    _agent_queue(monkeypatch, wt, [
        {"result": {"outcome": "done"}, "writes": {"src/a.py": "A = 2\n"}},
        {"result": {"outcome": "done", "structured": {"findings": [
            {"severity": "blocker", "title": "bad fix", "detail": "still broken"}]}}}])

    ok = pipeline._debugger_round(rd, run, cfg, wt, 1, "failure detail")

    assert ok is False
    assert git(wt, "rev-parse", "HEAD").strip() == pre_round_sha
    subjects = git(wt, "log", "--format=%s", f"{run.base_sha}..HEAD")
    assert "fix(ci): debugger round 1" not in subjects
    assert git(wt, "status", "--porcelain").strip() == ""


def test_quota_across_debugger_providers_halts_immediately(
        regie_home, fixture_repo, remote_repo, tmp_path, fake_profiles, monkeypatch):
    """Quota failover may try each configured debugger provider once, but
    exhausting the ladder must halt before another CI/debug round or review."""
    rd, run, wt = _pr_wt_run(regie_home, fixture_repo, tmp_path)
    _agent_queue(monkeypatch, wt, [DEBUG_QUOTA, DEBUG_QUOTA])
    _stub_gh(tmp_path, monkeypatch, ["FAILURE"])

    cfg = load_config(fixture_repo, fake_profiles)
    pr_stage(rd, run, cfg, wt)

    assert run.stage == "halted"
    assert run.halt_reason == "quota exhausted across debugger providers in round 1"


def test_debugger_quota_fails_over_to_next_binding(
        regie_home, fixture_repo, remote_repo, tmp_path, fake_profiles, monkeypatch):
    rd, run, wt = _pr_wt_run(regie_home, fixture_repo, tmp_path)
    _agent_queue(monkeypatch, wt, [
        DEBUG_QUOTA,
        {"result": {"outcome": "done"}, "writes": {"src/a.py": "A = 2\n"}},
        {"result": {"outcome": "done", "structured": {"findings": []}}},
    ])
    cfg = load_config(fixture_repo, fake_profiles)

    ok = pipeline._debugger_round(rd, run, cfg, wt, 1, "failure detail")

    assert ok is True
    assert git(wt, "show", "HEAD:src/a.py").strip() == "A = 2"


def test_pr_stage_no_groups_halts(regie_home, fixture_repo, remote_repo, tmp_path,
                                  fake_profiles):
    rd, run, wt = _pr_wt_run(regie_home, fixture_repo, tmp_path)
    # squash away the two groups first so base..HEAD is empty
    git(wt, "reset", "--hard", run.base_sha)
    cfg = load_config(fixture_repo, fake_profiles)
    pr_stage(rd, run, cfg, wt)
    assert run.stage == "halted" and "nothing to submit" in run.halt_reason


# ---------------------------------------------------------------------------
# pr_stage re-entrancy after a halt at/after the first push (resume stranding)
# ---------------------------------------------------------------------------


def test_pr_stage_reentry_when_pushed_skips_rebuild(regie_home, fixture_repo, remote_repo,
                                                     tmp_path, fake_profiles, monkeypatch):
    rd, run, wt = _pr_wt_run(regie_home, fixture_repo, tmp_path)
    _stub_gh(tmp_path, monkeypatch, ["SUCCESS"])
    cfg = load_config(fixture_repo, fake_profiles)

    # First pass: a normal push all the way to "done" -- this is what leaves
    # run.pushed=True and run.pr_url set, matching the state a halt at/after
    # the first push (CI-red debugger round, CI timeout, ...) would leave
    # behind for a later `regie resume` to pick up.
    pr_stage(rd, run, cfg, wt)
    assert run.stage == "done"
    assert run.pushed is True
    commits_before = git(wt, "log", "--format=%H", f"{run.base_sha}..HEAD").splitlines()

    # Re-entry, as cli.resume performs it after resetting stage back to "pr"
    # for a halted-while-pushed run: must not re-squash the already-pushed
    # history a second time.
    run.stage = "pr"
    rebuild_calls = []
    monkeypatch.setattr(pipeline, "rebuild_history",
                        lambda *a, **kw: rebuild_calls.append(a))
    pr_stage(rd, run, cfg, wt)

    assert run.stage == "done"
    assert rebuild_calls == []
    commits_after = git(wt, "log", "--format=%H", f"{run.base_sha}..HEAD").splitlines()
    assert commits_after == commits_before


def test_resume_after_halt_while_pushed_reenters_pr_stage_not_tasks(
        regie_home, fixture_repo, remote_repo, tmp_path, fake_profiles, monkeypatch):
    from typer.testing import CliRunner

    from regie.cli import app

    rd, run, _wt = _pr_wt_run(regie_home, fixture_repo, tmp_path)
    run.pushed = True
    run.pr_url = "https://github.com/x/y/pull/1"
    run.stage = "halted"
    run.halt_reason = "CI timeout"
    rd.write_state(run)

    rebuild_calls = []
    monkeypatch.setattr(pipeline, "rebuild_history",
                        lambda *a, **kw: rebuild_calls.append(a))
    _stub_gh(tmp_path, monkeypatch, ["SUCCESS"])

    result = CliRunner().invoke(app, ["resume", "rp", "--repo", str(fixture_repo),
                                      "--profiles", str(fake_profiles)])

    assert result.exit_code == 0, result.output
    final = RunDir.open(regie_home, "rp").read_state()
    assert final.stage == "done"
    assert rebuild_calls == []


def test_fallback_title_prefers_content_over_heading():
    from regie.pipeline import _fallback_title
    spec = "## Goal\nAdd a slugify function to text_utils.\n\n## Criteria\n..."
    assert _fallback_title(spec, "rid") == "Add a slugify function to text_utils."
    assert _fallback_title("## Only Heading\n", "rid") == "Only Heading"
    assert _fallback_title("", "rid") == "rid"


def test_pr_stage_commits_spec_into_repo(regie_home, fixture_repo, remote_repo,
                                         tmp_path, fake_profiles, monkeypatch):
    rd, run, wt = _pr_wt_run(regie_home, fixture_repo, tmp_path)
    (rd.path / "spec").mkdir(exist_ok=True)
    (rd.path / "spec" / "spec.md").write_text("# Spec: the feature\ndetails")
    _stub_gh(tmp_path, monkeypatch, ["SUCCESS"])
    cfg = load_config(fixture_repo, fake_profiles)
    pr_stage(rd, run, cfg, wt)
    assert run.stage == "done"
    spec_file = Path(wt) / "specs" / f"{run.id}.md"
    assert spec_file.exists() and spec_file.read_text().startswith("# Spec")
    subjects = git(wt, "log", "--format=%s", f"{run.base_sha}..HEAD").splitlines()
    assert any(s.startswith("docs(spec):") for s in subjects)
    # the spec file is part of the pushed history
    files = git(wt, "log", "--name-only", f"{run.base_sha}..HEAD")
    assert f"specs/{run.id}.md" in files


def test_finalize_idempotent_when_already_on_base(regie_home, fixture_repo,
                                                  remote_repo, tmp_path, fake_profiles):
    """A worktree already rebased onto current origin/main (e.g. a human
    resolved a conflict) must proceed to pr, not re-rebase and re-conflict."""
    rd, run, wt = _wt_run(regie_home, fixture_repo, remote_repo, tmp_path)
    # simulate a manual rebase: fast-forward the worktree is already off main's
    # tip, so HEAD already has origin/main as ancestor
    finalize_stage(rd, run, load_config(fixture_repo, fake_profiles), wt)
    assert run.stage == "pr"
    # base_sha refreshed to current origin/main
    assert run.base_sha == git(wt, "rev-parse", "origin/main").strip()


def test_finalize_conflict_names_files_and_drift(regie_home, fixture_repo,
                                                 remote_repo, tmp_path, fake_profiles):
    rd, run, wt = _wt_run(regie_home, fixture_repo, remote_repo, tmp_path)
    # advance origin/main with a conflicting change to the same file
    (fixture_repo / "src" / "x.py").write_text("X = 99\n")
    commit_all(fixture_repo, "conflict on main")
    subprocess.run(["git", "-C", str(fixture_repo), "push", "-q", "origin", "main"],
                   check=True)
    finalize_stage(rd, run, load_config(fixture_repo, fake_profiles), wt)
    assert run.stage == "halted"
    assert "src/x.py" in run.halt_reason        # names the conflicting file
    assert "1 new commit" in run.halt_reason     # reports drift
    # the abort left a clean tree (no rebase in progress wedging resume)
    assert not _rebase_in_progress(wt)


def _rebase_in_progress(wt):
    from pathlib import Path as _P
    gitdir = git(wt, "rev-parse", "--git-dir").strip()
    base = _P(wt) / gitdir if not gitdir.startswith("/") else _P(gitdir)
    return (base / "rebase-merge").exists() or (base / "rebase-apply").exists()
