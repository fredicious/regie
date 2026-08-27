from __future__ import annotations

import re
import subprocess
import time
from functools import lru_cache
from pathlib import Path

from regie.gitops import changed_files
from regie.models import GateResult

_TAIL = 4000
_INFRASTRUCTURE_FAILURE = re.compile(
    r"(?:"
    r"(?:command not found|executable .*not found|executable doesn't exist)|"
    r"(?:\bENOENT\b|\bENOTFOUND\b|\bECONNREFUSED\b|\bETIMEDOUT\b)|"
    r"(?:ERR_PNPM_(?:META_FETCH|FETCH)|unable to resolve package registry)|"
    r"(?:chromium|chrome|firefox|webkit).*(?:not found|isn't installed)|"
    r"server files not found.*did you run [`']?build"
    r")",
    re.IGNORECASE | re.DOTALL,
)


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


def _run(cmd: str, cwd: Path) -> tuple[int, str, float]:
    # shell=True is deliberate: cmd is an operator-authored shell string from
    # regie.toml (same trust level as a Makefile), never agent output or task
    # data. Invariant: nothing agent-generated is ever interpolated into a gate
    # command string; agents can only influence gate outcomes through the files
    # the commands inspect.
    started = time.monotonic()
    proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True,
                          check=False)
    duration = time.monotonic() - started
    return proc.returncode, (proc.stdout + proc.stderr)[-_TAIL:], duration


def classify_gate_failure(output: str) -> str:
    """Separate unavailable tooling/runtime from failures in the submitted code."""
    return "infrastructure" if _INFRASTRUCTURE_FAILURE.search(output) else "code"


def run_command_gate(name: str, cmd: str, cwd: Path,
                     rerun_on_fail: bool = False) -> GateResult:
    code, output, duration = _run(cmd, cwd)
    if code == 0:
        return GateResult(
            name=name, passed=True, detail=output, duration_seconds=duration)
    if rerun_on_fail:
        code2, output2, rerun_duration = _run(cmd, cwd)
        duration += rerun_duration
        if code2 == 0:
            return GateResult(
                name=name,
                passed=True,
                detail=output2,
                flaky=True,
                duration_seconds=duration,
            )
        output = output2
    return GateResult(
        name=name,
        passed=False,
        detail=output,
        failure_kind=classify_gate_failure(output),
        duration_seconds=duration,
    )


def diff_gate(repo: Path, test_globs: list[str]) -> GateResult:
    started = time.monotonic()
    hits = [f for f in changed_files(repo) if match_globs(f, test_globs)]
    duration = time.monotonic() - started
    if hits:
        return GateResult(name="diff-guard", passed=False,
                          detail=f"test files modified: {', '.join(hits)}",
                          duration_seconds=duration)
    return GateResult(name="diff-guard", passed=True, duration_seconds=duration)


def red_test_gate(cwd: Path, test_cmd: str) -> GateResult:
    collect_code, collect_out, collect_duration = _run(
        f"{test_cmd} --collect-only", cwd)
    if collect_code != 0:
        return GateResult(name="tdd-red", passed=False,
                          detail=f"collection-error: {collect_out[-1000:]}",
                          failure_kind=classify_gate_failure(collect_out),
                          duration_seconds=collect_duration)
    code, _output, run_duration = _run(test_cmd, cwd)
    duration = collect_duration + run_duration
    if code == 0:
        return GateResult(
            name="tdd-red", passed=False, detail="unexpectedly-green",
            duration_seconds=duration)
    # Honest red = test files collect and the suite does not pass. That is
    # the gate's whole contract (third dogfood iteration): the earlier
    # AssertionError whitelist rejected domain-exception reds, and the
    # failed-vs-ERROR distinction rejected legitimate breaking-change reds
    # where old fixtures raise at setup against the not-yet-written API.
    # Genuinely broken test code is still caught: syntax/import junk fails
    # the collection check above, and the build stage demands a fully green
    # suite the builder can only reach via honest code (or the bad-test
    # escape back to the test author).
    return GateResult(
        name="tdd-red", passed=True, detail="red-suite",
        duration_seconds=duration)
