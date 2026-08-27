import warnings
from pathlib import Path

import pytest

from regie.config import ConfigError, Profile, load_config
from regie.models import Binding

PROFILES = Path(__file__).parent.parent / "profiles"

GOOD_TOML = """
test_globs = ["tests/**"]
[commands]
test = "pytest -q"
lint = "ruff check ."
"""

GOOD_TOML_WITH_BINDING_STRENGTH = """
test_globs = ["tests/**"]
binding_strength = ["fake:m1", "codex:gpt-5.x", "claude:strongest"]
[commands]
test = "pytest -q"
lint = "ruff check ."
"""


def _repo_with(tmp_path, toml_text):
    (tmp_path / "regie.toml").write_text(toml_text)
    return tmp_path


def _profiles_dir_with(tmp_path, yaml_body, dirname="profiles"):
    """A profiles/ directory holding a single profile 'solo' with the given yaml body."""
    d = tmp_path / dirname
    d.mkdir()
    (d / "solo.yaml").write_text(yaml_body)
    (d / "solo.md").write_text("You are solo.")
    return d


def test_loads_commands_globs_and_profiles(tmp_path):
    cfg = load_config(_repo_with(tmp_path, GOOD_TOML), PROFILES)
    assert cfg.commands["test"] == "pytest -q"
    assert {"planner", "test-writer", "builder", "reviewer", "debugger"} <= set(cfg.profiles)
    assert {"security-reviewer", "migration-reviewer", "api-reviewer",
            "ui-reviewer", "architecture-reviewer"} <= set(cfg.profiles)
    # Builder binds claude while the codex CLI is not installed on this
    # machine; the assertion checks the yaml loads, not a fixed vendor.
    assert cfg.profiles["builder"].primary.cli in ("claude", "codex")
    assert len(cfg.profiles["builder"].prompt_hash()) == 64


def test_submit_pr_can_be_disabled_for_local_evaluation(tmp_path):
    cfg = load_config(
        _repo_with(tmp_path, GOOD_TOML + "\n[workflow]\nsubmit_pr = false\n"),
        PROFILES,
    )

    assert cfg.workflow.submit_pr is False


def test_enabled_providers_filter_every_profile_ladder(tmp_path):
    repo = _repo_with(
        tmp_path,
        GOOD_TOML + '\n[providers]\nenabled = ["codex"]\n',
    )

    cfg = load_config(repo, PROFILES)

    assert cfg.enabled_providers == {"codex"}
    assert all(
        binding.cli == "codex"
        for profile in cfg.profiles.values()
        for binding in profile.bindings
    )
    assert all(
        profile.hard_binding is None or profile.hard_binding.cli == "codex"
        for profile in cfg.profiles.values()
    )


def test_provider_filter_rejects_profiles_without_a_viable_binding(tmp_path):
    profiles = _profiles_dir_with(
        tmp_path,
        "binding: { cli: claude, model: sonnet }\n",
    )
    repo = _repo_with(
        tmp_path,
        GOOD_TOML + '\n[providers]\nenabled = ["codex"]\n',
    )

    with pytest.raises(ConfigError, match="no bindings from enabled providers"):
        load_config(repo, profiles)


def test_missing_keys_reported_together(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_config(_repo_with(tmp_path, "[commands]\nlint='x'"), PROFILES)
    msg = str(exc.value)
    assert "commands.test" in msg and "test_globs" in msg


def test_missing_regie_toml_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path, PROFILES)


def test_bindings_list_loads_in_order(tmp_path):
    profiles = _profiles_dir_with(
        tmp_path,
        "bindings:\n"
        "  - { cli: claude, model: sonnet }\n"
        "  - { cli: claude, model: opus }\n"
        "budgets: { turns: 5 }\n",
    )
    cfg = load_config(_repo_with(tmp_path, GOOD_TOML), profiles)
    bindings = cfg.profiles["solo"].bindings
    assert bindings == [Binding(cli="claude", model="sonnet"), Binding(cli="claude", model="opus")]
    assert cfg.profiles["solo"].primary == Binding(cli="claude", model="sonnet")


