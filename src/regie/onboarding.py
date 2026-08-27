from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PROVIDERS = ("claude", "codex")


@dataclass
class ProjectDetection:
    language: str
    setup: str | None
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
            language="python",
            setup="uv sync --frozen" if (repo / "uv.lock").exists() else None,
            test="uv run pytest -q", lint="uvx ruff check .",
            typecheck="uv run mypy ." if "mypy" in (repo / "pyproject.toml").read_text() else None,
            build=None, coverage="uv run pytest --cov --cov-report=term-missing",
            test_globs=["tests/**", "**/test_*.py"], ui=False)
    if (repo / "package.json").exists():
        text = (repo / "package.json").read_text()
        runner = "pnpm" if (repo / "pnpm-lock.yaml").exists() else "npm"
        run = f"{runner} run"
        return ProjectDetection(
            language="typescript" if (repo / "tsconfig.json").exists() else "javascript",
            setup=bootstrap_command(repo),
            test=f"{run} test", lint=f"{run} lint",
            typecheck=f"{run} typecheck" if "typecheck" in text else None,
            build=f"{run} build" if "build" in text else None,
            coverage=f"{run} test -- --coverage",
            test_globs=["**/*.test.*", "**/*.spec.*", "tests/**"],
            ui=any(name in text for name in ("react", "next", "vue", "svelte")))
    if (repo / "Cargo.toml").exists():
        return ProjectDetection("rust", "cargo fetch", "cargo test", "cargo clippy -- -D warnings",
                                None, "cargo build", None, ["tests/**", "**/*_test.rs"], False)
    if (repo / "go.mod").exists():
        return ProjectDetection("go", "go mod download", "go test ./...", "go vet ./...", None,
                                "go build ./...", "go test -cover ./...",
                                ["**/*_test.go"], False)
    return ProjectDetection("unknown", None, "true", "true", None, None, None,
                            ["tests/**"], False)


def bootstrap_command(repo: Path) -> str | None:
    """Infer a lockfile-respecting dependency bootstrap for an isolated worktree."""
    if (repo / "pnpm-lock.yaml").exists():
        return "pnpm install --frozen-lockfile"
    if (repo / "bun.lock").exists() or (repo / "bun.lockb").exists():
        return "bun install --frozen-lockfile"
    if (repo / "yarn.lock").exists():
        return "yarn install --frozen-lockfile"
    if (repo / "package-lock.json").exists():
        return "npm ci"
    if (repo / "package.json").exists():
        return "npm install"
    if (repo / "uv.lock").exists():
        return "uv sync --frozen"
    if (repo / "Cargo.toml").exists():
        return "cargo fetch"
    if (repo / "go.mod").exists():
        return "go mod download"
    return None


def render_config(
    detection: ProjectDetection,
    enabled_providers: tuple[str, ...] = DEFAULT_PROVIDERS,
) -> str:
    lines = [
        f"test_globs = {detection.test_globs!r}".replace("'", '"'),
        "", "[workflow]", 'default_tier = "auto"', "max_parallel_tasks = 3",
        "direct_execution = true",
        "plan_reviews = true", "design_reviews = true", "final_review = true",
        "knowledge = true", "reflection = true", "", "[commands]",
    ]
    if detection.setup:
        lines.append(f'setup = "{detection.setup}"')
    lines += [
        f'test = "{detection.test}"', f'lint = "{detection.lint}"',
    ]
    for name in ("typecheck", "build", "coverage"):
        value = getattr(detection, name)
        if value:
            lines.append(f'{name} = "{value}"')
    if detection.ui:
        lines += ["", "[gates.visual]", 'command = "npx playwright test"',
                  'stages = ["finalize"]',
                  'trigger_globs = ["**/*.tsx", "**/*.jsx", "**/*.svelte", "**/*.css"]',
                  'tiers = ["standard", "critical"]']
    lines += ["", "[providers]", f"enabled = {json.dumps(list(enabled_providers))}"]
    return "\n".join(lines) + "\n"


def initialize(
    repo: Path,
    detection: ProjectDetection | None = None,
    enabled_providers: tuple[str, ...] = DEFAULT_PROVIDERS,
) -> ProjectDetection:
    """Detect project tooling and write its starter Régie configuration."""
    detection = detection or detect(repo)
    (repo / "regie.toml").write_text(render_config(detection, enabled_providers))
    return detection


def enabled_providers(repo: Path) -> tuple[str, ...]:
    """Read provider preferences, defaulting old configs to Claude and Codex."""
    try:
        data = tomllib.loads((repo / "regie.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return DEFAULT_PROVIDERS
    raw = data.get("providers", {}).get("enabled", DEFAULT_PROVIDERS)
    if not isinstance(raw, list) or not all(isinstance(name, str) for name in raw):
        return DEFAULT_PROVIDERS
    return tuple(raw)


def update_providers(repo: Path, providers: tuple[str, ...]) -> None:
    """Update only providers.enabled while preserving the rest of regie.toml."""
    if not providers:
        raise ValueError("at least one provider must be enabled")
    path = repo / "regie.toml"
    text = path.read_text()
    value = f"enabled = {json.dumps(list(providers))}"
    section = re.search(r"(?m)^\[providers\]\s*$", text)
    if section is None:
        text = text.rstrip() + f"\n\n[providers]\n{value}\n"
    else:
        next_section = re.search(r"(?m)^\[[^\]]+\]\s*$", text[section.end():])
        end = section.end() + (next_section.start() if next_section else len(text))
        body = text[section.end():end]
        if re.search(r"(?m)^[ \t]*enabled[ \t]*=.*$", body):
            body = re.sub(r"(?m)^[ \t]*enabled[ \t]*=.*$", value, body, count=1)
        else:
            body = f"\n{value}" + body
        text = text[:section.end()] + body + text[end:]
    path.write_text(text)
