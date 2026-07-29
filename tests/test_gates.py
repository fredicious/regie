from regie.gates import diff_gate, red_test_gate, run_command_gate
from regie.gitops import commit_all


def test_command_gate_pass_and_fail(tmp_path):
    assert run_command_gate("ok", "true", tmp_path).passed
    result = run_command_gate("boom", "echo nope && false", tmp_path)
    assert not result.passed and "nope" in result.detail


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
    assert result.passed and "assertion" in result.detail


def test_red_gate_rejects_import_error(fixture_repo):
    (fixture_repo / "tests" / "test_new.py").write_text(
        "from src.nonexistent import thing\n\ndef test_new():\n    assert thing()\n")
    result = red_test_gate(fixture_repo, "python -m pytest tests/test_new.py -q")
    assert not result.passed


def test_red_gate_rejects_green_tests(fixture_repo):
    (fixture_repo / "tests" / "test_new.py").write_text("def test_new():\n    assert True\n")
    result = red_test_gate(fixture_repo, "python -m pytest tests/test_new.py -q")
    assert not result.passed and "unexpectedly-green" in result.detail
