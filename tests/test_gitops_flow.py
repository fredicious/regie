import subprocess

import pytest

from regie import gitops
from regie.gitops import (
    GitError,
    create_run_worktree,
    delete_branch,
    fetch_base_sha,
    head_sha,
    push_branch,
    remove_run_worktree,
)


def test_worktree_lifecycle_off_pinned_base(fixture_repo, tmp_path):
    base = head_sha(fixture_repo)
    wt = create_run_worktree(fixture_repo, "regie/r1", base, tmp_path / "wt")
    assert (wt / "src" / "calc.py").exists()
    assert head_sha(wt) == base
    remove_run_worktree(fixture_repo, wt)
    assert not wt.exists()
    remove_run_worktree(fixture_repo, wt)  # idempotent


def test_fetch_base_and_push(fixture_repo, remote_repo, tmp_path):
    base = fetch_base_sha(fixture_repo, "main")
    assert base == head_sha(fixture_repo)
    wt = create_run_worktree(fixture_repo, "regie/r2", base, tmp_path / "wt2")
    (wt / "new.txt").write_text("x")
    from regie.gitops import commit_all
    commit_all(wt, "feat(T1): x")
    push_branch(wt, "regie/r2")
    out = subprocess.run(["git", "-C", str(remote_repo), "branch"],
                         capture_output=True, text=True, check=False).stdout
    assert "regie/r2" in out


def test_delete_branch_refuses_non_regie(fixture_repo):
    with pytest.raises(GitError):
        delete_branch(fixture_repo, "main")


def test_remove_run_worktree_surfaces_genuine_failure(fixture_repo, tmp_path, monkeypatch):
    base = head_sha(fixture_repo)
    wt = create_run_worktree(fixture_repo, "regie/r3", base, tmp_path / "wt3")
    real_git = gitops.git

    def flaky_git(repo, *args):
        if args[:2] == ("worktree", "remove"):
            raise GitError("simulated: locked or permission denied")
        return real_git(repo, *args)

    monkeypatch.setattr(gitops, "git", flaky_git)
    assert wt.exists()
    with pytest.raises(GitError):
        remove_run_worktree(fixture_repo, wt)
