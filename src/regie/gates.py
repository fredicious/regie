from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from pathlib import Path

from regie.gitops import changed_files
from regie.models import GateResult

_TAIL = 4000


def _segment_regex(segment: str) -> str:
    """Translate one path segment (no `/`) to regex: `*`/`?` stay within it."""
    out = []
    for ch in segment:
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
    return "".join(out)


@lru_cache(maxsize=256)
def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a gitignore-style glob to a regex with correct segment semantics:
    `**` matches zero or more whole path segments (including none, so
    `**/x` also matches root-level `x`), while a bare `*` or `?` matches only
    within a single segment and never crosses a `/`. Python 3.12's stdlib has
    no correct primitive for this (`fnmatch` lets `*` cross slashes; the
    3.13-only `PurePosixPath.full_match` was unavailable), hence this
    translator.
    """
    segments = pattern.split("/")
    n = len(segments)
    pieces: list[str] = []
    for i, seg in enumerate(segments):
        is_first = i == 0
        is_last = i == n - 1
        if seg == "**":
            if is_first and is_last:
                frag = ".*"
            elif is_first:
                frag = "(?:.*/)?"
            elif is_last:
                frag = "(?:/.*)?"
            else:
                frag = "(?:.*/)?"
        else:
            frag = _segment_regex(seg)
        # A '**' fragment always folds the adjoining slash into itself
        # (whether at the start, in the middle, or trailing); only insert an
        # explicit separator between two plain segments.
        add_sep = i > 0 and segments[i - 1] != "**" and not (seg == "**" and is_last)
        if add_sep:
            pieces.append("/")
        pieces.append(frag)
    return re.compile("^" + "".join(pieces) + "$")


def _glob_match(path: str, pattern: str) -> bool:
    return _glob_to_regex(pattern).match(path) is not None


def match_globs(path: str, globs: list[str]) -> bool:
    return any(_glob_match(path, g) for g in globs)


def _run(cmd: str, cwd: Path) -> tuple[int, str]:
    # shell=True is deliberate: cmd is an operator-authored shell string from
    # regie.toml (same trust level as a Makefile), never agent output or task
    # data. Invariant: nothing agent-generated is ever interpolated into a gate
    # command string; agents can only influence gate outcomes through the files
    # the commands inspect.
    proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True,
                          check=False)
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
    hits = [f for f in changed_files(repo) if match_globs(f, test_globs)]
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