def test_singular_binding_key_backward_compat(tmp_path):
    profiles = _profiles_dir_with(
        tmp_path,
        "binding: { cli: claude, model: sonnet }\n"
        "budgets: { turns: 5 }\n",
    )
    cfg = load_config(_repo_with(tmp_path, GOOD_TOML), profiles)
    assert cfg.profiles["solo"].bindings == [Binding(cli="claude", model="sonnet")]


def test_empty_bindings_list_is_config_error(tmp_path):
    profiles = _profiles_dir_with(
        tmp_path,
        "bindings: []\n"
        "budgets: { turns: 5 }\n",
    )
    with pytest.raises(ConfigError) as exc:
        load_config(_repo_with(tmp_path, GOOD_TOML), profiles)
    msg = str(exc.value)
    assert "solo" in msg
    assert "bindings" in msg and "empty" in msg


def test_missing_binding_keys_is_config_error(tmp_path):
    profiles = _profiles_dir_with(
        tmp_path,
        "budgets: { turns: 5 }\n",
    )
    with pytest.raises(ConfigError) as exc:
        load_config(_repo_with(tmp_path, GOOD_TOML), profiles)
    assert "solo" in str(exc.value)


def test_no_binding_strength_key_succeeds_and_leaves_no_attribute(tmp_path):
    profiles = _profiles_dir_with(
        tmp_path,
        "binding: { cli: claude, model: sonnet }\n"
        "budgets: { turns: 5 }\n",
    )
    cfg = load_config(_repo_with(tmp_path, GOOD_TOML), profiles)
    assert not hasattr(cfg, "binding_strength")


def test_stale_binding_strength_key_is_ignored(tmp_path):
    profiles = _profiles_dir_with(
        tmp_path,
        "binding: { cli: claude, model: sonnet }\n"
        "budgets: { turns: 5 }\n",
    )
    repo = _repo_with(tmp_path, GOOD_TOML_WITH_BINDING_STRENGTH)
    # AC5: the stale key must be genuinely silent — not an error, and not a
    # warning either, since existing target repos still carry it and callers
    # may run under -W error.
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        cfg = load_config(repo, profiles)
    assert not hasattr(cfg, "binding_strength")
    # and the rest of the config still came through
    assert cfg.test_globs == ["tests/**"]
    assert cfg.profiles["solo"].bindings == [Binding(cli="claude", model="sonnet")]


def test_shipped_profiles_are_migrated(tmp_path):
    """The shipped profiles all use the bindings-list form. The exact model
    lineup is operator policy (researched + live-validated 2026-07-31), so
    assert the durable invariants, not vendors: every profile loads, every
    list has at least two rungs (an escalation/failover path exists), and any
    profile whose primary is codex carries a cross-vendor (claude) rung so a
    quota hit can actually escape the starved provider — and vice versa.
    """
    cfg = load_config(_repo_with(tmp_path, GOOD_TOML), PROFILES)
    assert {"planner", "test-writer", "builder", "reviewer", "debugger"} <= set(cfg.profiles)
    for name, prof in cfg.profiles.items():
        assert len(prof.bindings) >= 2, f"{name} has no escalation rung"
        vendors = {b.cli for b in prof.bindings}
        assert len(vendors) >= 2, f"{name} has no cross-vendor quota escape"

def test_profile_binding_field_is_removed_not_aliased(tmp_path):
    profiles = _profiles_dir_with(
        tmp_path,
        "binding: { cli: claude, model: sonnet }\n"
        "budgets: { turns: 5 }\n",
    )
    cfg = load_config(_repo_with(tmp_path, GOOD_TOML), profiles)
    assert "binding" not in Profile.model_fields
    with pytest.raises(AttributeError):
        _ = cfg.profiles["solo"].binding


def test_bindings_list_wins_over_singular_binding_key(tmp_path):
    # A yaml carrying both keys is deliberately resolved by preferring the
    # plural `bindings:` list; `binding:` is legacy-only fallback (AC2).
    profiles = _profiles_dir_with(
        tmp_path,
        "binding: { cli: claude, model: opus }\n"
        "bindings:\n"
        "  - { cli: claude, model: sonnet }\n"
        "budgets: { turns: 5 }\n",
    )
    cfg = load_config(_repo_with(tmp_path, GOOD_TOML), profiles)
    assert cfg.profiles["solo"].bindings == [Binding(cli="claude", model="sonnet")]
