import json

import pytest

from regie.models import RunState
from regie.rundir import RunDir, RunLocked


def test_create_write_read_state(regie_home):
    rd = RunDir.create(regie_home, "r1")
    rd.write_state(RunState(id="r1", target_repo="/x", branch="regie/r1"))
    assert RunDir.open(regie_home, "r1").read_state().branch == "regie/r1"


def test_state_write_is_atomic_no_tmp_left_behind(regie_home):
    rd = RunDir.create(regie_home, "r1")
    rd.write_state(RunState(id="r1", target_repo="/x", branch="b"))
    assert not (rd.path / "state.json.tmp").exists()
    assert json.loads((rd.path / "state.json").read_text())["id"] == "r1"


def test_second_lock_refused(regie_home):
    rd1 = RunDir.create(regie_home, "r1")
    rd1.acquire_lock()
    rd2 = RunDir.open(regie_home, "r1")
    with pytest.raises(RunLocked):
        rd2.acquire_lock()
    rd1.release_lock()
    rd2.acquire_lock()  # now succeeds


def test_intents_and_events_append(regie_home):
    rd = RunDir.create(regie_home, "r1")
    rd.append_intent({"task": "T1", "stage": "build", "attempt": 1})
    rd.append_intent({"task": "T1", "stage": "build", "attempt": 2})
    assert [i["attempt"] for i in rd.read_intents()] == [1, 2]
    assert all("ts" in intent for intent in rd.read_intents())
    rd.append_event({"kind": "dispatch"})
    line = json.loads((rd.path / "events.jsonl").read_text().splitlines()[0])
    assert line["kind"] == "dispatch" and "ts" in line


def test_truncated_intent_line_is_ignored(regie_home):
    rd = RunDir.create(regie_home, "r1")
    rd.append_intent({"task": "T1"})
    with (rd.path / "intent.jsonl").open("a") as f:
        f.write('{"task": "T2", "trunc')  # simulated crash mid-append
    assert [i["task"] for i in rd.read_intents()] == ["T1"]
