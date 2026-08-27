import pytest

from regie.models import RunState
from regie.operations import record_clarification
from regie.rundir import RunDir


def test_record_clarification_persists_question_answer_and_event(
        regie_home, fixture_repo):
    rundir = RunDir.create(regie_home, "r1")
    state = RunState(
        id="r1", target_repo=str(fixture_repo), branch="regie/r1",
        stage="halted", halt_reason="blocked: clarify: Should selection use Shift?",
    )
    rundir.write_state(state)

    question = record_clarification(rundir, state, "Yes, use Shift-click.")

    assert question == "Should selection use Shift?"
    decisions = (rundir.path / "decisions.md").read_text()
    assert "Should selection use Shift?" in decisions
    assert "Yes, use Shift-click." in decisions
    assert '"action": "clarify"' in (rundir.path / "events.jsonl").read_text()


def test_record_clarification_rejects_unrelated_halt(regie_home, fixture_repo):
    rundir = RunDir.create(regie_home, "r1")
    state = RunState(
        id="r1", target_repo=str(fixture_repo), branch="regie/r1",
        stage="halted", halt_reason="quota exhausted",
    )
    with pytest.raises(ValueError, match="not awaiting clarification"):
        record_clarification(rundir, state, "anything")
