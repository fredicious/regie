from __future__ import annotations

import subprocess
import sys


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def notify(title: str, message: str) -> None:
    """Best-effort desktop notification. Never raises."""
    if sys.platform == "darwin":
        script = f'display notification "{_escape(message)}" with title "{_escape(title)}"'
        try:
            subprocess.run(["osascript", "-e", script], check=False,
                           capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        print(f"[notify] {title}: {message}")
