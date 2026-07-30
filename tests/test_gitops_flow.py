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


import os
import stat

from regie.gitops import (
    ci_status,
    commit_all,
    create_pr,
    git,
    rebuild_history,
    run_commit_groups,
)


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


def test_rebuild_history_squashes_per_task(fixture_repo, tmp_path):
    from regie.gitops import create_run_worktree, head_sha
    base = head_sha(fixture_repo)
    wt = create_run_worktree(fixture_repo, "regie/r3", base, tmp_path / "wt3")
    _mk_task_commits(wt)
    pre_tree = git(wt, "rev-parse", "HEAD^{tree}").strip()
    groups = run_commit_groups(wt, base)
    assert [g[0] for g in groups] == ["feat(T1): implement", "feat(T2): implement"]
    rebuild_history(wt, base, [("feat(t1): task one", groups[0][1]),
                               ("feat(t2): task two", groups[1][1])], "r3")
    assert len(git(wt, "log", "--format=%H", f"{base}..HEAD").split()) == 2
    assert git(wt, "rev-parse", "HEAD^{tree}").strip() == pre_tree
    assert git(wt, "rev-parse", "refs/regie/backup/r3")


def test_rebuild_history_restores_on_tree_mismatch(fixture_repo, tmp_path):
    import pytest

    from regie.gitops import GitError, create_run_worktree, head_sha
    base = head_sha(fixture_repo)
    wt = create_run_worktree(fixture_repo, "regie/r4", base, tmp_path / "wt4")
    _mk_task_commits(wt)
    head_before = head_sha(wt)
    groups = run_commit_groups(wt, base)
    bad = [("feat: incomplete", groups[0][1][:1])]  # drops commits → tree differs
    with pytest.raises(GitError):
        rebuild_history(wt, base, bad, "r4")
    assert head_sha(wt) == head_before  # restored from backup ref


def test_rebuild_history_restores_on_cherry_pick_failure(fixture_repo, tmp_path):
    import pytest

    from regie.gitops import GitError, create_run_worktree, head_sha
    base = head_sha(fixture_repo)
    wt = create_run_worktree(fixture_repo, "regie/r5", base, tmp_path / "wt5")
    _mk_task_commits(wt)
    head_before = head_sha(wt)
    groups = run_commit_groups(wt, base)
    bad = [("feat: bogus", ["0" * 40]), ("feat: rest", groups[1][1])]
    with pytest.raises(GitError):
        rebuild_history(wt, base, bad, "r5")
    assert head_sha(wt) == head_before  # fully restored, not half-rebuilt


def test_rebuild_history_refuses_dirty_worktree(fixture_repo, tmp_path):
    import pytest

    from regie.gitops import GitError, create_run_worktree, head_sha
    base = head_sha(fixture_repo)
    wt = create_run_worktree(fixture_repo, "regie/r6", base, tmp_path / "wt6")
    _mk_task_commits(wt)
    groups = run_commit_groups(wt, base)
    head_before = head_sha(wt)
    (wt / "src" / "calc.py").write_text("dirty\n")
    with pytest.raises(GitError):
        rebuild_history(wt, base, [("feat(t1): task one", groups[0][1])], "r6")
    assert head_sha(wt) == head_before
    assert (wt / "src" / "calc.py").read_text() == "dirty\n"  # untouched


def _stub_gh(tmp_path, monkeypatch, checks_json):
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir(parents=True, exist_ok=True)
    gh.write_text("#!/bin/sh\n"
                  'if [ "$1 $2" = "pr create" ]; then echo "https://github.com/x/y/pull/1"; exit 0; fi\n'
                  f"echo '{checks_json}'\n")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{gh.parent}:{os.environ['PATH']}")


def test_create_pr_and_ci_status(fixture_repo, tmp_path, monkeypatch):
    _stub_gh(tmp_path, monkeypatch, "SUCCESS")
    body = tmp_path / "body.md"; body.write_text("PR body")
    url = create_pr(fixture_repo, "main", "feat: x", body)
    assert url == "https://github.com/x/y/pull/1"
    assert ci_status(fixture_repo) == "green"
    _stub_gh(tmp_path, monkeypatch, "FAILURE,SUCCESS")
    assert ci_status(fixture_repo) == "red"


def test_commit_all_uses_configured_identity(fixture_repo):
    from regie.gitops import commit_all, git
    git(fixture_repo, "config", "user.name", "Dev Person")
    git(fixture_repo, "config", "user.email", "dev@example.com")
    (fixture_repo / "id.txt").write_text("x")
    commit_all(fixture_repo, "chore: identity check")
    author = git(fixture_repo, "log", "-1", "--format=%an <%ae>").strip()
    assert author == "Dev Person <dev@example.com>"


def test_commit_all_falls_back_to_regie_identity(fixture_repo, monkeypatch):
    from regie.gitops import commit_all, git
    # Hide global/system config so the repo genuinely has no identity.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    (fixture_repo / "id2.txt").write_text("x")
    commit_all(fixture_repo, "chore: fallback identity check")
    author = git(fixture_repo, "log", "-1", "--format=%an <%ae>").strip()
    assert author == "Régie <regie@noreply.local>"
