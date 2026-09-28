# Phase 0a — Bench Machinery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give regie-bench the measurement machinery the redesign will be judged by: Régie's baseline mode, evaluator fingerprints, hidden-test evaluators, the per-commit acceptance trajectory, reference-solution validation, and an empirical-difficulty report.

**Architecture:** Régie gains one measurement-only flag (`regie run --baseline`: one developer, configured gates, no review). regie-bench gains a small `acceptance` module that owns how hidden acceptance runs (script evaluators, mirrored hidden tests, trajectories, fingerprints), a `process` module for the shared subprocess helper, and three CLI features (`--baseline`, `validate`, `difficulty`). Everything is standard library; results move to schema version 2.

**Tech Stack:** Python 3.12, Typer (Régie CLI), argparse (bench CLI), pytest, git worktrees.

**Spec:** `docs/superpowers/specs/2026-09-28-regie-agile-framework-design.md` (§6 Bench and calibration, §7 Baseline mode, §8 Phase 0a).

## Global Constraints

- Python `>=3.12` in both repositories.
- regie-bench keeps `dependencies = []`: standard library only at runtime.
- regie-bench ruff line length is 100 (`[tool.ruff] line-length = 100`).
- Task 1 runs in `/Users/frederic/Code/regie`; Tasks 2–9 run in `/Users/frederic/Code/regie-bench`.
- Commit on `main` in each repository; never push.
- Every commit message ends with:
  ```
  Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01RbtkGAHGVyLqrM25Tdxw2w
  ```
- Verification: Régie — `uv run pytest -q` and `uvx ruff check src tests`; regie-bench — `uv run pytest -q`, `uv run ruff check src tests`, and `uv run regie-bench smoke`.
- `result.json` becomes `schema_version: 2` in this phase.

## Review Focus

1. A hidden-test file whose path collides with a file the agent created must never overwrite or delete the agent's file; acceptance fails with a message naming the collision. (Task 4)
2. Temporary trajectory worktrees borrow installed dependencies (`node_modules`, `.venv`) by symlink; removing them must never damage the originals. (Task 6)
3. A run that halted before committing anything, or a state without `base_sha`, yields an empty trajectory and still writes `result.json`. (Task 6)
4. A `reference.patch` that does not apply is reported as a validation problem, not a crash. (Task 7)
5. Schema-v1 records (no fingerprints, no trajectory, no `stage_breakdown`) mixed into a label must not crash `difficulty` or `report`. (Tasks 3 and 8)

---

### Task 1: Régie baseline mode

**Files:**
- Modify: `src/regie/models.py` (`RunState`)
- Modify: `src/regie/cli.py` (`run` command)
- Modify: `src/regie/pipeline.py` (`run_task`, the build-stage `_pass_build`)
- Modify: `README.md` (command list)
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `regie run BRIEF --repo PATH --baseline` — routes direct, sets `RunState.baseline = True` and `RunState.workflow = "fast"`, finishes a task after its build gates pass with no review attempt. Rejects `--workflow` other than `auto` and `--tasks-file` with exit code 2.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_cli.py` (add `git` to the existing `from regie.gitops import ...` line):

```python
def _baseline_repo(fixture_repo) -> None:
    (fixture_repo / "regie.toml").write_text(
        'test_globs = ["tests/**"]\n'
        "[workflow]\nsubmit_pr = false\nreflection = false\nknowledge = false\n"
        '[commands]\ntest = "true"\nlint = "true"\n')
    (fixture_repo / ".fake_agent.json").write_text(json.dumps({
        "writes": {"src/extra.py": "EXTRA = 1\n"},
        "result": {"outcome": "done"}}))
    commit_all(fixture_repo, "chore: configure fake agent")
    git(fixture_repo, "push", "-q", "origin", "main")


def test_baseline_run_finishes_after_gates_without_review(
        regie_home, fixture_repo, remote_repo, fake_profiles, tmp_path):
    _baseline_repo(fixture_repo)
    brief = tmp_path / "schema-migration.md"
    # Auto-routing would send this brief to planning; baseline must not.
    brief.write_text("Add a database schema migration for extra values")

    result = runner.invoke(app, ["run", str(brief), "--repo", str(fixture_repo),
                                 "--profiles", str(fake_profiles), "--baseline"])

    assert result.exit_code == 0, result.output
    state = RunDir.open(regie_home, _last_run_id(regie_home)).read_state()
    assert state.stage == "done"
    assert state.baseline is True
    assert state.execution_route == "direct"
    assert state.planner_attempts == []
    assert [a.outcome for a in state.tasks["T1"].attempts["build"]] == ["done"]
    assert state.tasks["T1"].attempts["review"] == []


@pytest.mark.parametrize("extra", [["--workflow", "critical"], ["--tasks-file", "tasks.json"]])
def test_baseline_rejects_other_routing_options(regie_home, fixture_repo, fake_profiles,
                                                tmp_path, extra):
    _toml(fixture_repo)
    brief = tmp_path / "brief.md"
    brief.write_text("# Do the thing")
    (tmp_path / "tasks.json").write_text("[]")
    extra = [str(tmp_path / item) if item == "tasks.json" else item for item in extra]

    result = runner.invoke(app, ["run", str(brief), "--repo", str(fixture_repo),
                                 "--profiles", str(fake_profiles), "--baseline", *extra])

    assert result.exit_code == 2
    assert "--baseline cannot be combined" in result.output
    assert not (regie_home / "runs").exists()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -q tests/test_cli.py -k baseline`
Expected: FAIL — `No such option: --baseline`.

- [ ] **Step 3: Implement**

In `src/regie/models.py`, `RunState`, directly after `autonomous: bool = False`:

```python
    # Measurement-only mode: one developer, configured gates, no review.
    baseline: bool = False
```

In `src/regie/cli.py`, add the option as the last parameter of `run`:

```python
        parent: Annotated[str | None, typer.Option("--parent")] = None,
        baseline: Annotated[bool, typer.Option(
            "--baseline",
            help="Measurement only: one developer and the configured gates, "
                 "no planning and no review.")] = False):
```

Directly after the `if not brief.exists():` block:

```python
    if baseline and (workflow != "auto" or tasks_file is not None):
        typer.echo("--baseline cannot be combined with --workflow or --tasks-file",
                   err=True)
        raise typer.Exit(2)
```

In the `RunState(...)` construction, replace `workflow=workflow,` with:

```python
                     workflow="fast" if baseline else workflow, baseline=baseline,
```

Replace `route, reason = route_brief(brief_text, workflow, cfg)` with:

```python
        if baseline:
            route, reason = "direct", "baseline mode: one developer, gates only"
        else:
            route, reason = route_brief(brief_text, workflow, cfg)
```

In `src/regie/pipeline.py`, inside `run_task`, replace the body of `_pass_build`:

```python
            def _pass_build(pre=pre_dispatch):
                if set(changed_files(repo)) - pre:
                    commit_all(repo, f"feat({task_id}): {task.spec.title}")
                if run.baseline:
                    # Baseline measures what one developer achieves with the
                    # mechanical gates alone: no review stage.
                    task.status = "done"
                    return
                task.review_cycle_start = len(task.attempts["review"])
                task.stage = "review"
