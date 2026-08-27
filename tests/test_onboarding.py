from regie.onboarding import (
    detect,
    enabled_providers,
    initialize,
    render_config,
    update_providers,
)


def test_detects_python_and_renders_workflow_config(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    detection = detect(tmp_path)
    rendered = render_config(detection)

    assert detection.language == "python"
    assert "uv run pytest" in detection.test
    assert "[workflow]" in rendered
    assert "max_parallel_tasks = 3" in rendered
    assert "direct_execution = true" in rendered
    assert "[commands]" in rendered


def test_detects_ui_project_and_visual_gate(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"scripts":{"test":"vitest","lint":"eslint","build":"vite"},'
        '"dependencies":{"react":"latest"}}')
    (tmp_path / "pnpm-lock.yaml").write_text("")
    detection = detect(tmp_path)

    assert detection.ui
    rendered = render_config(detection)
    assert detection.setup == "pnpm install --frozen-lockfile"
    assert 'setup = "pnpm install --frozen-lockfile"' in rendered
    assert "[gates.visual]" in rendered
    assert "**/*.svelte" in rendered


def test_initialize_writes_detected_starter_config(tmp_path):
    (tmp_path / "go.mod").write_text("module example.test/project\n")

    detection = initialize(tmp_path)

    assert detection.language == "go"
    assert 'test = "go test ./..."' in (tmp_path / "regie.toml").read_text()
    assert enabled_providers(tmp_path) == ("claude", "codex")


def test_provider_preferences_update_without_rewriting_other_config(tmp_path):
    (tmp_path / "regie.toml").write_text(
        'test_globs = ["tests/**"]\n'
        'custom_key = "keep-me"\n'
        '[providers]\n'
        'enabled = ["claude", "codex"]\n'
        'future_setting = true\n'
        '[commands]\n'
        'test = "pytest"\n'
        'lint = "ruff"\n'
    )

    update_providers(tmp_path, ("codex",))

    text = (tmp_path / "regie.toml").read_text()
    assert enabled_providers(tmp_path) == ("codex",)
    assert 'custom_key = "keep-me"' in text
    assert "future_setting = true" in text
