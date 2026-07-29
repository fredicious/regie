from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path

from regie.gitops import changed_files
from regie.models import GateResult

_TAIL = 4000


def _run(cmd: str, cwd: Path) -> tuple[int, str]:
    # shell=True is deliberate: cmd is an operator-authored shell string from
    # regie.toml (same trust level as a Makefile), never agent output or task
    # data. Invariant: nothing agent-generated is ever interpolated into a gate
    # command string; agents can only influence gate outcomes through the files
    # the commands inspect.
    proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr)[-_TAIL:]


def run_command_gate(name: str, cmd: str, cwd: Path,
                     rerun_on_fail: bool = False) -> GateResult:
    code, output = _run(cmd, cwd)
    if code == 0:
        return GateResult(name=name, passed=True, detail=output)
    if rerun_on_fail:
        code2, output2 = _run(cmd, cwd)
        if code2 == 0:
            return GateResult(name=name, passed=True, detail=output2, flaky=True)
        output = output2
    return GateResult(name=name, passed=False, detail=output)


def diff_gate(repo: Path, test_globs: list[str]) -> GateResult:
    hits = [f for f in changed_files(repo)
            if any(fnmatch.fnmatch(f, g) for g in test_globs)]
    if hits:
        return GateResult(name="diff-guard", passed=False,
                          detail=f"test files modified: {', '.join(hits)}")
    return GateResult(name="diff-guard", passed=True)


def red_test_gate(cwd: Path, test_cmd: str) -> GateResult:
    collect_code, collect_out = _run(f"{test_cmd} --collect-only", cwd)
    if collect_code != 0:
        return GateResult(name="tdd-red", passed=False,
                          detail=f"collection-error: {collect_out[-1000:]}")
    code, output = _run(test_cmd, cwd)
    if code == 0:
        return GateResult(name="tdd-red", passed=False, detail="unexpectedly-green")
    if "ImportError" in output or "ModuleNotFoundError" in output or "SyntaxError" in output:
        return GateResult(name="tdd-red", passed=False,
                          detail=f"import-error: {output[-1000:]}")
    if "NotImplementedError" in output:
        return GateResult(name="tdd-red", passed=True, detail="notimplemented")
    if "AssertionError" in output or "assert" in output:
        return GateResult(name="tdd-red", passed=True, detail="assertion")
    return GateResult(name="tdd-red", passed=False,
                      detail=f"failed for unrecognized reason: {output[-1000:]}")
