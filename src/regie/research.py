from __future__ import annotations

import json
from pathlib import Path

from regie.gitops import GitError, git
from regie.rundir import RunDir

_MARKERS = (
    "pyproject.toml", "package.json", "Cargo.toml", "go.mod", "pom.xml",
    "Makefile", "justfile", "AGENTS.md", "CLAUDE.md", "README.md",
)


def research_repository(rundir: RunDir, repo: Path) -> dict:
    """Create a bounded, deterministic repository-facts artifact for planning."""
    files = []
    for path in sorted(repo.rglob("*")):
        if not path.is_file() or ".git" in path.parts:
            continue
        rel = str(path.relative_to(repo))
        if any(part in {"node_modules", ".venv", "dist", "build"} for part in path.parts):
            continue
        files.append(rel)
        if len(files) >= 600:
            break
    markers = {name: (repo / name).read_text(errors="replace")[:5000]
               for name in _MARKERS if (repo / name).is_file()}
    try:
        history = git(repo, "log", "-12", "--format=%h %s").splitlines()
    except GitError:
        history = []
    facts = {
        "repo": str(repo.resolve()),
        "files": files,
        "markers": markers,
        "recent_commits": history,
        "top_level": sorted({path.split("/", 1)[0] for path in files}),
    }
    path = rundir.path / "research.json"
    path.write_text(json.dumps(facts, indent=2))
    md = ["# Repository research", "", f"Repository: `{repo.resolve()}`", "",
          "## Top-level areas", *[f"- {p}" for p in facts["top_level"]], "",
          "## Detected project files", *[f"- {p}" for p in markers], "",
          "## Recent commits", *[f"- {p}" for p in history]]
    (rundir.path / "research.md").write_text("\n".join(md) + "\n")
    return facts