```

In `README.md`, in the "Run Régie" command block, after the `--workflow critical` line:

```
regie run brief.md --repo <path> --baseline     # measurement: one developer, gates, no review
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q tests/test_cli.py -k baseline`
Expected: 3 passed.

- [ ] **Step 5: Run the full verification**

Run: `uv run pytest -q && uvx ruff check src tests`
Expected: all tests pass; `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/regie/models.py src/regie/cli.py src/regie/pipeline.py README.md tests/test_cli.py
git commit -F - <<'EOF'
feat(cli): add baseline mode for empirical difficulty

One developer and the configured gates, no planning or review. It is the
reference regie-bench runs repeatedly to measure whether a task fits one
agent.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RbtkGAHGVyLqrM25Tdxw2w
EOF
```

---

### Task 2: Bench `--baseline` passthrough

**Files:**
- Modify: `src/regie_bench/cli.py` (`_invoke_regie`, `run_benchmark`, `_parser`, `main`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 1's `regie run ... --baseline`.
- Produces: `_invoke_regie(trial, regie_root, timeout_minutes, baseline: bool = False)`; `run_benchmark(..., baseline: bool = False)`; CLI `regie-bench run CASE --baseline`.

- [ ] **Step 1: Discard the spike's uncommitted `--workflow` passthrough**

Run: `git checkout -- src/regie_bench/cli.py && git status --short`
Expected: no output (clean tree). The spike code was labelled throwaway; `--baseline` replaces it.

- [ ] **Step 2: Write the failing test** — append to `tests/test_cli.py`:

```python
@pytest.mark.parametrize("baseline", [True, False])
def test_invoke_regie_passes_baseline_flag_only_when_asked(tmp_path, monkeypatch, baseline):
    captured = {}

    class Finished:
        pid = 1
        returncode = 0

        def communicate(self, timeout=None):
            return "ok", None

    def fake_popen(command, **kwargs):
        captured["command"] = command
        return Finished()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    _invoke_regie(tmp_path, tmp_path / "regie", 1, baseline=baseline)

    assert ("--baseline" in captured["command"]) is baseline
```

- [ ] **Step 3: Run it to verify it fails**

Run: `uv run pytest -q tests/test_cli.py -k passes_baseline`
Expected: FAIL — `TypeError: _invoke_regie() got an unexpected keyword argument 'baseline'`.

- [ ] **Step 4: Implement**

`_invoke_regie` signature and command:

```python
def _invoke_regie(trial: Path, regie_root: Path, timeout_minutes: int,
                  baseline: bool = False) -> tuple[int, float]:
    ...
    command = [
        "uv", "run", "--project", str(regie_root), "regie", "run",
        str(trial / "brief.md"), "--repo", str(repo),
        "--profiles", str(regie_root / "profiles"), "--autonomous",
    ]
    if baseline:
        command.append("--baseline")
```

`run_benchmark`:

```python
def run_benchmark(case: Case, results_root: Path, regie_root: Path, provider: str,
                  label: str, repeat: int, timeout_minutes: int,
                  baseline: bool = False) -> int:
    ...
        exit_code, elapsed = _invoke_regie(trial, regie_root, timeout_minutes, baseline)
```

`_parser`, after the `--timeout-minutes` argument:

```python
    run.add_argument("--baseline", action="store_true",
                     help="run Régie's baseline mode: one developer, gates only")
```

`main`, final call:

```python
    return run_benchmark(
        cases[args.case],
        args.results,
        args.regie_root.resolve(),
        args.provider,
        args.label,
        args.repeat,
        args.timeout_minutes,
        baseline=args.baseline,
    )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: all pass; `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/regie_bench/cli.py tests/test_cli.py
git commit -F - <<'EOF'
feat: pass Régie's baseline mode through run --baseline

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RbtkGAHGVyLqrM25Tdxw2w
EOF
```

---

### Task 3: Test bench root, fixture resolution, optional expected route

Cases must resolve fixtures from their own bench root (so tests can build throwaway benches), and the feature ladder has no route to expect.

