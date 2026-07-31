from regie.preflight import all_passed, preflight


def test_preflight_runs_present_commands_in_order(tmp_path):
    results = preflight({"lint": "true", "test": "true", "typecheck": "true"}, tmp_path)
    # order is lint, typecheck, test — and only present commands run
    assert [r.name for r in results] == ["lint", "typecheck", "test"]
    assert all_passed(results)


def test_preflight_verdict_is_exit_code_not_output(tmp_path):
    # a command that prints reassuring text but exits nonzero must FAIL —
    # the exact trap that shipped a lint error to CI (tail said "ok", exit 1)
    results = preflight({"lint": "echo 'All checks passed!'; exit 1",
                         "test": "true"}, tmp_path)
    lint = next(r for r in results if r.name == "lint")
    assert lint.passed is False and lint.exit_code == 1
    assert "All checks passed" in lint.tail  # output captured, but not the verdict
    assert not all_passed(results)


def test_preflight_skips_absent_commands(tmp_path):
    results = preflight({"test": "true"}, tmp_path)
    assert [r.name for r in results] == ["test"]
