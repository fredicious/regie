"""Preflight: run a repo's own gate commands and report pass/fail strictly by
exit code. Exists because a gate is only honest if its verdict comes from the
command's exit status, never from eyeballing tail output — the exact mistake
that shipped a lint error to CI on 2026-07-30.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

# The commands worth checking before dispatching agents, in run order. Only
# those present in the repo's regie.toml [commands] are run.
_PREFLIGHT_COMMANDS = ("lint", "typecheck", "test")


@dataclass
class CommandResult:
    name: str
    command: str
    passed: bool
    exit_code: int
    tail: str  # last chunk of combined output, for the human — never the verdict


def _run(command: str, cwd: Path) -> tuple[int, str]:
    # shell=True is deliberate and matches gates._run: the command is an
    # operator-authored shell string from regie.toml [commands] (Makefile
    # trust level), never agent output or task data. Nothing agent-generated
    # is ever interpolated here.
    proc = subprocess.run(command, shell=True, cwd=cwd, capture_output=True,
                          text=True, check=False)
    return proc.returncode, (proc.stdout + proc.stderr)[-2000:]


def preflight(commands: dict[str, str], repo: Path) -> list[CommandResult]:
    """Run each known preflight command present in `commands`, in order.
    The verdict is the exit code and nothing else."""
    results: list[CommandResult] = []
    for name in _PREFLIGHT_COMMANDS:
        command = commands.get(name)
        if not command:
            continue
        code, tail = _run(command, repo)
        results.append(CommandResult(name=name, command=command,
                                     passed=(code == 0), exit_code=code, tail=tail))
    return results


def all_passed(results: list[CommandResult]) -> bool:
    return all(r.passed for r in results)