**Files:**
- Create: `src/regie_bench/process.py`
- Create: `tests/minibench.py` (test utilities)
- Modify: `src/regie_bench/cli.py` (`Case`, `load_cases`, `prepare_case`, `evaluate_trial`, `_summary`, `report`, `main`'s `list` output; replace the local `_run`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `regie_bench.process.run(command, cwd, *, check=True, env=None) -> CompletedProcess[str]` (combined stdout/stderr as text).
- Produces: `Case.fixture_dir: Path`; `Case.expected_route: str | None`; `result["route_match"]: bool | None`; `_summary(...)["route_accuracy"]: float | None`.
- Produces (tests): `minibench.PYTHON`, `DONE_SCRIPT`, `HIDDEN_DONE_TEST`, `SCRIPT_EVALUATOR`, `HIDDEN_EVALUATOR`, `write(path, text)`, `build_bench(tmp_path) -> Path`, `add_case(root, case_id, manifest, files=None) -> Path`, `git(repo, *args) -> str`, `commit_value(repo, value, subject) -> str`, `write_state(trial, state, events=None)`, `make_patch(fixture, changes) -> str`.

- [ ] **Step 1: Create the subprocess helper** — `src/regie_bench/process.py`:

```python
from __future__ import annotations

import subprocess
from pathlib import Path


def run(command: list[str], cwd: Path, *, check: bool = True,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run a command with stdout and stderr captured together as text."""
    return subprocess.run(
        command,
        cwd=cwd,
        check=check,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
```

In `src/regie_bench/cli.py`, delete the local `def _run(...)` and add to the imports:

```python
from regie_bench.process import run as _run
```

- [ ] **Step 2: Create the test utilities** — `tests/minibench.py`:

```python
"""Test utilities: a throwaway bench root with one tiny fixture.

The `mini` fixture holds value.txt = "todo". Its public test only requires the
file to be non-empty; the evaluators below accept it only when it says "done".
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

PYTHON = sys.executable

DONE_SCRIPT = """
import os, pathlib, sys
repo = pathlib.Path(os.environ["TARGET_REPO"])
sys.exit(0 if (repo / "value.txt").read_text().strip() == "done" else 1)
"""

HIDDEN_DONE_TEST = """
import pathlib, sys
sys.exit(0 if pathlib.Path("value.txt").read_text().strip() == "done" else 1)
"""

SCRIPT_EVALUATOR = f'evaluator = "check.py"\nevaluator_command = ["{PYTHON}"]\n'

HIDDEN_EVALUATOR = (
    '[evaluator]\nkind = "hidden-tests"\nfiles = "hidden"\n'
    f'commands = ["{PYTHON} tests/_hidden/check_value.py"]\n'
)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip())
    return path


def build_bench(tmp_path: Path) -> Path:
    root = tmp_path / "bench"
    fixture = root / "fixtures" / "mini"
    write(fixture / "value.txt", "todo\n")
    write(fixture / "check_public.py", """
        import pathlib, sys
        sys.exit(0 if pathlib.Path("value.txt").read_text().strip() else 1)
    """)
    write(fixture / "regie.toml", f"""
        [providers]
        enabled = ["codex"]

        [commands]
        test = "{PYTHON} check_public.py"
        lint = "{PYTHON} -c pass"
    """)
    return root


def add_case(root: Path, case_id: str, manifest: str,
             files: dict[str, str] | None = None) -> Path:
    """Write cases/mini/<case_id>; `manifest` is appended to the required keys."""
    case_dir = root / "cases" / "mini" / case_id
    write(case_dir / "case.toml",
          f'id = "{case_id}"\nfixture = "mini"\ndescription = "test case"\n'
          f'expected_outcome = "completed"\n{manifest}')
    write(case_dir / "brief.md", "Set value.txt to done.\n")
    for relative, text in (files or {}).items():
        write(case_dir / relative, text)
    return case_dir


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, text=True,
                          capture_output=True).stdout.strip()


def commit_value(repo: Path, value: str, subject: str) -> str:
    (repo / "value.txt").write_text(value + "\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", subject)
    return git(repo, "rev-parse", "HEAD")


def write_state(trial: Path, state: dict, events: list[dict] | None = None) -> None:
    run_dir = trial / "regie-home" / "runs" / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(json.dumps(state))
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events or []))


def make_patch(fixture: Path, changes: dict[str, str]) -> str:
    """A `git diff` of `changes` applied to a scratch copy of `fixture`."""
    scratch = fixture.parents[2] / "patch-scratch"
    shutil.copytree(fixture, scratch)
    git(scratch, "init", "-q")
    git(scratch, "add", "-A")
    git(scratch, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    for relative, text in changes.items():
        (scratch / relative).write_text(text)
    diff = git(scratch, "diff")
    shutil.rmtree(scratch)
    return diff + "\n"
```

- [ ] **Step 3: Write the failing tests** — append to `tests/test_cli.py` (add `from minibench import DONE_SCRIPT, SCRIPT_EVALUATOR, add_case, build_bench, commit_value, git, write_state`):

```python
def test_prepare_case_copies_the_fixture_of_its_own_bench_root(tmp_path):
    root = build_bench(tmp_path)
    add_case(root, "mini-done", SCRIPT_EVALUATOR, {"check.py": DONE_SCRIPT})

    trial = prepare_case(load_cases(root)["mini-done"], tmp_path / "results", "codex",
                         trial_id="t1")

    assert (trial / "repo" / "value.txt").read_text() == "todo\n"


def test_case_without_expected_route_passes_on_outcome_alone(tmp_path):
    root = build_bench(tmp_path)
    add_case(root, "mini-done", SCRIPT_EVALUATOR, {"check.py": DONE_SCRIPT})
    case = load_cases(root)["mini-done"]
    trial = prepare_case(case, tmp_path / "results", "codex", trial_id="t1")
    repo = trial / "repo"
    base = git(repo, "rev-parse", "HEAD")
    commit_value(repo, "done", "feat(T1): done")
    write_state(trial, {"stage": "done", "execution_route": "planned",
                        "worktree_path": str(repo), "base_sha": base, "tasks": {}})

    result = evaluate_trial(case, trial, label="t", provider="codex",
                            regie_version="abc", exit_code=0, elapsed=1.0)

    assert case.expected_route is None
    assert result["route_match"] is None
    assert result["passed"] is True


def test_summary_route_accuracy_ignores_cases_without_expected_route():
    unrouted = {"passed": True, "route_match": None, "elapsed_seconds": 1.0,
                "attempts": 1, "fresh_tokens": 1, "cached_tokens": 1, "cost_usd": 0.0}
    routed = {**unrouted, "route_match": True}

    assert _summary([unrouted, routed])["route_accuracy"] == 1.0
    assert _summary([unrouted])["route_accuracy"] is None
```

- [ ] **Step 4: Run them to verify they fail**

Run: `uv run pytest -q tests/test_cli.py -k "own_bench_root or without_expected_route or ignores_cases"`
Expected: FAIL — `FileNotFoundError` (fixture copied from the real bench root), `KeyError: 'expected_route'`, and a `TypeError` summing `None`.

- [ ] **Step 5: Implement**

`Case`: add `fixture_dir: Path` after `fixture`, and change `expected_route: str` to `expected_route: str | None`.

`load_cases`, in the `Case(...)` construction:

```python
            fixture=raw["fixture"],
            fixture_dir=root / "fixtures" / raw["fixture"],
            description=raw["description"],
            expected_route=raw.get("expected_route"),
```

`prepare_case`: replace `shutil.copytree(ROOT / "fixtures" / case.fixture, repo)` with `shutil.copytree(case.fixture_dir, repo)`.

`evaluate_trial`: replace the `route_match = ...` line and the `"passed"` entry:

```python
    route_match = (None if case.expected_route is None
                   else state.get("execution_route") == case.expected_route)
    ...
        "passed": outcome_match and route_match is not False,
```

`_summary`: compute route accuracy only over records that expected a route:

```python
    routed = [record["route_match"] for record in records
              if record.get("route_match") is not None]
    return {
        "trials": len(records),
        "pass_rate": sum(record["passed"] for record in records) / len(records),
        "route_accuracy": sum(routed) / len(routed) if routed else None,
        ...
```

Add two formatting helpers above `report`, and use them in `report`:

```python
def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _delta_percent(before: float | None, after: float | None) -> str:
    return "n/a" if before is None or after is None else f"{after - before:+.0%}"
```

```python
            f'{summary["pass_rate"]:.0%} pass · {_percent(summary["route_accuracy"])} route · '
```

```python
                f'{_delta_percent(before["route_accuracy"], after["route_accuracy"])} route · '
```

`main`, `list` branch: print `f"{case.expected_route or '-':7} "` instead of `f"{case.expected_route:7} "`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest -q && uv run ruff check src tests && uv run regie-bench list`
Expected: all pass; `All checks passed!`; the case list prints unchanged.

- [ ] **Step 7: Commit**

```bash
git add src/regie_bench/process.py src/regie_bench/cli.py tests/minibench.py tests/test_cli.py
git commit -F - <<'EOF'
refactor: resolve fixtures per bench root; make expected route optional

Throwaway bench roots make evaluator behavior testable, and ladder cases
judge the outcome without prescribing a route.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RbtkGAHGVyLqrM25Tdxw2w
EOF
```

---

### Task 4: Evaluator kinds and `run_acceptance`

Ladder cases need hidden tests that run inside the repository (pytest, vitest) rather than one script outside it.

**Files:**
- Create: `src/regie_bench/acceptance.py`
- Modify: `src/regie_bench/cli.py` (`Case.evaluator`, `load_cases`, `evaluate_trial`; delete `_acceptance`)
- Modify: `README.md` (case manifest documentation)
- Test: `tests/test_acceptance.py` (create), `tests/test_cli.py` (update two existing tests)

**Interfaces:**
- Consumes: `regie_bench.process.run`.
- Produces: `Evaluator(kind, path, command=(), commands=(), shared_paths=())`; `load_evaluator(raw: dict, case_dir: Path) -> Evaluator | None`; `payload_files(directory: Path) -> list[Path]`; `run_acceptance(evaluator: Evaluator | None, repo: Path) -> tuple[bool | None, str]`. `Case.evaluator: Evaluator | None` replaces `Case.evaluator: Path | None` and `Case.evaluator_command`.

Manifest formats:

```toml
# script (existing cases, unchanged)
evaluator = "acceptance.test.mjs"
evaluator_command = ["node", "--test"]   # optional, this is the default

# hidden tests (new)
[evaluator]
kind = "hidden-tests"
files = "hidden"                         # mirrors repository paths
commands = ["uv run --project backend pytest -q backend/tests/_hidden"]
shared_paths = ["backend/.venv", "frontend/node_modules"]   # optional
```

- [ ] **Step 1: Write the failing tests** — create `tests/test_acceptance.py`:

```python
from __future__ import annotations

import pytest
from minibench import (
    DONE_SCRIPT,
    HIDDEN_DONE_TEST,
    HIDDEN_EVALUATOR,
    SCRIPT_EVALUATOR,
    add_case,
    build_bench,
    git,
)

from regie_bench.acceptance import run_acceptance
from regie_bench.cli import load_cases, prepare_case

HIDDEN_FILES = {"hidden/tests/_hidden/check_value.py": HIDDEN_DONE_TEST}


def _prepared_repo(tmp_path, manifest, files):
    root = build_bench(tmp_path)
    add_case(root, "mini", manifest, files)
    case = load_cases(root)["mini"]
    return case, prepare_case(case, tmp_path / "results", "codex", trial_id="t1") / "repo"


@pytest.mark.parametrize("manifest,files", [
    (SCRIPT_EVALUATOR, {"check.py": DONE_SCRIPT}),
    (HIDDEN_EVALUATOR, HIDDEN_FILES),
], ids=["script", "hidden-tests"])
def test_acceptance_rejects_the_fixture_and_accepts_the_change(tmp_path, manifest, files):
    case, repo = _prepared_repo(tmp_path, manifest, files)

    assert run_acceptance(case.evaluator, repo)[0] is False
    (repo / "value.txt").write_text("done\n")
    assert run_acceptance(case.evaluator, repo)[0] is True


def test_hidden_tests_leave_no_trace_in_the_repository(tmp_path):
    case, repo = _prepared_repo(tmp_path, HIDDEN_EVALUATOR, HIDDEN_FILES)

    run_acceptance(case.evaluator, repo)

    assert not (repo / "tests").exists()
    assert git(repo, "status", "--porcelain") == ""


def test_hidden_tests_never_overwrite_a_file_the_agent_wrote(tmp_path):
    case, repo = _prepared_repo(tmp_path, HIDDEN_EVALUATOR, HIDDEN_FILES)
    agent_file = repo / "tests" / "_hidden" / "check_value.py"
    agent_file.parent.mkdir(parents=True)
    agent_file.write_text("print('agent')\n")

    passed, log = run_acceptance(case.evaluator, repo)

    assert passed is False
    assert "collides" in log and "tests/_hidden/check_value.py" in log
    assert agent_file.read_text() == "print('agent')\n"


def test_case_without_evaluator_has_no_verdict(tmp_path):
    case, repo = _prepared_repo(tmp_path, "", None)

    assert run_acceptance(case.evaluator, repo) == (
        None, "no acceptance evaluator for this outcome")


def test_unknown_evaluator_kind_is_rejected(tmp_path):
    root = build_bench(tmp_path)
    add_case(root, "mini", '[evaluator]\nkind = "magic"\nfiles = "x"\ncommands = []\n')

    with pytest.raises(ValueError, match="unknown evaluator kind"):
        load_cases(root)
```

In `tests/test_cli.py`, update two existing tests:

```python
    assert cases["strict-quantity"].evaluator.command == ("python",)
```

```python
def test_completed_case_evaluators_reject_the_unchanged_fixture(tmp_path):
    completed = [
        case for case in load_cases().values()
        if case.expected_outcome == "completed"
    ]

    for case in completed:
        trial = prepare_case(case, tmp_path, "codex", trial_id=f"trial-{case.id}")
        passed, _ = run_acceptance(case.evaluator, trial / "repo")
        assert passed is False, f"{case.id} evaluator accepts the baseline"
```

(Add `from regie_bench.acceptance import run_acceptance` to its imports.)

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest -q tests/test_acceptance.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'regie_bench.acceptance'`.

- [ ] **Step 3: Implement** — create `src/regie_bench/acceptance.py`:

```python
"""Hidden acceptance: how a case's evaluator judges a repository."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from regie_bench.process import run


@dataclass(frozen=True)
class Evaluator:
    """`script`: `command` runs one file from outside the repository with
    TARGET_REPO set. `hidden-tests`: the files under `path` are mirrored into
    the repository while `commands` run there, then removed. `shared_paths`
    are untracked directories (installed dependencies) that temporary
    worktrees borrow from the trial worktree."""

    kind: str
    path: Path
    command: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    shared_paths: tuple[str, ...] = ()


def load_evaluator(raw: dict[str, Any], case_dir: Path) -> Evaluator | None:
    table = raw.get("evaluator")
    if isinstance(table, dict):
        if table.get("kind") != "hidden-tests":
            raise ValueError(
                f"{case_dir.name}: unknown evaluator kind {table.get('kind')!r}")
        return Evaluator(
            kind="hidden-tests",
            path=case_dir / table["files"],
            commands=tuple(table["commands"]),
            shared_paths=tuple(table.get("shared_paths", [])),
        )
    if table:
        return Evaluator(
            kind="script",
            path=case_dir / table,
            command=tuple(raw.get("evaluator_command", ["node", "--test"])),
            shared_paths=tuple(raw.get("shared_paths", [])),
        )
    return None


def payload_files(directory: Path) -> list[Path]:
    """Hidden-test files, ignoring interpreter caches."""
    return sorted(
        path for path in directory.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )


def _mirror(source: Path, destination: Path) -> tuple[list[Path], list[Path]]:
    targets = [(path, destination / path.relative_to(source))
               for path in payload_files(source)]
    collisions = [target.relative_to(destination).as_posix()
                  for _, target in targets if target.exists()]
    if collisions:
        raise FileExistsError(", ".join(collisions))
    created_files: list[Path] = []
    created_dirs: list[Path] = []
    for path, target in targets:
        missing: list[Path] = []
        parent = target.parent
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        for directory in reversed(missing):
            directory.mkdir()
            created_dirs.append(directory)
        shutil.copy2(path, target)
        created_files.append(target)
    return created_files, created_dirs


def _unmirror(created_files: list[Path], created_dirs: list[Path]) -> None:
    for path in created_files:
        path.unlink(missing_ok=True)
    for directory in sorted(created_dirs, key=lambda item: len(item.parts), reverse=True):
        shutil.rmtree(directory, ignore_errors=True)


def run_acceptance(evaluator: Evaluator | None, repo: Path) -> tuple[bool | None, str]:
    """Judge `repo` with a case's hidden acceptance: (passed, log). `passed`
    is None when the case has no evaluator."""
    if evaluator is None:
        return None, "no acceptance evaluator for this outcome"
    env = {**os.environ, "TARGET_REPO": str(repo), "PYTHONDONTWRITEBYTECODE": "1"}
    if evaluator.kind == "script":
        result = run([*evaluator.command, str(evaluator.path)], repo, check=False, env=env)
        return result.returncode == 0, result.stdout
    try:
        created = _mirror(evaluator.path, repo)
    except FileExistsError as exc:
        return False, f"hidden test path collides with a repository file: {exc}"
    try:
        passed = True
        logs = []
        for command in evaluator.commands:
            result = run(["/bin/sh", "-c", command], repo, check=False, env=env)
            logs.append(f"$ {command}\n{result.stdout}")
            passed = passed and result.returncode == 0
        return passed, "\n".join(logs)
    finally:
        _unmirror(*created)
```

In `src/regie_bench/cli.py`:
- import `from regie_bench.acceptance import Evaluator, load_evaluator, run_acceptance`;
- `Case`: replace `evaluator: Path | None` and `evaluator_command: tuple[str, ...]` with `evaluator: Evaluator | None`;
- `load_cases`: delete the local `evaluator = ...` line and pass `evaluator=load_evaluator(raw, case_dir)` (drop `evaluator_command=`);
- `evaluate_trial`: replace `acceptance_passed, _ = _acceptance(case, worktree, trial)` with

```python
    acceptance_passed, acceptance_log = run_acceptance(case.evaluator, worktree)
    (trial / "acceptance.log").write_text(acceptance_log)
```

- delete `_acceptance`.

In `README.md`, add a section after "What counts as a pass":

````markdown
## Case manifests

A case's hidden acceptance is either one script run from outside the repository
(`evaluator = "acceptance.test.mjs"`, optional `evaluator_command`), or hidden
tests mirrored into the repository while their commands run:

```toml
[evaluator]
kind = "hidden-tests"
files = "hidden"          # a directory mirroring repository paths
commands = ["uv run --project backend pytest -q backend/tests/_hidden"]
shared_paths = ["backend/.venv", "frontend/node_modules"]
```

Hidden files never overwrite repository files; a collision fails acceptance.
`expected_route` is optional; without it a case is judged on its outcome alone.
````

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q && uv run ruff check src tests && uv run regie-bench smoke`
Expected: all pass; `All checks passed!`; every smoke case `PASS`.

- [ ] **Step 5: Commit**

```bash
git add src/regie_bench/acceptance.py src/regie_bench/cli.py README.md tests/test_acceptance.py tests/test_cli.py
git commit -F - <<'EOF'
feat: hidden-test evaluators mirrored into the repository

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RbtkGAHGVyLqrM25Tdxw2w
EOF
```

---

### Task 5: Fingerprints, mode, and result schema 2

Results are comparable only when judged by the same brief and hidden acceptance (`78c431f` silently broke that).

**Files:**
- Modify: `src/regie_bench/acceptance.py` (add `fingerprint`)
- Modify: `src/regie_bench/cli.py` (`evaluate_trial`, `run_benchmark`)
- Modify: `docs/METHODOLOGY.md`
- Test: `tests/test_acceptance.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `Evaluator`, `payload_files`.
- Produces: `fingerprint(brief: Path, evaluator: Evaluator | None) -> {"brief_sha256": str, "evaluator_sha256": str | None}`; `evaluate_trial(..., mode: str = "regie")`; result keys `schema_version == 2`, `mode`, `brief_sha256`, `evaluator_sha256`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_acceptance.py` (add `import hashlib` and `from regie_bench.acceptance import fingerprint`):

```python
def test_fingerprint_changes_exactly_when_the_hidden_acceptance_changes(tmp_path):
    root = build_bench(tmp_path)
    case_dir = add_case(root, "mini", HIDDEN_EVALUATOR, HIDDEN_FILES)
    case = load_cases(root)["mini"]
    first = fingerprint(case.brief, case.evaluator)

    assert fingerprint(case.brief, case.evaluator) == first
    assert first["brief_sha256"] == hashlib.sha256(b"Set value.txt to done.\n").hexdigest()

    (case_dir / "hidden/tests/_hidden/check_value.py").write_text("import sys\nsys.exit(1)\n")
    changed = fingerprint(case.brief, case.evaluator)

    assert changed["evaluator_sha256"] != first["evaluator_sha256"]
    assert changed["brief_sha256"] == first["brief_sha256"]


def test_fingerprint_ignores_interpreter_caches(tmp_path):
    root = build_bench(tmp_path)
    case_dir = add_case(root, "mini", HIDDEN_EVALUATOR, HIDDEN_FILES)
    case = load_cases(root)["mini"]
    before = fingerprint(case.brief, case.evaluator)
    cache = case_dir / "hidden/tests/_hidden/__pycache__/check_value.cpython-312.pyc"
    cache.parent.mkdir()
    cache.write_bytes(b"\x00compiled")

    assert fingerprint(case.brief, case.evaluator) == before
```

Append to `tests/test_cli.py` (add `from regie_bench.acceptance import fingerprint`):

```python
def test_result_records_mode_and_fingerprints(tmp_path):
    root = build_bench(tmp_path)
    add_case(root, "mini-done", SCRIPT_EVALUATOR, {"check.py": DONE_SCRIPT})
    case = load_cases(root)["mini-done"]
    trial = prepare_case(case, tmp_path / "results", "codex", trial_id="t1")
    write_state(trial, {"stage": "halted", "tasks": {}})

    result = evaluate_trial(case, trial, label="t", provider="codex", regie_version="abc",
                            exit_code=1, elapsed=1.0, mode="baseline")

    assert result["schema_version"] == 2
    assert result["mode"] == "baseline"
    assert {key: result[key] for key in ("brief_sha256", "evaluator_sha256")} == (
        fingerprint(case.brief, case.evaluator))
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest -q tests/test_acceptance.py tests/test_cli.py -k "fingerprint"`
Expected: FAIL — `ImportError: cannot import name 'fingerprint'`.

- [ ] **Step 3: Implement** — in `src/regie_bench/acceptance.py` (add `import hashlib`):

```python
def _digest(files: list[Path], base: Path, header: str) -> str:
    digest = hashlib.sha256(header.encode())
    for path in files:
        digest.update(b"\0" + path.relative_to(base).as_posix().encode() + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def fingerprint(brief: Path, evaluator: Evaluator | None) -> dict[str, str | None]:
    """Content hashes: two results are comparable only when the brief and the
    hidden acceptance that judged them are identical."""
    if evaluator is None:
        evaluator_sha = None
    elif evaluator.kind == "script":
        evaluator_sha = _digest([evaluator.path], evaluator.path.parent,
                                "\0".join(evaluator.command))
    else:
        evaluator_sha = _digest(payload_files(evaluator.path), evaluator.path,
                                "\0".join(evaluator.commands))
    return {
        "brief_sha256": hashlib.sha256(brief.read_bytes()).hexdigest(),
        "evaluator_sha256": evaluator_sha,
    }
```

In `src/regie_bench/cli.py`:
- import `fingerprint` from `regie_bench.acceptance`;
- `evaluate_trial` signature gains `mode: str = "regie"` after `elapsed: float`;
- in the `result` dict: `"schema_version": 2,` and, after `"provider": provider,`:

```python
        "mode": mode,
        **fingerprint(case.brief, case.evaluator),
```

- `run_benchmark`: pass `mode="baseline" if baseline else "regie"` to `evaluate_trial`.

In `docs/METHODOLOGY.md`, add to the "Record these variables" list:

```markdown
- `brief_sha256` and `evaluator_sha256`: compare results only when both match;
- `mode`: `regie` or `baseline` (one developer, gates only).
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: all pass; `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add src/regie_bench/acceptance.py src/regie_bench/cli.py docs/METHODOLOGY.md tests/test_acceptance.py tests/test_cli.py
git commit -F - <<'EOF'
feat: fingerprint briefs and evaluators; record mode (schema 2)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RbtkGAHGVyLqrM25Tdxw2w
EOF
```

---

### Task 6: Acceptance trajectory

Shows when the change first became acceptable and how many commits came after that point, which is what separated useful review from churn in the spike.

**Files:**
- Modify: `src/regie_bench/acceptance.py` (add `acceptance_trajectory`, `trajectory_summary`)
- Modify: `src/regie_bench/cli.py` (`evaluate_trial`)
- Modify: `docs/METHODOLOGY.md`
- Test: `tests/test_acceptance.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `Evaluator`, `run_acceptance`, `regie_bench.process.run`.
- Produces: `acceptance_trajectory(evaluator, repo: Path, base_sha: str) -> list[{"sha": str, "subject": str, "accepted": bool | None}]` (oldest first); `trajectory_summary(rows) -> {"first_accepting_commit": int | None, "commits_after_first_accept": int | None}`; result keys `acceptance_trajectory`, `first_accepting_commit`, `commits_after_first_accept`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_acceptance.py` (add `PYTHON, commit_value` to the `minibench` import and `acceptance_trajectory, trajectory_summary` to the `regie_bench.acceptance` import):

```python
def test_trajectory_shows_when_the_change_first_became_acceptable(tmp_path):
    case, repo = _prepared_repo(tmp_path, SCRIPT_EVALUATOR, {"check.py": DONE_SCRIPT})
    base = git(repo, "rev-parse", "HEAD")
    commit_value(repo, "wip", "feat(T1): café, first try")
    commit_value(repo, "done", "feat(T1): fix after review")
    (repo / "notes.md").write_text("notes\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "docs: notes")

    rows = acceptance_trajectory(case.evaluator, repo, base)

    assert [(row["subject"], row["accepted"]) for row in rows] == [
        ("feat(T1): café, first try", False),
        ("feat(T1): fix after review", True),
        ("docs: notes", True),
    ]
    assert trajectory_summary(rows) == {
        "first_accepting_commit": 1, "commits_after_first_accept": 1}
    assert len(git(repo, "worktree", "list").splitlines()) == 1


def test_trajectory_is_empty_without_commits_or_base(tmp_path):
    case, repo = _prepared_repo(tmp_path, SCRIPT_EVALUATOR, {"check.py": DONE_SCRIPT})
    base = git(repo, "rev-parse", "HEAD")

    assert acceptance_trajectory(case.evaluator, repo, base) == []
    assert acceptance_trajectory(case.evaluator, repo, "") == []
    assert trajectory_summary([]) == {
        "first_accepting_commit": None, "commits_after_first_accept": None}


def test_trajectory_worktrees_borrow_dependencies_without_harming_them(tmp_path):
    manifest = ('[evaluator]\nkind = "hidden-tests"\nfiles = "hidden"\n'
                'shared_paths = ["deps"]\n'
                f'commands = ["{PYTHON} tests/_hidden/check_deps.py"]\n')
    check = """
        import pathlib, sys
        installed = pathlib.Path("deps/marker").exists()
        done = pathlib.Path("value.txt").read_text().strip() == "done"
        sys.exit(0 if installed and done else 1)
    """
    case, repo = _prepared_repo(tmp_path, manifest,
                                {"hidden/tests/_hidden/check_deps.py": check})
    base = git(repo, "rev-parse", "HEAD")
    commit_value(repo, "done", "feat(T1): done")
    (repo / "deps").mkdir()
    (repo / "deps" / "marker").write_text("installed\n")  # untracked, like node_modules

    rows = acceptance_trajectory(case.evaluator, repo, base)

    assert [row["accepted"] for row in rows] == [True]
    assert (repo / "deps" / "marker").read_text() == "installed\n"
```

Append to `tests/test_cli.py`:

```python
def test_result_records_the_acceptance_trajectory(tmp_path):
    root = build_bench(tmp_path)
    add_case(root, "mini-done", SCRIPT_EVALUATOR, {"check.py": DONE_SCRIPT})
    case = load_cases(root)["mini-done"]
    trial = prepare_case(case, tmp_path / "results", "codex", trial_id="t1")
    repo = trial / "repo"
    base = git(repo, "rev-parse", "HEAD")
    commit_value(repo, "wip", "feat(T1): first")
    commit_value(repo, "done", "feat(T1): second")
    write_state(trial, {"stage": "done", "worktree_path": str(repo),
                        "base_sha": base, "tasks": {}})

    result = evaluate_trial(case, trial, label="t", provider="codex",
                            regie_version="abc", exit_code=0, elapsed=1.0)

    assert [row["accepted"] for row in result["acceptance_trajectory"]] == [False, True]
    assert result["first_accepting_commit"] == 1
    assert result["commits_after_first_accept"] == 0
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest -q tests/test_acceptance.py tests/test_cli.py -k trajectory`
Expected: FAIL — `ImportError: cannot import name 'acceptance_trajectory'`.

- [ ] **Step 3: Implement** — in `src/regie_bench/acceptance.py` (add `import tempfile`):

```python
def acceptance_trajectory(evaluator: Evaluator | None, repo: Path,
                          base_sha: str) -> list[dict[str, Any]]:
    """Hidden acceptance of every commit after `base_sha`, oldest first. Each
    commit is judged in a temporary worktree, so `repo` is never touched."""
    if evaluator is None or not base_sha:
        return []
    log = run(["git", "log", "--reverse", "--format=%H%x09%s", f"{base_sha}..HEAD"],
              repo, check=False)
    if log.returncode:
        return []
    rows: list[dict[str, Any]] = []
    for line in log.stdout.splitlines():
        sha, _, subject = line.partition("\t")
        with tempfile.TemporaryDirectory(prefix="regie-bench-trajectory-") as scratch:
            target = Path(scratch) / "worktree"
            run(["git", "worktree", "add", "--detach", "-q", str(target), sha], repo)
            try:
                for relative in evaluator.shared_paths:
                    source, link = repo / relative, target / relative
                    if source.exists() and not link.exists():
                        link.parent.mkdir(parents=True, exist_ok=True)
                        link.symlink_to(source.resolve(),
                                        target_is_directory=source.is_dir())
                accepted, _ = run_acceptance(evaluator, target)
            finally:
                # Removal unlinks borrowed symlinks without following them.
                run(["git", "worktree", "remove", "--force", str(target)], repo,
                    check=False)
        rows.append({"sha": sha, "subject": subject, "accepted": accepted})
    return rows


def trajectory_summary(rows: list[dict[str, Any]]) -> dict[str, int | None]:
    first = next((index for index, row in enumerate(rows) if row["accepted"]), None)
    return {
        "first_accepting_commit": first,
        "commits_after_first_accept": None if first is None else len(rows) - 1 - first,
    }
```

In `src/regie_bench/cli.py`, import both functions and, in `evaluate_trial` after the acceptance log is written:

```python
    trajectory = (acceptance_trajectory(case.evaluator, worktree, state.get("base_sha", ""))
                  if worktree.is_dir() else [])
```

and in the `result` dict, after `"acceptance_passed": acceptance_passed,`:

```python
        "acceptance_trajectory": trajectory,
        **trajectory_summary(trajectory),
```

In `docs/METHODOLOGY.md`, under "Acceptance layers", add:

```markdown
Every trial also records its **acceptance trajectory**: hidden acceptance of each
commit Régie made, judged in a temporary worktree. `first_accepting_commit` shows
when the change first became acceptable; `commits_after_first_accept` counts the
revisions that followed. A revision after the first acceptable commit did not
change the measured outcome; whether it fixed something the evaluator does not
encode must be read from the trial's findings.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: all pass; `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add src/regie_bench/acceptance.py src/regie_bench/cli.py docs/METHODOLOGY.md tests/test_acceptance.py tests/test_cli.py
git commit -F - <<'EOF'
feat: record the per-commit acceptance trajectory

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RbtkGAHGVyLqrM25Tdxw2w
EOF
```

---

### Task 7: Reference-solution validation

A case is sound only if its hidden tests fail on the fixture and pass on a reference solution that also passes the public gates.

**Files:**
- Modify: `src/regie_bench/cli.py` (`Case.reference`, `load_cases`, `validate_case`, `validate`, `_parser`, `main`)
- Modify: `.github/workflows/ci.yml`, `README.md`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `prepare_case`, `run_acceptance`.
- Produces: `Case.reference: Path | None` (`reference.patch` in the case directory); `validate_case(case, results_root) -> list[str]` (empty when valid); `validate(cases, results_root, names) -> int`; CLI `regie-bench validate [CASE ...]`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_cli.py` (add `make_patch` to the `minibench` import, `validate, validate_case` to the `regie_bench.cli` import, and `from pathlib import Path`):

```python
def _reference_case(tmp_path, changes, evaluator_script=DONE_SCRIPT):
    root = build_bench(tmp_path)
    case_dir = add_case(root, "mini", SCRIPT_EVALUATOR, {"check.py": evaluator_script})
    (case_dir / "reference.patch").write_text(make_patch(root / "fixtures" / "mini", changes))
    return load_cases(root)["mini"]


def test_validate_accepts_a_sound_case(tmp_path):
    case = _reference_case(tmp_path, {"value.txt": "done\n"})

    assert validate_case(case, tmp_path / "results") == []


def test_validate_flags_hidden_acceptance_the_fixture_already_passes(tmp_path):
    lenient = DONE_SCRIPT.replace('== "done"', 'in {"todo", "done"}')
    case = _reference_case(tmp_path, {"value.txt": "done\n"}, lenient)

    assert validate_case(case, tmp_path / "results") == [
        "hidden acceptance passes on the unchanged fixture"]


def test_validate_flags_a_reference_that_breaks_public_tests(tmp_path):
    case = _reference_case(tmp_path, {"value.txt": "\n"})

    assert validate_case(case, tmp_path / "results") == [
        "public test fails on the reference solution",
        "hidden acceptance fails on the reference solution",
    ]


def test_validate_reports_a_patch_that_does_not_apply(tmp_path):
    case = _reference_case(tmp_path, {"value.txt": "done\n"})
    case.reference.write_text(
        "diff --git a/missing.txt b/missing.txt\n--- a/missing.txt\n+++ b/missing.txt\n"
        "@@ -1 +1 @@\n-old\n+new\n")

    problems = validate_case(case, tmp_path / "results")

    assert len(problems) == 1
    assert problems[0].startswith("reference.patch does not apply")


def test_validate_with_no_reference_cases_is_a_no_op(capsys):
    assert validate(load_cases(), Path("/nonexistent"), []) == 0
    assert "no cases with reference solutions" in capsys.readouterr().out
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest -q tests/test_cli.py -k validate`
Expected: FAIL — `ImportError: cannot import name 'validate'`.

- [ ] **Step 3: Implement** — in `src/regie_bench/cli.py`:

`Case`: add `reference: Path | None` after `overlay`. In `load_cases`, next to the overlay:

```python
        reference = case_dir / "reference.patch"
        ...
            reference=reference if reference.is_file() else None,
```

New functions (after `evaluate_trial`):

```python
def validate_case(case: Case, results_root: Path) -> list[str]:
    """Problems that make a case unsound; an empty list means it is valid."""
    if case.reference is None:
        return ["no reference.patch"]
    if case.evaluator is None:
        return ["no hidden acceptance evaluator"]
    trial = prepare_case(case, results_root, "codex")
    repo = trial / "repo"
    commands = tomllib.loads((repo / "regie.toml").read_text()).get("commands", {})

    def gate(name: str) -> bool:
        result = _run(["/bin/sh", "-c", commands[name]], repo, check=False)
        (trial / f"validate-{name}.log").write_text(result.stdout)
        return result.returncode == 0

    if "setup" in commands and not gate("setup"):
        return ["fixture setup fails"]
    problems: list[str] = []
    if run_acceptance(case.evaluator, repo)[0]:
        problems.append("hidden acceptance passes on the unchanged fixture")
    applied = _run(["git", "apply", "--whitespace=nowarn", str(case.reference)],
                   repo, check=False)
    if applied.returncode:
        return [*problems, f"reference.patch does not apply: {applied.stdout.strip()}"]
    for name in ("build", "test", "lint"):
        if name in commands and not gate(name):
            problems.append(f"public {name} fails on the reference solution")
    if not run_acceptance(case.evaluator, repo)[0]:
        problems.append("hidden acceptance fails on the reference solution")
    return problems


def validate(cases: dict[str, Case], results_root: Path, names: list[str]) -> int:
    unknown = [name for name in names if name not in cases]
    if unknown:
        raise SystemExit(f"unknown case: {', '.join(unknown)}")
    selected = ([cases[name] for name in names] if names
                else [case for case in cases.values() if case.reference])
    if not selected:
        print("no cases with reference solutions")
        return 0
    failures = 0
    for case in selected:
        problems = validate_case(case, results_root)
        print(f'{"FAIL" if problems else "PASS":4} {case.id}')
        for problem in problems:
            print(f"      {problem}")
        failures += bool(problems)
    return 1 if failures else 0
```

`_parser`, after the `smoke` parser:

```python
    validate_parser = subparsers.add_parser(
        "validate", help="check hidden tests against each case's reference solution")
    validate_parser.add_argument("cases", nargs="*")
```

`main`, after the `smoke` branch:

```python
    if args.command == "validate":
        return validate(cases, args.results, args.cases)
```

In `.github/workflows/ci.yml`, after `- run: uv run regie-bench smoke`:

```yaml
      - run: uv run regie-bench validate
```

In `README.md`, under "Case manifests", add:

```markdown
A case may ship `reference.patch`, a solution the case author validated.
`uv run regie-bench validate` proves every such case is sound: hidden acceptance
fails on the fixture, the patch applies, the public build/test/lint gates pass on
it, and hidden acceptance passes on it.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q && uv run ruff check src tests && uv run regie-bench validate`
Expected: all pass; `All checks passed!`; `no cases with reference solutions`.

- [ ] **Step 5: Commit**

```bash
git add src/regie_bench/cli.py .github/workflows/ci.yml README.md tests/test_cli.py
git commit -F - <<'EOF'
feat: validate cases against reference solutions

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RbtkGAHGVyLqrM25Tdxw2w
EOF
```

---

### Task 8: Empirical difficulty report

**Files:**
- Modify: `src/regie_bench/cli.py` (`case_difficulty`, `difficulty`, `_parser`, `main`)
- Modify: `docs/METHODOLOGY.md`, `README.md`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `_results(results_root, label)`.
- Produces: `case_difficulty(records) -> {"trials", "success_rate", "median_seconds", "median_fresh_tokens", "median_revisions", "class"}` with `class` in `fits` (≥ 2/3 solved), `borderline` (≥ 1/3), `exceeds`; CLI `regie-bench difficulty --label LABEL`.

A trial is **solved** when it reached `done` and passed hidden acceptance. **Revisions** are build dispatches after the first, excluding quota skips.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_cli.py` (add `case_difficulty` to the `regie_bench.cli` import):

```python
def _trial(solved: bool, builds: int) -> dict:
    return {
        "case_id": "c", "stage": "done" if solved else "halted",
        "acceptance_passed": solved, "elapsed_seconds": 100.0, "fresh_tokens": 1000,
        "stage_breakdown": [{"component": "agent", "stage": "build", "outcome": "done"}
                            for _ in range(builds)],
    }


@pytest.mark.parametrize("solved,expected", [
    ((True, True, False), "fits"),
    ((True, False, False), "borderline"),
    ((False, False, False), "exceeds"),
])
def test_difficulty_class_follows_baseline_success_rate(solved, expected):
    assert case_difficulty([_trial(item, 1) for item in solved])["class"] == expected


def test_difficulty_counts_build_retries_as_revisions_ignoring_quota_skips():
    records = [_trial(True, 1), _trial(True, 3), _trial(False, 2)]
    records[1]["stage_breakdown"].append(
        {"component": "agent", "stage": "build", "outcome": "quota"})

    stats = case_difficulty(records)

    assert stats["median_revisions"] == 1  # revisions 0, 2, 1
    assert stats["success_rate"] == pytest.approx(2 / 3)


def test_difficulty_tolerates_schema_v1_records():
    legacy = {"case_id": "c", "stage": "done", "acceptance_passed": True,
              "elapsed_seconds": 5.0, "fresh_tokens": 10}

    assert case_difficulty([legacy])["median_revisions"] == 0
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest -q tests/test_cli.py -k difficulty`
Expected: FAIL — `ImportError: cannot import name 'case_difficulty'`.

- [ ] **Step 3: Implement** — in `src/regie_bench/cli.py` (add `from fractions import Fraction`):

```python
DIFFICULTY_CLASSES = (("fits", Fraction(2, 3)), ("borderline", Fraction(1, 3)),
                      ("exceeds", Fraction(0)))


def case_difficulty(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Empirical difficulty of one case from repeated baseline trials."""
    solved = sum(record.get("stage") == "done" and record.get("acceptance_passed") is True
                 for record in records)
    rate = Fraction(solved, len(records))
    revisions = [
        max(0, sum(1 for row in record.get("stage_breakdown", [])
                   if row.get("component") == "agent" and row.get("stage") == "build"
                   and row.get("outcome") != "quota") - 1)
        for record in records
    ]
    return {
        "trials": len(records),
        "success_rate": float(rate),
        "median_seconds": statistics.median(r.get("elapsed_seconds", 0.0) for r in records),
        "median_fresh_tokens": statistics.median(r.get("fresh_tokens", 0) for r in records),
        "median_revisions": statistics.median(revisions),
        "class": next(name for name, floor in DIFFICULTY_CLASSES if rate >= floor),
    }


def difficulty(results_root: Path, label: str) -> int:
    records = _results(results_root, label)
    if not records:
        print(f"{label}: no trials")
        return 1
    by_case: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_case.setdefault(record["case_id"], []).append(record)
    print(f'{"case":28} {"trials":>6} {"solved":>7} {"median s":>9} '
          f'{"fresh tok":>10} {"revisions":>9}  class')
    for case_id, rows in sorted(by_case.items()):
        stats = case_difficulty(rows)
        print(f'{case_id:28} {stats["trials"]:6d} {stats["success_rate"]:7.0%} '
              f'{stats["median_seconds"]:9.1f} {stats["median_fresh_tokens"]:10,.0f} '
              f'{stats["median_revisions"]:9.1f}  {stats["class"]}')
    return 0
```

`_parser`, after the `report` parser:

```python
    difficulty_parser = subparsers.add_parser(
        "difficulty", help="empirical difficulty per case from repeated baseline trials")
    difficulty_parser.add_argument("--label", required=True)
```

`main`, after the `report` branch:

```python
    if args.command == "difficulty":
        return difficulty(args.results, args.label)
```

In `docs/METHODOLOGY.md`, add a section before "Anti-overfitting policy":

```markdown
## Empirical difficulty

A case's difficulty is measured, not labelled: run it in Régie's baseline mode
(one developer, gates only) at least three times, then
`uv run regie-bench difficulty --label <label>`. A trial is solved when it reaches
`done` and passes hidden acceptance. Cases solved in at least two thirds of trials
**fit** one agent, in at least one third are **borderline**, otherwise they
**exceed** one agent. Régie's complexity estimates and split threshold are
calibrated against these classes.
```

In `README.md` "Quick start", add:

```bash
uv run regie-bench run <case> --regie-root ../regie --baseline --label diff --repeat 3
uv run regie-bench difficulty --label diff
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q && uv run ruff check src tests && uv run regie-bench smoke`
Expected: all pass; `All checks passed!`; every smoke case `PASS`.

- [ ] **Step 5: Commit**

```bash
git add src/regie_bench/cli.py docs/METHODOLOGY.md README.md tests/test_cli.py
git commit -F - <<'EOF'
feat: report empirical difficulty from repeated baseline trials

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RbtkGAHGVyLqrM25Tdxw2w
EOF
```

---

### Task 9: End-to-end check with real agents

Unit tests cannot show that Régie and the bench agree on the new fields. Two cheap real trials can.

**Files:** none (verification only; trial output lands in the git-ignored `results/`).

- [ ] **Step 1: Run one default and one baseline trial**

Run:
```bash
uv run regie-bench run reject-blank-title --regie-root ../regie --provider codex --label e2e-0a
uv run regie-bench run reject-blank-title --regie-root ../regie --provider codex --label e2e-0a-baseline --baseline
```
Expected: both print a result line; the baseline run shows no `agent review` row.

- [ ] **Step 2: Inspect the new fields**

Run:
```bash
python3 - <<'EOF'
import glob, json
for path in sorted(glob.glob("results/*/result.json"))[-2:]:
    r = json.load(open(path))
    print(r["label"], r["schema_version"], r["mode"], r["brief_sha256"][:12],
          r["evaluator_sha256"][:12], [row["accepted"] for row in r["acceptance_trajectory"]],
          r["first_accepting_commit"])
EOF
uv run regie-bench difficulty --label e2e-0a-baseline
```
Expected: schema `2`; modes `regie` and `baseline`; identical fingerprints on both lines; each trajectory ends in `True` if the trial passed; the difficulty table has one `reject-blank-title` row.

- [ ] **Step 3: Report**

Report both result lines, the inspected fields, and the difficulty row. If any field is missing or inconsistent, stop and debug (superpowers:systematic-debugging) before Plan 0b.

---

## Next: Plan 0b (written after this plan lands)

Plan 0b builds the feature ladder on this machinery, using the spec's §6.2–6.5: a mid-size FastAPI + Next.js fixture (SQLite, pytest API tests, vitest frontend tests, deliberate tech debt), six to ten stakeholder-style briefs from S to XL (at least a bug fix, an end-to-end field, an ambiguous request, a behavior-preserving refactor, a multi-increment feature, and a greenfield case), a hidden intent document per case, hidden tests, and a validated `reference.patch` per case. Then `validate` in CI and the ladder's empirical difficulty (`--baseline --repeat 3` per case). Two ladder cases are marked `track = "holdout"`.
