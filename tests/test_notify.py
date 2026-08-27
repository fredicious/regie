import subprocess

import pytest

from regie.notify import notifications_enabled, notify


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", " OFF "])
def test_notifications_can_be_disabled(monkeypatch, value):
    monkeypatch.setenv("REGIE_NOTIFICATIONS", value)
    monkeypatch.setattr(
        "regie.notify.subprocess.run",
        lambda *args, **kwargs: pytest.fail("disabled notification reached the OS"),
    )

    assert notifications_enabled() is False
    notify("regie test", "must stay silent")


def test_notifications_remain_enabled_by_default(monkeypatch):
    calls = []
    monkeypatch.delenv("REGIE_NOTIFICATIONS", raising=False)
    monkeypatch.setattr("regie.notify.sys.platform", "darwin")
    monkeypatch.setattr(
        "regie.notify.subprocess.run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    notify("regie ready", "run complete")

    assert notifications_enabled() is True
    assert calls[0][0][0][:2] == ["osascript", "-e"]
    assert calls[0][1] == {
        "check": False,
        "capture_output": True,
        "timeout": 5,
    }


def test_notification_timeout_is_still_best_effort(monkeypatch):
    monkeypatch.delenv("REGIE_NOTIFICATIONS", raising=False)
    monkeypatch.setattr("regie.notify.sys.platform", "darwin")

    def time_out(*args, **kwargs):
        raise subprocess.TimeoutExpired("osascript", 5)

    monkeypatch.setattr("regie.notify.subprocess.run", time_out)

    notify("regie ready", "run complete")
