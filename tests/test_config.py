from pathlib import Path

import pytest

from regie.config import ConfigError, load_config

PROFILES = Path(__file__).parent.parent / "profiles"

GOOD_TOML = """
test_globs = ["tests/**"]
binding_strength = ["fake:m1", "codex:gpt-5.x", "claude:strongest"]
[commands]
test = "pytest -q"
lint = "ruff check ."
"""


def _repo_with(tmp_path, toml_text):
    (tmp_path / "regie.toml").write_text(toml_text)
    return tmp_path


def test_loads_commands_globs_and_profiles(tmp_path):
    cfg = load_config(_repo_with(tmp_path, GOOD_TOML), PROFILES)
    assert cfg.commands["test"] == "pytest -q"
    assert set(cfg.profiles) == {"planner", "test-writer", "builder", "reviewer", "debugger"}
    # Builder binds claude while the codex CLI is not installed on this
    # machine; the assertion checks the yaml loads, not a fixed vendor.
    assert cfg.profiles["builder"].binding.cli in ("claude", "codex")
    assert len(cfg.profiles["builder"].prompt_hash()) == 64


def test_missing_keys_reported_together(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_config(_repo_with(tmp_path, "[commands]\nlint='x'"), PROFILES)
    msg = str(exc.value)
    assert "commands.test" in msg and "test_globs" in msg and "binding_strength" in msg


def test_missing_regie_toml_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path, PROFILES)
