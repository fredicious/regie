import subprocess
from pathlib import Path

import pytest


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
