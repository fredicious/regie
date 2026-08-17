from regie.ladder import next_action
from regie.models import Attempt, Binding

B0 = Binding(cli="fake", model="m1")
B1 = Binding(cli="fake", model="m2")
B2 = Binding(cli="fake", model="m3")
TWO_RUNGS = [B0, B1]
THREE_RUNGS = [B0, B1, B2]


def _failed(n: int, binding: Binding = B0) -> list[Attempt]:
    return [Attempt(binding=binding, outcome="failed") for _ in range(n)]


def test_ac7_one_failed_attempt_retries_primary_binding():
    assert next_action(_failed(1), TWO_RUNGS) == ("retry", B0)


def test_ac8_two_failed_attempts_escalate_to_second_binding():
    assert next_action(_failed(2), TWO_RUNGS) == ("escalate", B1)


def test_ac9_three_failed_attempts_escalate_to_third_binding():
    assert next_action(_failed(3), THREE_RUNGS) == ("escalate", B2)


def test_ac10_exhaustion_halts_on_two_element_list():
    assert next_action(_failed(3), TWO_RUNGS)[0] == "halt"


def test_ac10_exhaustion_halts_on_single_element_list():
    assert next_action(_failed(2), [B0])[0] == "halt"


def test_binding_recorded_on_attempt_absent_from_list_does_not_halt():
    """D1: index comes from the attempt COUNT, never from looking the
    recorded binding up in the list — a stale/foreign binding on a past
    attempt (e.g. list reconfigured mid-run) must not raise or halt."""
    stale = Binding(cli="fake", model="retired")
    action, binding = next_action([Attempt(binding=stale, outcome="failed")], TWO_RUNGS)
    assert (action, binding) == ("retry", B0)


def test_budget_death_escalates_without_same_model_retry():
    attempts = [Attempt(binding=B0, outcome="failed", failure_kind="budget")]
    assert next_action(attempts, TWO_RUNGS) == ("escalate", B1)
