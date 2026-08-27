from __future__ import annotations

import os
import subprocess
import sys

_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def notifications_enabled() -> bool:
    """Return whether desktop notifications are enabled for this process."""
    value = os.environ.get("REGIE_NOTIFICATIONS")
    return value is None or value.strip().lower() not in _FALSE_VALUES


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def notify(title: str, message: str) -> None:
    """Best-effort desktop notification. Never raises."""
    if not notifications_enabled():
        return
    if sys.platform == "darwin":
        script = f'display notification "{_escape(message)}" with title "{_escape(title)}"'
        try:
            subprocess.run(["osascript", "-e", script], check=False,
                           capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        print(f"[notify] {title}: {message}")
