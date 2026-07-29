from regie.ladder import next_action
from regie.models import Attempt, Binding

ORDER = ["fake:m1", "fake:m2", "claude:strongest"]
B1 = Binding(cli="fake", model="m1")


def _fails(n: int) -> list[Attempt]:
    return [Attempt(binding=B1, outcome="failed") for _ in range(n)]


def test_first_two_failures_retry_same_binding():
    assert next_action(_fails(1), B1, ORDER) == ("retry", B1)


def test_third_attempt_escalates_to_next_stronger():
    action, binding = next_action(_fails(2), B1, ORDER)
    assert action == "escalate" and binding.model == "m2"


def test_top_binding_skips_escalation_and_halts():
    top = Binding(cli="claude", model="strongest")
    attempts = [Attempt(binding=top, outcome="failed")] * 2
    assert next_action(attempts, top, ORDER)[0] == "halt"


def test_exhausted_halts():
    assert next_action(_fails(3), B1, ORDER)[0] == "halt"


def test_quota_halts_immediately_without_burning_ladder():
    attempts = [Attempt(binding=B1, outcome="quota")]
    assert next_action(attempts, B1, ORDER)[0] == "halt"
