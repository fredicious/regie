import json

from regie.research import research_repository
from regie.rundir import RunDir


def test_repository_research_is_bounded_and_persisted(regie_home, fixture_repo):
    rundir = RunDir.create(regie_home, "r1")
    facts = research_repository(rundir, fixture_repo)

    assert "src/calc.py" in facts["files"]
    assert facts["recent_commits"]
    assert (rundir.path / "research.md").exists()
    assert json.loads((rundir.path / "research.json").read_text())["repo"] == str(
        fixture_repo.resolve())
