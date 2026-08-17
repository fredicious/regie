from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class ProjectDetection:
    language: str
    test: str
    lint: str
    typecheck: str | None
    build: str | None
    coverage: str | None
    test_globs: list[str]
    ui: bool


def detect(repo: Path) -> ProjectDetection:
    if (repo / "pyproject.toml").exists():
        return ProjectDetection(
            language="python", test="uv run pytest -q", lint="uvx ruff check .",
            typecheck="uv run mypy ." if "mypy" in (repo / "pyproject.toml").read_text() else None,
            build=None, coverage="uv run pytest --cov --cov-report=term-missing",
            test_globs=["tests/**", "**/test_*.py"], ui=False)
    if (repo / "package.json").exists():
        text = (repo / "package.json").read_text()
        runner = "pnpm" if (repo / "pnpm-lock.yaml").exists() else "npm"
        run = f"{runner} run"
        return ProjectDetection(
            language="typescript" if (repo / "tsconfig.json").exists() else "javascript",
            test=f"{run} test", lint=f"{run} lint",
            typecheck=f"{run} typecheck" if "typecheck" in text else None,
            build=f"{run} build" if "build" in text else None,
            coverage=f"{run} test -- --coverage",
            test_globs=["**/*.test.*", "**/*.spec.*", "tests/**"],
            ui=any(name in text for name in ("react", "next", "vue", "svelte")))
    if (repo / "Cargo.toml").exists():
        return ProjectDetection("rust", "cargo test", "cargo clippy -- -D warnings",
                                None, "cargo build", None, ["tests/**", "**/*_test.rs"], False)
    if (repo / "go.mod").exists():
        return ProjectDetection("go", "go test ./...", "go vet ./...", None,
                                "go build ./...", "go test -cover ./...",
                                ["**/*_test.go"], False)
    return ProjectDetection("unknown", "true", "true", None, None, None,
                            ["tests/**"], False)


def render_config(detection: ProjectDetection) -> str:
    lines = [
        f"test_globs = {detection.test_globs!r}".replace("'", '"'),
        "", "[workflow]", 'default_tier = "auto"', "max_parallel_tasks = 3",
        "plan_reviews = true", "design_reviews = true", "final_review = true",
        "knowledge = true", "reflection = true", "", "[commands]",
        f'test = "{detection.test}"', f'lint = "{detection.lint}"',
    ]
    for name in ("typecheck", "build", "coverage"):
        value = getattr(detection, name)
        if value:
            lines.append(f'{name} = "{value}"')
    if detection.ui:
        lines += ["", "[gates.visual]", 'command = "npx playwright test"',
                  'stages = ["finalize"]', 'trigger_globs = ["**/*.tsx", "**/*.css"]',
                  'tiers = ["standard", "critical"]']
    return "\n".join(lines) + "\n"
