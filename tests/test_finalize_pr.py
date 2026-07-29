import subprocess

from regie.gitops import commit_all, create_run_worktree, fetch_base_sha
from regie.models import RunState
from regie.pipeline import finalize_stage
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
