from regie.models import Attempt, Binding, RunState, TaskSpec, TaskState
from regie.rundir import RunDir
from regie.stats import collect, suggestions


def _run_with_attempts(regie_home, rid, build_specs, planner_specs=()):
    rd = RunDir.create(regie_home, rid)
    run = RunState(id=rid, target_repo="/x", branch=f"regie/{rid}")
    run.tasks["T1"] = TaskState(
        spec=TaskSpec(id="T1", title="t", profile="builder", criteria=["c"]))
    for model, outcome, turns in build_specs:
        run.tasks["T1"].attempts["build"].append(Attempt(
            binding=Binding(cli="claude", model=model), outcome=outcome, turns=turns))
    for model, outcome, turns in planner_specs:
        run.planner_attempts.append(Attempt(
            binding=Binding(cli="claude", model=model), outcome=outcome, turns=turns))
    rd.write_state(run)


def test_collect_aggregates_across_runs(regie_home):
    _run_with_attempts(regie_home, "r1",
                       [("sonnet", "failed", 10), ("sonnet", "failed", 12),
                        ("opus", "done", 30)])
    _run_with_attempts(regie_home, "r2", [("sonnet", "done", 20)],
                       planner_specs=[("opus", "done", 15)])
    stats = collect(regie_home)
    assert stats.runs == 2
    sonnet = stats.by_binding[("build", "claude:sonnet")]
    assert sonnet.attempts == 3 and sonnet.done == 1 and sonnet.failed == 2
    assert sonnet.first_attempts == 2 and sonnet.first_done == 1
    opus = stats.by_binding[("build", "claude:opus")]
    assert opus.escalation_done == 1
    assert stats.by_binding[("plan", "claude:opus")].done == 1


def test_suggestions_flag_weak_primary_and_strong_escalation(regie_home):
    # sonnet fails first attempt 5x; opus rescues twice as escalation
    for i in range(5):
        _run_with_attempts(regie_home, f"r{i}",
                           [("sonnet", "failed", 10), ("sonnet", "failed", 10),
                            ("opus", "done", 30)])
    stats = collect(regie_home)
    sugg = "\n".join(suggestions(stats))
    assert "claude:sonnet first-attempt success is 0%" in sugg
    assert "claude:opus succeeded" in sugg and "promoting" in sugg


def test_suggestions_quiet_on_thin_data(regie_home):
    _run_with_attempts(regie_home, "r1", [("sonnet", "failed", 10)])
    assert suggestions(collect(regie_home)) == []


def test_stats_cli_renders_table_and_suggestions(regie_home):
    from typer.testing import CliRunner

    from regie.cli import app
    for i in range(5):
        _run_with_attempts(regie_home, f"r{i}",
                           [("sonnet", "failed", 10), ("sonnet", "failed", 10),
                            ("opus", "done", 30)])
    result = CliRunner().invoke(app, ["stats"])
    assert result.exit_code == 0
    assert "claude:sonnet" in result.output and "build" in result.output
    assert "suggestions" in result.output.lower()


def test_stats_cli_renders_normalized_tokens(regie_home):
    from typer.testing import CliRunner

    from regie.cli import app
    _run_with_attempts(regie_home, "r1", [("sonnet", "done", 2)])
    rd = RunDir.open(regie_home, "r1")
    run = rd.read_state()
    attempt = run.tasks["T1"].attempts["build"][0]
    attempt.metrics.new_input_tokens = 100
    attempt.metrics.cached_input_tokens = 80
    attempt.metrics.output_tokens = 20
    attempt.metrics.tool_output_bytes = 1_000_000
    rd.write_state(run)

    result = CliRunner().invoke(app, ["stats", "--tokens"])
    assert result.exit_code == 0
    assert "token usage" in result.output and "tool MB" in result.output
    assert "done/MTok" in result.output
    assert "100" in result.output and "1.00" in result.output
