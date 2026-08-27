import json
import subprocess

from regie.gates import _glob_match, diff_gate, red_test_gate, run_command_gate
from regie.gitops import commit_all


def test_command_gate_pass_and_fail(tmp_path):
    passed = run_command_gate("ok", "true", tmp_path)
    assert passed.passed
    assert passed.duration_seconds >= 0
    result = run_command_gate("boom", "echo nope && false", tmp_path)
    assert not result.passed and "nope" in result.detail
    assert result.duration_seconds >= 0


def test_command_gate_classifies_missing_tool_as_infrastructure(tmp_path):
    result = run_command_gate("test", "definitely-not-a-regie-command", tmp_path)
    assert not result.passed
    assert result.failure_kind == "infrastructure"


def test_command_gate_keeps_assertion_failure_as_code(tmp_path):
    result = run_command_gate(
        "test", "python -c 'raise AssertionError(\"wrong value\")'", tmp_path)
    assert not result.passed
    assert result.failure_kind == "code"


def test_flaky_rerun_marks_flaky(tmp_path):
    # fails first run, passes second: a file acts as the coin
    cmd = "test -f flag || { touch flag; false; }"
    result = run_command_gate("flaky", cmd, tmp_path, rerun_on_fail=True)
    assert result.passed and result.flaky


def test_diff_gate_blocks_test_edits(fixture_repo):
    (fixture_repo / "tests" / "test_calc.py").write_text("def test_add(): pass\n")
    result = diff_gate(fixture_repo, ["tests/**"])
    assert not result.passed and "test_calc.py" in result.detail


def test_diff_gate_allows_source_edits(fixture_repo):
    (fixture_repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b  # touched\n")
    assert diff_gate(fixture_repo, ["tests/**"]).passed


def test_red_gate_accepts_assertion_failure(fixture_repo):
    (fixture_repo / "tests" / "test_new.py").write_text(
        "def test_new():\n    assert 1 == 2\n")
    commit_all(fixture_repo, "add red test")
    result = red_test_gate(fixture_repo, "python -m pytest tests/test_new.py -q")
    assert result.passed and "red-suite" in result.detail


def test_red_gate_rejects_import_error(fixture_repo):
    (fixture_repo / "tests" / "test_new.py").write_text(
        "from src.nonexistent import thing\n\ndef test_new():\n    assert thing()\n")
    result = red_test_gate(fixture_repo, "python -m pytest tests/test_new.py -q")
    assert not result.passed


def test_red_gate_rejects_green_tests(fixture_repo):
    (fixture_repo / "tests" / "test_new.py").write_text("def test_new():\n    assert True\n")
    result = red_test_gate(fixture_repo, "python -m pytest tests/test_new.py -q")
    assert not result.passed and "unexpectedly-green" in result.detail


def test_diff_gate_blocks_rename_into_tests(fixture_repo):
    subprocess.run(
        ["git", "mv", "src/calc.py", "tests/calc_snuck_in.py"],
        cwd=fixture_repo, check=True,
    )
    result = diff_gate(fixture_repo, ["tests/**"])
    assert not result.passed
    assert "calc_snuck_in.py" in result.detail


def test_diff_gate_blocks_rename_with_space_into_tests(fixture_repo):
    subprocess.run(
        ["git", "mv", "src/calc.py", "tests/test file.py"],
        cwd=fixture_repo, check=True,
    )
    result = diff_gate(fixture_repo, ["tests/**"])
    assert not result.passed
    assert "test file.py" in result.detail


def test_diff_gate_matches_root_level_file_with_leading_globstar(fixture_repo):
    (fixture_repo / "foo.test.js").write_text("// new file\n")
    result = diff_gate(fixture_repo, ["**/*.test.*"])
    assert not result.passed
    assert "foo.test.js" in result.detail


def test_diff_gate_single_star_does_not_cross_directories(fixture_repo):
    nested = fixture_repo / "apps" / "webapp" / "nested" / "tests"
    nested.mkdir(parents=True)
    (nested / "foo.py").write_text("# nested test-like file\n")
    result = diff_gate(fixture_repo, ["apps/*/tests/**"])
    assert result.passed


def test_glob_translator_table():
    assert _glob_match("foo.test.js", "**/*.test.*")
    assert _glob_match("a/b/foo.test.js", "**/*.test.*")
    assert _glob_match("apps/webapp/tests/foo.py", "apps/*/tests/**")
    assert not _glob_match("apps/webapp/nested/tests/foo.py", "apps/*/tests/**")
    assert _glob_match("tests/test_calc.py", "tests/**")
    assert _glob_match("tests", "tests/**")
    assert not _glob_match("src/calc.py", "tests/**")


def test_red_gate_accepts_domain_exception_red(fixture_repo):
    """Dogfood finding: honest reds against unwritten code fail with domain
    exceptions (KeyError, ConfigError, pytest.raises mismatches) — not only
    AssertionError/NotImplementedError."""
    (fixture_repo / "tests" / "test_new.py").write_text(
        "def test_new():\n    raise KeyError('bindings')\n")
    commit_all(fixture_repo, "add domain-exception red test")
    result = red_test_gate(fixture_repo, "python -m pytest tests/test_new.py -q")
    assert result.passed and "red-suite" in result.detail


def test_red_gate_accepts_fixture_error_red(fixture_repo):
    """Breaking-change red: a fixture raising at setup against a
    not-yet-written API reports as pytest ERROR — still an honest red."""
    (fixture_repo / "tests" / "test_new.py").write_text(
        "import pytest\n\n@pytest.fixture\ndef fx():\n"
        "    raise KeyError('bindings')\n\n"
        "def test_new(fx):\n    assert fx\n")
    commit_all(fixture_repo, "add fixture-error red test")
    result = red_test_gate(fixture_repo, "python -m pytest tests/test_new.py -q")
    assert result.passed and "red-suite" in result.detail


def test_red_gate_accepts_node_test_assertion_failure(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "scripts": {"test": "node --test"},
        "type": "module",
    }))
    (tmp_path / "red.test.js").write_text(
        "import test from 'node:test';\n"
        "import assert from 'node:assert/strict';\n"
        "test('red', () => assert.equal(1, 2));\n"
    )

    result = red_test_gate(tmp_path, "npm test")

    assert result.passed and result.detail == "red-suite"


def test_red_gate_rejects_node_test_syntax_error(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "scripts": {"test": "node --test"},
        "type": "module",
    }))
    (tmp_path / "broken.test.js").write_text("this is not valid javascript !!!\n")

    result = red_test_gate(tmp_path, "npm test")

    assert not result.passed and "collection-error" in result.detail
