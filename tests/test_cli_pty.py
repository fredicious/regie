from __future__ import annotations

import fcntl
import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import termios
import time

from regie.models import RunState
from regie.rundir import RunDir


def test_bare_regie_launches_and_quits_in_a_real_pty(tmp_path):
    """Exercise the installed console entry point through a real terminal."""
    project = tmp_path / "blank-project"
    project.mkdir()
    regie_home = tmp_path / "regie-home"
    regie_home.mkdir()
    rundir = RunDir.create(regie_home, "pty-smoke")
    rundir.write_state(RunState(
        id="pty-smoke",
        target_repo=str(project),
        branch="regie/pty-smoke",
        stage="halted",
        halt_reason="PTY smoke fixture",
    ))
    executable = shutil.which("regie")
    assert executable is not None

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    env = os.environ.copy()
    env.update({
        "REGIE_HOME": str(regie_home),
        "REGIE_NOTIFICATIONS": "0",
        "TERM": "xterm-256color",
        "COLUMNS": "100",
        "LINES": "30",
    })
    process = subprocess.Popen(
        [executable],
        cwd=project,
        env=env,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    output = bytearray()
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and b"Control Room" not in output:
            readable, _, _ = select.select([master], [], [], 0.25)
            if readable:
                output.extend(os.read(master, 65_536))
            if process.poll() is not None:
                break
        assert b"Control Room" in output, output[-2_000:].decode(errors="replace")

        os.write(master, b"q")
        exit_deadline = time.monotonic() + 10
        while time.monotonic() < exit_deadline and process.poll() is None:
            readable, _, _ = select.select([master], [], [], 0.1)
            if readable:
                try:
                    os.read(master, 65_536)
                except OSError:
                    break
        process.wait(timeout=1)
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        os.close(master)
