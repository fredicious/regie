from regie.onboarding import detect, render_config


def test_detects_python_and_renders_workflow_config(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    detection = detect(tmp_path)
    rendered = render_config(detection)

    assert detection.language == "python"
    assert "uv run pytest" in detection.test
    assert "[workflow]" in rendered
    assert "max_parallel_tasks = 3" in rendered
    assert "[commands]" in rendered


def test_detects_ui_project_and_visual_gate(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"scripts":{"test":"vitest","lint":"eslint","build":"vite"},'
        '"dependencies":{"react":"latest"}}')
    (tmp_path / "pnpm-lock.yaml").write_text("")
    detection = detect(tmp_path)

    assert detection.ui
    assert "[gates.visual]" in render_config(detection)
