import subprocess
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def disable_desktop_notifications(monkeypatch) -> None:
    """Tests must never produce real operating-system notifications."""
    monkeypatch.setenv("REGIE_NOTIFICATIONS", "0")


@pytest.fixture
def fake_profiles(tmp_path) -> Path:
    d = tmp_path / "profiles"
    d.mkdir()
    for name in ("planner", "test-writer", "builder", "reviewer"):
        (d / f"{name}.yaml").write_text(
            # Two rungs so ladder tests keep a genuine escalation step now
            # that the global binding_strength order is gone (the per-profile
            # list is the only source of rungs) — the shape AC11/AC12/AC14
            # already assume.
            "bindings:\n"
            "  - { cli: fake, model: m1 }\n"
            "  - { cli: fake, model: m2 }\n"
            "hard: { cli: fake, model: m2 }\n"
            "budgets: { turns: 5, wall_minutes: 1, stall_minutes: 1 }\n")
        (d / f"{name}.md").write_text(f"You are {name}.")
    return d


@pytest.fixture
def regie_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "regie-home"
    home.mkdir()
    monkeypatch.setenv("REGIE_HOME", str(home))
    return home


@pytest.fixture
def fixture_repo(tmp_path) -> Path:
    """A tiny git repo with one source file and one test file."""
    repo = tmp_path / "fixture-repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "tests" / "test_calc.py").write_text(
        "from src.calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    for cmd in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
    ):
        subprocess.run(cmd, cwd=repo, check=True)
    return repo


@pytest.fixture
def remote_repo(fixture_repo, tmp_path):
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "--bare", "-q", str(fixture_repo), str(bare)], check=True)
    subprocess.run(["git", "-C", str(fixture_repo), "remote", "add", "origin", str(bare)], check=True)
    # fixture_repo's default branch must be named main for fetch_base_sha tests
    subprocess.run(["git", "-C", str(fixture_repo), "branch", "-M", "main"], check=True)
    subprocess.run(["git", "-C", str(fixture_repo), "push", "-q", "-u", "origin", "main"], check=True)
    return bare
