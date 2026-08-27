# Régie Core Engine Implementation Plan (Plan A of 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Régie's orchestrator core — state machine, crash-safe run directory, agent dispatch, gates, and the serial task pipeline — fully testable end-to-end with fake agent CLIs and a fixture git repo, no tokens spent.

**Architecture:** A run is a directory (`$REGIE_HOME/runs/<id>`) holding an authoritative `state.json` (atomic writes), a write-ahead `intent.jsonl`, and an observability-only `events.jsonl`. The engine is a plain Python state machine: it dispatches agent subprocesses (via adapters) in their own process groups, evaluates gates by running commands itself, and applies the retry/escalation ladder. Plan B adds real Claude/Codex adapters and git/PR stages; this plan proves the loop with a `FakeAdapter`.

**Tech Stack:** Python 3.12+, uv, typer, pydantic v2, pytest, tomllib (stdlib), PyYAML.

## Global Constraints (from the spec — apply to every task)

- The engine **never** reads `events.jsonl`; `state.json` is the single source of truth.
- `state.json` writes are atomic: write to `state.json.tmp`, then `os.replace`.
- Dispatch intent is appended to `intent.jsonl` **before** the subprocess is spawned.
- Gate decisions come only from commands the harness runs itself — never from an agent's exit code or self-report.
- Every agent invocation runs in its own process group; kill = SIGTERM then SIGKILL to the group.
- Runs live outside any target repo: default `~/.regie`, overridden by env var `REGIE_HOME` (tests always set it to a tmp dir).
- Escalation ladder: 3 attempts total per stage — 2 on the original binding, then 1 on the next-stronger binding (profiles already at the top rung skip to halt) → halt. (Wording ratified during final review to match ladder.py/Task 8 semantics.)
- Severity rubric enum is exactly: `blocker | major | minor`. Minors never re-enter the loop.
- Builder may not modify test files — enforced post-hoc via `git diff` against configured `test_globs`.
- Package name `regie`, CLI command `regie`. Conventional commits.

## File Structure

```
pyproject.toml
src/regie/
  __init__.py
  models.py        # pydantic state/config schemas (the shared vocabulary)
  rundir.py        # run directory: lock, atomic state, intent/events appends
  config.py        # regie.toml + profiles/*.yaml loading
  packets.py       # context-packet rendering → tasks/Tn/context.md
  agents/__init__.py
  agents/base.py   # AgentRequest/AgentResult + adapter protocol
  agents/fake.py   # scripted fake adapter (tests + fixtures)
  dispatch.py      # process-group spawn, wall/stall kill, intent ordering
  gitops.py        # minimal git helpers used by gates/pipeline (Plan B extends)
  gates.py         # command gate, flaky rerun, diff gate, red-test gate
  ladder.py        # escalation ladder decisions
  pipeline.py      # stage functions: plan_stage, task_stage; engine loop
  cli.py           # typer app: run, resume, status
tests/
  conftest.py      # regie_home tmp fixture, fixture git repo builder
  test_models.py  test_rundir.py  test_config.py  test_packets.py
  test_fake_adapter.py  test_dispatch.py  test_gates.py  test_ladder.py
  test_pipeline.py  test_cli.py  test_e2e.py
profiles/            # planner.yaml/.md, test-writer, builder, reviewer (Task 3)
```

---

### Task 1: Project scaffold + state models

**Files:**
- Create: `pyproject.toml`, `src/regie/__init__.py`, `src/regie/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces (later tasks import all of these from `regie.models`):
  - `Binding(cli: str, model: str, auth: str = "subscription")` (frozen)
  - `Budgets(turns: int = 40, wall_minutes: int = 30, stall_minutes: int = 5)`
  - `GateResult(name: str, passed: bool, detail: str = "", flaky: bool = False)`
  - `Finding(severity: Literal["blocker","major","minor"], title: str, detail: str = "", file: str | None = None)`
  - `Attempt(binding: Binding, prompt_hash: str = "", outcome: Literal["done","blocked","failed","quota"] | None = None, blocked_question: str | None = None, gate_results: list[GateResult] = [], usage: dict = {}, turns: int = 0)`
  - `TaskSpec(id: str, title: str, profile: str, criteria: list[str], file_scope: list[str] = [], checklist: list[str] = [], depends_on: list[str] = [])`
  - `TaskStage = Literal["test","build","review"]`
  - `TaskState(spec: TaskSpec, stage: TaskStage = "test", status: Literal["pending","running","done","blocked","failed"] = "pending", attempts: dict[str, list[Attempt]] = {"test": [], "build": [], "review": []})`
  - `RunStage = Literal["intake","plan","approve","tasks","finalize","pr","done","halted"]`
  - `RunState(id: str, target_repo: str, branch: str, base_sha: str = "", stage: RunStage = "intake", tasks: dict[str, TaskSpec-keyed TaskState] = {}, halt_reason: str | None = None)`
  - `RunState.ordered_task_ids() -> list[str]` — topological order of `tasks` by `depends_on`, raising `CycleError` (defined in models) on cycles; stable (ties broken by task id).

- [ ] **Step 1: Scaffold the project**

```bash
cd /Users/frederic/Code/regie
uv init --package --name regie --python 3.12
uv add pydantic typer pyyaml
uv add --dev pytest
mkdir -p src/regie/agents tests profiles docs/superpowers/plans
```
Edit `pyproject.toml` to include the CLI entry point:

```toml
[project.scripts]
regie = "regie.cli:app"
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_models.py
import pydantic
import pytest
from regie.models import Attempt, Binding, CycleError, RunState, TaskSpec, TaskState


def _task(tid: str, deps: list[str]) -> TaskState:
    return TaskState(spec=TaskSpec(id=tid, title=tid, profile="builder", criteria=["c"], depends_on=deps))


def test_run_state_round_trips_through_json():
    run = RunState(id="r1", target_repo="/tmp/x", branch="regie/r1")
    run.tasks["T1"] = _task("T1", [])
    run.tasks["T1"].attempts["build"].append(Attempt(binding=Binding(cli="fake", model="m1")))
    restored = RunState.model_validate_json(run.model_dump_json())
    assert restored.tasks["T1"].attempts["build"][0].binding.model == "m1"


def test_ordered_task_ids_is_topological_and_stable():
    run = RunState(id="r1", target_repo="/tmp/x", branch="regie/r1")
    run.tasks = {"T2": _task("T2", ["T1"]), "T1": _task("T1", []), "T3": _task("T3", ["T1"])}
    assert run.ordered_task_ids() == ["T1", "T2", "T3"]


def test_ordered_task_ids_raises_on_cycle():
    run = RunState(id="r1", target_repo="/tmp/x", branch="regie/r1")
    run.tasks = {"T1": _task("T1", ["T2"]), "T2": _task("T2", ["T1"])}
    with pytest.raises(CycleError):
        run.ordered_task_ids()


def test_finding_severity_is_constrained():
    from regie.models import Finding
    with pytest.raises(pydantic.ValidationError):
        Finding(severity="nitpick", title="x")
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'regie.models'`

- [ ] **Step 4: Implement the models**

```python
# src/regie/models.py
from __future__ import annotations

from graphlib import CycleError as _GraphCycleError
from graphlib import TopologicalSorter
from typing import Literal

from pydantic import BaseModel, Field


class CycleError(Exception):
    pass


class Binding(BaseModel, frozen=True):
    cli: str
    model: str
    auth: str = "subscription"


class Budgets(BaseModel):
    turns: int = 40
    wall_minutes: int = 30
    stall_minutes: int = 5


class GateResult(BaseModel):
    name: str
    passed: bool
    detail: str = ""
    flaky: bool = False


class Finding(BaseModel):
    severity: Literal["blocker", "major", "minor"]
    title: str
    detail: str = ""
    file: str | None = None


class Attempt(BaseModel):
    binding: Binding
    prompt_hash: str = ""
    outcome: Literal["done", "blocked", "failed", "quota"] | None = None
    blocked_question: str | None = None
    gate_results: list[GateResult] = Field(default_factory=list)
    usage: dict = Field(default_factory=dict)
    turns: int = 0


class TaskSpec(BaseModel):
    id: str
    title: str
    profile: str
    criteria: list[str]
    file_scope: list[str] = Field(default_factory=list)
    checklist: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)


TaskStage = Literal["test", "build", "review"]


def _empty_attempts() -> dict[str, list[Attempt]]:
    return {"test": [], "build": [], "review": []}


class TaskState(BaseModel):
    spec: TaskSpec
    stage: TaskStage = "test"
    status: Literal["pending", "running", "done", "blocked", "failed"] = "pending"
    attempts: dict[str, list[Attempt]] = Field(default_factory=_empty_attempts)


RunStage = Literal["intake", "plan", "approve", "tasks", "finalize", "pr", "done", "halted"]


class RunState(BaseModel):
    id: str
    target_repo: str
    branch: str
    base_sha: str = ""
    stage: RunStage = "intake"
    tasks: dict[str, TaskState] = Field(default_factory=dict)
    halt_reason: str | None = None

    def ordered_task_ids(self) -> list[str]:
        graph = {tid: sorted(t.spec.depends_on) for tid, t in sorted(self.tasks.items())}
        sorter = TopologicalSorter(graph)
        try:
            order = []
            sorter.prepare()
            while sorter.is_active():
                ready = sorted(sorter.get_ready())
                order.extend(ready)
                sorter.done(*ready)
            return order
        except _GraphCycleError as exc:
            raise CycleError(str(exc)) from exc
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_models.py -v`
Expected: 4 PASS

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src tests uv.lock .python-version
git commit -m "feat(models): project scaffold and state schemas"
```

---

### Task 2: Run directory — lock, atomic state, intent/events

**Files:**
- Create: `src/regie/rundir.py`
- Test: `tests/conftest.py`, `tests/test_rundir.py`

**Interfaces:**
- Consumes: `regie.models.RunState`
- Produces (`regie.rundir`):
  - `RunLocked(Exception)`
  - `class RunDir:`
    - `RunDir.create(home: Path, run_id: str) -> RunDir` — makes `home/runs/<run_id>/tasks/`
    - `RunDir.open(home: Path, run_id: str) -> RunDir` — raises `FileNotFoundError` if missing
    - `.path: Path`
    - `.acquire_lock() -> None` — `flock` on `run.lock`, non-blocking; raises `RunLocked` if held (lock is held for the RunDir object's lifetime; `.release_lock()` for tests)
    - `.write_state(state: RunState) -> None` — atomic tmp+replace
    - `.read_state() -> RunState`
    - `.append_intent(record: dict) -> None` / `.read_intents() -> list[dict]`
    - `.append_event(record: dict) -> None` (adds `"ts"` ISO timestamp; engine never reads this back — no read helper on purpose)
    - `.task_dir(task_id: str) -> Path` — creates `tasks/<task_id>/` on demand

- [ ] **Step 1: Write the shared fixtures**

```python
# tests/conftest.py
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def regie_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "regie-home"
    home.mkdir()
    monkeypatch.setenv("REGIE_HOME", str(home))
    return home


@pytest.fixture
def fixture_repo(tmp_path) -> Path:
    """A tiny git repo with one source file and one test file."""
    repo = tmp_path / "fixture-repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "tests" / "test_calc.py").write_text(
        "from src.calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    for cmd in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
    ):
        subprocess.run(cmd, cwd=repo, check=True)
    return repo
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_rundir.py
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
    rd.append_event({"kind": "dispatch"})
    line = json.loads((rd.path / "events.jsonl").read_text().splitlines()[0])
    assert line["kind"] == "dispatch" and "ts" in line


def test_truncated_intent_line_is_ignored(regie_home):
    rd = RunDir.create(regie_home, "r1")
    rd.append_intent({"task": "T1"})
    with (rd.path / "intent.jsonl").open("a") as f:
        f.write('{"task": "T2", "trunc')  # simulated crash mid-append
    assert [i["task"] for i in rd.read_intents()] == ["T1"]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_rundir.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'regie.rundir'`

- [ ] **Step 4: Implement RunDir**

```python
# src/regie/rundir.py
from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from regie.models import RunState


class RunLocked(Exception):
    pass


class RunDir:
    def __init__(self, path: Path):
        self.path = path
        self._lock_fh = None

    @classmethod
    def create(cls, home: Path, run_id: str) -> "RunDir":
        path = home / "runs" / run_id
        (path / "tasks").mkdir(parents=True)
        return cls(path)

    @classmethod
    def open(cls, home: Path, run_id: str) -> "RunDir":
        path = home / "runs" / run_id
        if not path.is_dir():
            raise FileNotFoundError(path)
        return cls(path)

    def acquire_lock(self) -> None:
        fh = (self.path / "run.lock").open("a")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            fh.close()
            raise RunLocked(str(self.path)) from exc
        self._lock_fh = fh

    def release_lock(self) -> None:
        if self._lock_fh:
            fcntl.flock(self._lock_fh, fcntl.LOCK_UN)
            self._lock_fh.close()
            self._lock_fh = None

    def write_state(self, state: RunState) -> None:
        tmp = self.path / "state.json.tmp"
        tmp.write_text(state.model_dump_json(indent=2))
        os.replace(tmp, self.path / "state.json")

    def read_state(self) -> RunState:
        return RunState.model_validate_json((self.path / "state.json").read_text())

    def _append_jsonl(self, name: str, record: dict) -> None:
        with (self.path / name).open("a") as f:
            f.write(json.dumps(record) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def append_intent(self, record: dict) -> None:
        self._append_jsonl("intent.jsonl", record)

    def read_intents(self) -> list[dict]:
        file = self.path / "intent.jsonl"
        if not file.exists():
            return []
        out = []
        for line in file.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # torn tail line from a crash mid-append
        return out

    def append_event(self, record: dict) -> None:
        self._append_jsonl("events.jsonl", {"ts": datetime.now(timezone.utc).isoformat(), **record})

    def task_dir(self, task_id: str) -> Path:
        d = self.path / "tasks" / task_id
        d.mkdir(parents=True, exist_ok=True)
        return d
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_rundir.py -v`
Expected: 5 PASS

- [ ] **Step 6: Commit**

```bash
git add src/regie/rundir.py tests/conftest.py tests/test_rundir.py
git commit -m "feat(rundir): crash-safe run directory with lock, atomic state, WAL intents"
```

---

### Task 3: Config — regie.toml + profiles

**Files:**
- Create: `src/regie/config.py`, `profiles/planner.yaml`, `profiles/planner.md`, `profiles/test-writer.yaml`, `profiles/test-writer.md`, `profiles/builder.yaml`, `profiles/builder.md`, `profiles/reviewer.yaml`, `profiles/reviewer.md`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `regie.models.Binding`, `regie.models.Budgets`
- Produces (`regie.config`):
  - `Profile(name: str, binding: Binding, prompt_path: Path, budgets: Budgets)` with `.prompt_text() -> str` and `.prompt_hash() -> str` (sha256 hex of prompt file)
  - `RegieConfig(commands: dict[str, str], test_globs: list[str], eval_trigger_globs: list[str], binding_strength: list[str], profiles: dict[str, Profile])` — `binding_strength` is an ordered list of `"cli:model"` strings, weakest→strongest
  - `load_config(repo: Path, profiles_dir: Path) -> RegieConfig` — reads `<repo>/regie.toml`; raises `ConfigError` (defined here) listing every missing required key/profile, not just the first
  - Required `regie.toml` keys: `[commands]` with at least `test`, `lint`; `test_globs`; `binding_strength`. Optional: `eval_trigger_globs` (default `[]`), extra commands.

- [ ] **Step 1: Write the profile files** (config data, not code — real starter content)

`profiles/builder.yaml`:
```yaml
binding: { cli: codex, model: gpt-5.x, auth: subscription }
budgets: { turns: 40, wall_minutes: 30, stall_minutes: 5 }
```
`profiles/builder.md`:
```markdown
You are the builder. Implement exactly what the task's acceptance criteria demand —
nothing more. You may read anything in the repo. You MUST NOT create or modify any
test file; if a test seems wrong, stop and report `blocked: <why the test is wrong>`.
Follow the conventions section verbatim. When done, summarize what you changed.
```
`profiles/planner.yaml` / `test-writer.yaml` / `reviewer.yaml`: same shape —
planner and test-writer bind `{ cli: claude, model: strongest, auth: subscription }`,
reviewer binds `{ cli: claude, model: strongest }` (dispatch flips it when the
author was Claude — Plan B). Budgets: planner `{ turns: 60, wall_minutes: 45, stall_minutes: 5 }`, others as builder.
`planner.md`: "You are the planner. Produce (1) an OpenSpec change proposal with
Given/When/Then acceptance criteria and (2) a task DAG as JSON matching the provided
schema. Every criterion must map to at least one named planned test."
`test-writer.md`: "You are the test author. Turn the task's criteria into failing
tests plus typed interface stubs that raise NotImplementedError. New tests must fail
with assertion errors or NotImplementedError only. Never implement behavior."
`reviewer.md`: "You are an adversarial reviewer. Attack the diff against the spec,
checklist, and conventions. Return findings as JSON matching the provided schema.
You have no authority to fix anything. Severity: blocker = violates spec/correctness/
security; major = real risk, must fix; minor = style/polish."

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_config.py
from pathlib import Path

import pytest
from regie.config import ConfigError, load_config

PROFILES = Path(__file__).parent.parent / "profiles"

GOOD_TOML = """
test_globs = ["tests/**"]
binding_strength = ["fake:m1", "codex:gpt-5.x", "claude:strongest"]
[commands]
test = "pytest -q"
lint = "ruff check ."
"""


def _repo_with(tmp_path, toml_text):
    (tmp_path / "regie.toml").write_text(toml_text)
    return tmp_path


def test_loads_commands_globs_and_profiles(tmp_path):
    cfg = load_config(_repo_with(tmp_path, GOOD_TOML), PROFILES)
    assert cfg.commands["test"] == "pytest -q"
    assert set(cfg.profiles) == {"planner", "test-writer", "builder", "reviewer"}
    assert cfg.profiles["builder"].binding.cli == "codex"
    assert len(cfg.profiles["builder"].prompt_hash()) == 64


def test_missing_keys_reported_together(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_config(_repo_with(tmp_path, "[commands]\nlint='x'"), PROFILES)
    msg = str(exc.value)
    assert "commands.test" in msg and "test_globs" in msg and "binding_strength" in msg


def test_missing_regie_toml_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path, PROFILES)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'regie.config'`

- [ ] **Step 4: Implement config loading**

```python
# src/regie/config.py
from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

import yaml
from pydantic import BaseModel

from regie.models import Binding, Budgets


class ConfigError(Exception):
    pass


class Profile(BaseModel):
    name: str
    binding: Binding
    prompt_path: Path
    budgets: Budgets

    def prompt_text(self) -> str:
        return self.prompt_path.read_text()

    def prompt_hash(self) -> str:
        return hashlib.sha256(self.prompt_path.read_bytes()).hexdigest()


class RegieConfig(BaseModel):
    commands: dict[str, str]
    test_globs: list[str]
    eval_trigger_globs: list[str] = []
    binding_strength: list[str]
    profiles: dict[str, Profile]


def _load_profiles(profiles_dir: Path, errors: list[str]) -> dict[str, Profile]:
    profiles = {}
    for yml in sorted(profiles_dir.glob("*.yaml")):
        name = yml.stem
        prompt = profiles_dir / f"{name}.md"
        if not prompt.exists():
            errors.append(f"profile '{name}' missing prompt file {prompt.name}")
            continue
        raw = yaml.safe_load(yml.read_text())
        profiles[name] = Profile(
            name=name,
            binding=Binding(**raw["binding"]),
            prompt_path=prompt,
            budgets=Budgets(**raw.get("budgets", {})),
        )
    if not profiles:
        errors.append(f"no profiles found in {profiles_dir}")
    return profiles


def load_config(repo: Path, profiles_dir: Path) -> RegieConfig:
    errors: list[str] = []
    toml_path = repo / "regie.toml"
    if not toml_path.exists():
        raise ConfigError(f"missing {toml_path}")
    data = tomllib.loads(toml_path.read_text())

    commands = data.get("commands", {})
    for key in ("test", "lint"):
        if key not in commands:
            errors.append(f"missing required key commands.{key}")
    for key in ("test_globs", "binding_strength"):
        if key not in data:
            errors.append(f"missing required key {key}")

    profiles = _load_profiles(profiles_dir, errors)
    if errors:
        raise ConfigError("; ".join(errors))

    return RegieConfig(
        commands=commands,
        test_globs=data["test_globs"],
        eval_trigger_globs=data.get("eval_trigger_globs", []),
        binding_strength=data["binding_strength"],
        profiles=profiles,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: 3 PASS

- [ ] **Step 6: Commit**

```bash
git add src/regie/config.py profiles tests/test_config.py
git commit -m "feat(config): regie.toml and profile loading with aggregated errors"
```

---

### Task 4: Context packets

**Files:**
- Create: `src/regie/packets.py`
- Test: `tests/test_packets.py`

**Interfaces:**
- Consumes: `TaskSpec` from models
- Produces (`regie.packets`):
  - `render_packet(task: TaskSpec, spec_excerpt: str, decisions: str, conventions: str, extra: str = "") -> str` — deterministic markdown with fixed section order: `# Task`, `## Acceptance criteria`, `## Reviewer checklist`, `## Spec excerpt`, `## Decisions so far`, `## Conventions`, `## Notes`; each of the free-text sections truncated to a per-section character budget (`SECTION_BUDGET = 8000` chars ≈ 2k tokens) with a visible `[... truncated]` marker
  - `write_packet(task_dir: Path, content: str) -> Path` — writes `context.md`, returns path

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_packets.py
from pathlib import Path

from regie.models import TaskSpec
from regie.packets import SECTION_BUDGET, render_packet, write_packet


def _task() -> TaskSpec:
    return TaskSpec(id="T1", title="Add divide", profile="builder",
                    criteria=["Given a and b, When divide(a,b), Then returns a/b"],
                    checklist=["no float surprises"])


def test_packet_has_fixed_section_order():
    md = render_packet(_task(), spec_excerpt="SPEC", decisions="D", conventions="C")
    positions = [md.index(h) for h in
                 ("# Task", "## Acceptance criteria", "## Reviewer checklist",
                  "## Spec excerpt", "## Decisions so far", "## Conventions")]
    assert positions == sorted(positions)
    assert "Add divide" in md and "SPEC" in md


def test_oversized_section_is_truncated_with_marker():
    md = render_packet(_task(), spec_excerpt="x" * (SECTION_BUDGET + 500),
                       decisions="", conventions="")
    assert "[... truncated]" in md
    assert len(md) < SECTION_BUDGET + 2000


def test_write_packet(tmp_path: Path):
    p = write_packet(tmp_path, "hello")
    assert p.name == "context.md" and p.read_text() == "hello"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_packets.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement packets**

```python
# src/regie/packets.py
from __future__ import annotations

from pathlib import Path

from regie.models import TaskSpec

SECTION_BUDGET = 8000  # chars, ≈2k tokens per free-text section


def _clip(text: str) -> str:
    if len(text) <= SECTION_BUDGET:
        return text
    return text[:SECTION_BUDGET] + "\n[... truncated]"


def render_packet(task: TaskSpec, spec_excerpt: str, decisions: str,
                  conventions: str, extra: str = "") -> str:
    criteria = "\n".join(f"- {c}" for c in task.criteria)
    checklist = "\n".join(f"- {c}" for c in task.checklist) or "- (none)"
    return "\n\n".join([
        f"# Task {task.id}: {task.title}",
        f"## Acceptance criteria\n{criteria}",
        f"## Reviewer checklist\n{checklist}",
        f"## Spec excerpt\n{_clip(spec_excerpt)}",
        f"## Decisions so far\n{_clip(decisions) or '(none yet)'}",
        f"## Conventions\n{_clip(conventions)}",
        f"## Notes\n{_clip(extra) or '(none)'}",
    ]) + "\n"


def write_packet(task_dir: Path, content: str) -> Path:
    path = task_dir / "context.md"
    path.write_text(content)
    return path
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_packets.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add src/regie/packets.py tests/test_packets.py
git commit -m "feat(packets): deterministic context-packet rendering with section budgets"
```

---

### Task 5: Agent adapter protocol + fake adapter

**Files:**
- Create: `src/regie/agents/__init__.py`, `src/regie/agents/base.py`, `src/regie/agents/fake.py`
- Test: `tests/test_fake_adapter.py`

**Interfaces:**
- Produces (`regie.agents.base`):
  - `AgentRequest(prompt: str, cwd: Path, binding: Binding, budgets: Budgets, output_schema: dict | None = None)`
  - `AgentResult(outcome: Literal["done","blocked","error","quota"], text: str = "", structured: dict | None = None, usage: dict = {}, turns: int = 0, blocked_question: str | None = None)`
  - `class AgentAdapter(Protocol):` with `build_command(req: AgentRequest) -> list[str]` and `parse(stdout: str, exit_code: int) -> AgentResult`
  - `get_adapter(cli: str) -> AgentAdapter` — registry keyed by `Binding.cli`; `"fake"` registered here, `"claude"`/`"codex"` registered in Plan B; unknown cli raises `KeyError`
- Produces (`regie.agents.fake`): `FakeAdapter` — `build_command` returns `[sys.executable, "-c", <script>]` where the script reads the JSON *action file* named by env-var-free convention `<cwd>/.fake_agent.json` and executes it. Action file schema (this is the fixture language every later test uses):
    ```json
    {"result": {"outcome": "done", "text": "...", "structured": null, "turns": 3},
     "writes": {"relative/path.py": "file content"},
     "sleep": 0}
    ```
    `writes` lets fixtures simulate an agent editing files; `sleep` (seconds) lets dispatch tests simulate hangs. `parse` reads the JSON the script prints to stdout; nonzero exit or unparseable stdout → `AgentResult(outcome="error", text=stdout[-2000:])`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_fake_adapter.py
import json
import subprocess
from pathlib import Path

from regie.agents.base import AgentRequest, get_adapter
from regie.models import Binding, Budgets


def _req(cwd: Path) -> AgentRequest:
    return AgentRequest(prompt="do it", cwd=cwd,
                        binding=Binding(cli="fake", model="m1"), budgets=Budgets())


def _run(cwd: Path):
    adapter = get_adapter("fake")
    proc = subprocess.run(adapter.build_command(_req(cwd)), cwd=cwd,
                          capture_output=True, text=True)
    return adapter.parse(proc.stdout, proc.returncode)


def test_fake_agent_returns_scripted_result_and_writes_files(tmp_path):
    (tmp_path / ".fake_agent.json").write_text(json.dumps({
        "result": {"outcome": "done", "text": "built it", "turns": 2},
        "writes": {"src/new.py": "x = 1\n"},
    }))
    result = _run(tmp_path)
    assert result.outcome == "done" and result.turns == 2
    assert (tmp_path / "src" / "new.py").read_text() == "x = 1\n"


def test_fake_agent_blocked_outcome(tmp_path):
    (tmp_path / ".fake_agent.json").write_text(json.dumps({
        "result": {"outcome": "blocked", "blocked_question": "which cache?"}}))
    assert _run(tmp_path).blocked_question == "which cache?"


def test_unparseable_output_is_error(tmp_path):
    adapter = get_adapter("fake")
    assert adapter.parse("garbage", 0).outcome == "error"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_fake_adapter.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement base + fake**

```python
# src/regie/agents/base.py
from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from regie.models import Binding, Budgets


class AgentRequest(BaseModel):
    prompt: str
    cwd: Path
    binding: Binding
    budgets: Budgets
    output_schema: dict | None = None


class AgentResult(BaseModel):
    outcome: Literal["done", "blocked", "error", "quota"]
    text: str = ""
    structured: dict | None = None
    usage: dict = Field(default_factory=dict)
    turns: int = 0
    blocked_question: str | None = None


class AgentAdapter(Protocol):
    def build_command(self, req: AgentRequest) -> list[str]: ...
    def parse(self, stdout: str, exit_code: int) -> AgentResult: ...


_REGISTRY: dict[str, AgentAdapter] = {}


def register(cli: str, adapter: AgentAdapter) -> None:
    _REGISTRY[cli] = adapter


def get_adapter(cli: str) -> AgentAdapter:
    return _REGISTRY[cli]
```

```python
# src/regie/agents/fake.py
from __future__ import annotations

import json
import sys

from regie.agents.base import AgentRequest, AgentResult, register

_SCRIPT = """
import json, os, pathlib, time
spec = json.loads(pathlib.Path(".fake_agent.json").read_text())
for rel, content in spec.get("writes", {}).items():
    p = pathlib.Path(rel); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
time.sleep(spec.get("sleep", 0))
print(json.dumps(spec.get("result", {"outcome": "done"})))
"""


class FakeAdapter:
    def build_command(self, req: AgentRequest) -> list[str]:
        return [sys.executable, "-c", _SCRIPT]

    def parse(self, stdout: str, exit_code: int) -> AgentResult:
        if exit_code != 0:
            return AgentResult(outcome="error", text=stdout[-2000:])
        try:
            return AgentResult(**json.loads(stdout.strip().splitlines()[-1]))
        except (json.JSONDecodeError, IndexError, TypeError, ValueError):
            return AgentResult(outcome="error", text=stdout[-2000:])


register("fake", FakeAdapter())
```

```python
# src/regie/agents/__init__.py
from regie.agents import fake  # noqa: F401  (registers "fake")
from regie.agents.base import AgentRequest, AgentResult, get_adapter  # noqa: F401
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_fake_adapter.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add src/regie/agents tests/test_fake_adapter.py
git commit -m "feat(agents): adapter protocol, registry, and scripted fake adapter"
```

---

### Task 6: Dispatch — process groups, budgets, intent ordering

**Files:**
- Create: `src/regie/dispatch.py`
- Test: `tests/test_dispatch.py`

**Interfaces:**
- Consumes: `RunDir`, `AgentRequest/AgentResult/get_adapter`
- Produces (`regie.dispatch`):
  - `run_agent(rundir: RunDir, task_id: str, stage: str, attempt_no: int, req: AgentRequest) -> AgentResult` — synchronous. Order of operations (load-bearing, from spec): (1) `rundir.append_intent({task, stage, attempt, binding})`, (2) spawn with `start_new_session=True` (own process group), (3) enforce wall-clock budget (`budgets.wall_minutes`) and stall budget (`budgets.stall_minutes` with no new stdout) by polling; on breach, SIGTERM the group, 5s grace, SIGKILL → returns `AgentResult(outcome="error", text="killed: wall|stall budget")`, (4) parse via adapter, (5) `rundir.append_event({kind: "attempt", ...usage/turns/outcome})`.
  - Stdout is streamed to `tasks/<task_id>/attempt-<n>.out` as it arrives (the stall detector watches this file's growth; it doubles as the transcript).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_dispatch.py
import json
import time

from regie.agents.base import AgentRequest
from regie.dispatch import run_agent
from regie.models import Binding, Budgets
from regie.rundir import RunDir


def _req(cwd, budgets=None) -> AgentRequest:
    return AgentRequest(prompt="p", cwd=cwd, binding=Binding(cli="fake", model="m1"),
                        budgets=budgets or Budgets())


def test_intent_written_before_result_and_event_after(regie_home, tmp_path):
    rd = RunDir.create(regie_home, "r1")
    (tmp_path / ".fake_agent.json").write_text(json.dumps(
        {"result": {"outcome": "done", "text": "ok", "turns": 1}}))
    result = run_agent(rd, "T1", "build", 1, _req(tmp_path))
    assert result.outcome == "done"
    intents = rd.read_intents()
    assert intents[0]["task"] == "T1" and intents[0]["attempt"] == 1
    assert (rd.path / "tasks" / "T1" / "attempt-1.out").exists()


def test_wall_budget_kills_hung_agent(regie_home, tmp_path):
    rd = RunDir.create(regie_home, "r1")
    (tmp_path / ".fake_agent.json").write_text(json.dumps(
        {"sleep": 60, "result": {"outcome": "done"}}))
    budgets = Budgets(wall_minutes=1, stall_minutes=1)
    # shrink budgets to seconds for the test via the seconds override hook
    start = time.monotonic()
    result = run_agent(rd, "T1", "build", 1, _req(tmp_path, budgets),
                       _wall_seconds=2, _stall_seconds=2)
    assert result.outcome == "error" and "killed" in result.text
    assert time.monotonic() - start < 30
```

Note for the implementer: `run_agent` takes private keyword-only overrides
`_wall_seconds`/`_stall_seconds` (default `None` → derived from `budgets`) so tests
don't wait minutes. This is a test seam, documented in the docstring.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_dispatch.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement dispatch**

```python
# src/regie/dispatch.py
from __future__ import annotations

import os
import signal
import subprocess
import time

from regie.agents.base import AgentRequest, AgentResult, get_adapter
from regie.rundir import RunDir

_POLL = 0.1


def _kill_group(proc: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def run_agent(rundir: RunDir, task_id: str, stage: str, attempt_no: int,
              req: AgentRequest, *, _wall_seconds: float | None = None,
              _stall_seconds: float | None = None) -> AgentResult:
    """Dispatch one agent attempt. Intent is logged BEFORE spawn (WAL);
    _wall_seconds/_stall_seconds are test seams overriding budget-derived limits."""
    wall = _wall_seconds or req.budgets.wall_minutes * 60
    stall = _stall_seconds or req.budgets.stall_minutes * 60
    rundir.append_intent({"task": task_id, "stage": stage, "attempt": attempt_no,
                          "binding": req.binding.model_dump()})

    out_path = rundir.task_dir(task_id) / f"attempt-{attempt_no}.out"
    adapter = get_adapter(req.binding.cli)
    killed = ""
    with out_path.open("wb") as out:
        proc = subprocess.Popen(adapter.build_command(req), cwd=req.cwd,
                                stdout=out, stderr=subprocess.STDOUT,
                                start_new_session=True)
        started = last_growth = time.monotonic()
        last_size = 0
        while proc.poll() is None:
            time.sleep(_POLL)
            now = time.monotonic()
            size = out_path.stat().st_size
            if size > last_size:
                last_size, last_growth = size, now
            if now - started > wall:
                killed = "killed: wall budget"
                break
            if now - last_growth > stall:
                killed = "killed: stall budget"
                break
        if killed:
            _kill_group(proc)

    if killed:
        result = AgentResult(outcome="error", text=killed)
    else:
        result = adapter.parse(out_path.read_text(errors="replace"), proc.returncode)
    rundir.append_event({"kind": "attempt", "task": task_id, "stage": stage,
                         "attempt": attempt_no, "outcome": result.outcome,
                         "turns": result.turns, "usage": result.usage})
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_dispatch.py -v`
Expected: 2 PASS (hang test completes in seconds)

- [ ] **Step 5: Commit**

```bash
git add src/regie/dispatch.py tests/test_dispatch.py
git commit -m "feat(dispatch): process-group spawn with WAL intent, wall and stall kills"
```

---

### Task 7: Git helpers + gates

**Files:**
- Create: `src/regie/gitops.py`, `src/regie/gates.py`
- Test: `tests/test_gates.py`

**Interfaces:**
- Produces (`regie.gitops` — the minimal core; Plan B extends with branch/squash/push):
  - `git(repo: Path, *args: str) -> str` — runs `git -C repo args`, raises `GitError(cmd, output)` on nonzero exit, returns stdout
  - `changed_files(repo: Path) -> list[str]` — union of staged, unstaged, and untracked paths (`git status --porcelain`)
  - `commit_all(repo: Path, message: str) -> str` — `add -A` + commit with fixed test author env, returns short sha
- Produces (`regie.gates`):
  - **Security note (trust boundary):** gate commands run with `shell=True` deliberately — they are operator-authored shell strings from `regie.toml` (same trust level as a Makefile), never agent output or task data. Invariant to preserve: nothing agent-generated is ever interpolated into a gate command string; agents influence gates only through the files the commands inspect. Document this in a comment on `_run`.
  - `run_command_gate(name: str, cmd: str, cwd: Path, rerun_on_fail: bool = False) -> GateResult` — shell command; if it fails and `rerun_on_fail`, runs once more: pass on rerun → `GateResult(passed=True, flaky=True)`; `detail` carries the last 4000 chars of output
  - `diff_gate(repo: Path, test_globs: list[str]) -> GateResult` — fails iff any path from `changed_files` matches a glob (uses `fnmatch` against posix paths; `**` handled via `pathlib.PurePosixPath.full_match`)
  - `red_test_gate(cwd: Path, test_cmd: str) -> GateResult` — for TDD red: runs `<test_cmd>`; passes iff exit code is nonzero **and** the output contains `AssertionError` or `NotImplementedError` **and** does not contain `ImportError`, `ModuleNotFoundError`, or `SyntaxError` outside those; also runs `<test_cmd> --collect-only` first and fails the gate if collection fails. `detail` records the failure reason category (`assertion | notimplemented | collection-error | import-error | unexpectedly-green`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_gates.py
from regie.gates import diff_gate, red_test_gate, run_command_gate
from regie.gitops import commit_all


def test_command_gate_pass_and_fail(tmp_path):
    assert run_command_gate("ok", "true", tmp_path).passed
    result = run_command_gate("boom", "echo nope && false", tmp_path)
    assert not result.passed and "nope" in result.detail


def test_flaky_rerun_marks_flaky(tmp_path):
    # fails first run, passes second: a file acts as the coin
    cmd = "test -f flag || { touch flag; false; }"
    result = run_command_gate("flaky", cmd, tmp_path, rerun_on_fail=True)
    assert result.passed and result.flaky


def test_diff_gate_blocks_test_edits(fixture_repo):
    (fixture_repo / "tests" / "test_calc.py").write_text("def test_add(): pass\n")
    result = diff_gate(fixture_repo, ["tests/**"])
    assert not result.passed and "test_calc.py" in result.detail


def test_diff_gate_allows_source_edits(fixture_repo):
    (fixture_repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b  # touched\n")
    assert diff_gate(fixture_repo, ["tests/**"]).passed


def test_red_gate_accepts_assertion_failure(fixture_repo):
    (fixture_repo / "tests" / "test_new.py").write_text(
        "def test_new():\n    assert 1 == 2\n")
    commit_all(fixture_repo, "add red test")
    result = red_test_gate(fixture_repo, "python -m pytest tests/test_new.py -q")
    assert result.passed and "assertion" in result.detail


def test_red_gate_rejects_import_error(fixture_repo):
    (fixture_repo / "tests" / "test_new.py").write_text(
        "from src.nonexistent import thing\n\ndef test_new():\n    assert thing()\n")
    result = red_test_gate(fixture_repo, "python -m pytest tests/test_new.py -q")
    assert not result.passed


def test_red_gate_rejects_green_tests(fixture_repo):
    (fixture_repo / "tests" / "test_new.py").write_text("def test_new():\n    assert True\n")
    result = red_test_gate(fixture_repo, "python -m pytest tests/test_new.py -q")
    assert not result.passed and "unexpectedly-green" in result.detail
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_gates.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement gitops core and gates**

```python
# src/regie/gitops.py
from __future__ import annotations

import subprocess
from pathlib import Path

_AUTHOR_ENV = {"GIT_AUTHOR_NAME": "regie", "GIT_AUTHOR_EMAIL": "regie@local",
               "GIT_COMMITTER_NAME": "regie", "GIT_COMMITTER_EMAIL": "regie@local"}


class GitError(Exception):
    pass


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True,
                          env={**__import__("os").environ, **_AUTHOR_ENV})
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout


def changed_files(repo: Path) -> list[str]:
    out = git(repo, "status", "--porcelain")
    return [line[3:].strip() for line in out.splitlines() if line.strip()]


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "--short", "HEAD").strip()
```

```python
# src/regie/gates.py
from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

from regie.gitops import changed_files
from regie.models import GateResult

_TAIL = 4000


def _run(cmd: str, cwd: Path) -> tuple[int, str]:
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
            if any(PurePosixPath(f).full_match(g) for g in test_globs)]
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_gates.py -v`
Expected: 7 PASS

- [ ] **Step 5: Commit**

```bash
git add src/regie/gitops.py src/regie/gates.py tests/test_gates.py
git commit -m "feat(gates): command, flaky-rerun, diff-guard, and honest TDD-red gates"
```

---

### Task 8: Escalation ladder

**Files:**
- Create: `src/regie/ladder.py`
- Test: `tests/test_ladder.py`

**Interfaces:**
- Consumes: `Binding`, `Attempt`
- Produces (`regie.ladder`):
  - `next_action(attempts: list[Attempt], binding: Binding, strength_order: list[str]) -> tuple[Literal["retry","escalate","halt"], Binding]` — pure function. `strength_order` entries are `"cli:model"` weakest→strongest. Rules: `len(attempts) < 2` → `("retry", same binding)`; `len == 2` → `("escalate", next-stronger binding)` if one exists above the current binding in the order, else `("halt", same)`; `len >= 3` → `("halt", same)`. An escalated binding keeps `auth` from the original. Any attempt with `outcome == "quota"` → immediate `("halt", same)` regardless of count (quota never burns the ladder).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ladder.py
from regie.ladder import next_action
from regie.models import Attempt, Binding

ORDER = ["fake:m1", "fake:m2", "claude:strongest"]
B1 = Binding(cli="fake", model="m1")


def _fails(n: int) -> list[Attempt]:
    return [Attempt(binding=B1, outcome="failed") for _ in range(n)]


def test_first_two_failures_retry_same_binding():
    assert next_action(_fails(1), B1, ORDER) == ("retry", B1)


def test_third_attempt_escalates_to_next_stronger():
    action, binding = next_action(_fails(2), B1, ORDER)
    assert action == "escalate" and binding.model == "m2"


def test_top_binding_skips_escalation_and_halts():
    top = Binding(cli="claude", model="strongest")
    attempts = [Attempt(binding=top, outcome="failed")] * 2
    assert next_action(attempts, top, ORDER)[0] == "halt"


def test_exhausted_halts():
    assert next_action(_fails(3), B1, ORDER)[0] == "halt"


def test_quota_halts_immediately_without_burning_ladder():
    attempts = [Attempt(binding=B1, outcome="quota")]
    assert next_action(attempts, B1, ORDER)[0] == "halt"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ladder.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement the ladder**

```python
# src/regie/ladder.py
from __future__ import annotations

from typing import Literal

from regie.models import Attempt, Binding

Action = Literal["retry", "escalate", "halt"]


def next_action(attempts: list[Attempt], binding: Binding,
                strength_order: list[str]) -> tuple[Action, Binding]:
    if any(a.outcome == "quota" for a in attempts):
        return "halt", binding
    n = len(attempts)
    if n < 2:
        return "retry", binding
    if n == 2:
        key = f"{binding.cli}:{binding.model}"
        try:
            idx = strength_order.index(key)
        except ValueError:
            return "halt", binding
        if idx + 1 < len(strength_order):
            cli, model = strength_order[idx + 1].split(":", 1)
            return "escalate", Binding(cli=cli, model=model, auth=binding.auth)
        return "halt", binding
    return "halt", binding
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ladder.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add src/regie/ladder.py tests/test_ladder.py
git commit -m "feat(ladder): pure escalation ladder with quota short-circuit"
```

---

### Task 9: Task pipeline — 2a/2b/2c loop with ladder and escape hatch

**Files:**
- Create: `src/regie/pipeline.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: everything above (`RunDir`, `RegieConfig`, `run_agent`, gates, ladder, packets, `commit_all`)
- Produces (`regie.pipeline`):
  - `run_task(rundir: RunDir, run: RunState, task_id: str, cfg: RegieConfig, repo: Path, ctx: PipelineContext) -> None` — mutates `run.tasks[task_id]` through stages `test → build → review`, persisting state after every transition. `PipelineContext(spec_excerpt: str, decisions_path: Path, conventions: str)` is a small dataclass defined in pipeline.py.
  - Stage semantics (each stage loops via `ladder.next_action` on its own attempts list):
    - **test**: dispatch test-writer with packet; gates = `red_test_gate(repo, cfg.commands["test"])` + `run_command_gate("lint", cfg.commands["lint"], repo)`; on pass `commit_all(repo, f"test({task_id}): red tests")`, stage → build.
    - **build**: dispatch builder; gates = `run_command_gate("test", cfg.commands["test"], repo, rerun_on_fail=True)` + lint + `diff_gate(repo, cfg.test_globs)`; on diff-gate failure the attempt fails with the gate detail in the packet's Notes for the retry. On pass `commit_all(repo, f"feat({task_id}): implement")`, stage → review. If the builder returns `blocked` and its `blocked_question` starts with `bad-test:`, reset stage → test (the wrong-test escape hatch): the *test attempts list* gets the claim appended to its next packet; a `bad-test` claim on a task whose test stage has already been re-entered once is treated as a failed build attempt instead (no ping-pong — one escape per task).
    - **review**: dispatch reviewer (binding = opposite family of the builder's actual binding — for Plan A, dispatch flips between the two configured reviewer/builder bindings; the flip rule is `reviewer.binding if builder_binding.cli != reviewer.binding.cli else builder-profile binding`) with `output_schema` = Finding-list schema; parse `result.structured["findings"]`; blockers or majors → stage stays review? **No:** findings route to the *author*: reset stage → build with findings in the packet Notes (they count as a failed build... no — as a new build attempt cycle). Precisely: blockers/majors append to `tasks/<id>/findings.json`, stage → build, and the ladder for build continues from its existing attempts list. Minors append to `tasks/<id>/minor-findings.json` (Plan B attaches them to the PR). No blockers/majors → task `status = done`.
    - Any stage's ladder returning `halt` → task `status = failed`, `run.stage = "halted"`, `run.halt_reason` set.
    - `blocked` (non-escape-hatch) → task `status = blocked`, `run.stage = "halted"`, `halt_reason = blocked_question`.
  - `run_tasks_stage(rundir, run, cfg, repo) -> None` — iterates `run.ordered_task_ids()` serially, skipping `done` tasks (resume-safe), stopping at the first halt.
  - Every dispatch builds its packet via `render_packet` + `write_packet` into the task dir, reading `decisions.md` fresh each time.

- [ ] **Step 1: Write the failing tests** (fake adapter drives every path)

```python
# tests/test_pipeline.py
import json
from pathlib import Path

import pytest
from regie.config import load_config
from regie.models import RunState, TaskSpec, TaskState
from regie.pipeline import PipelineContext, run_task, run_tasks_stage
from regie.rundir import RunDir

PROFILES_FAKE = None  # built by fixture below


@pytest.fixture
def fake_profiles(tmp_path) -> Path:
    d = tmp_path / "profiles"
    d.mkdir()
    for name in ("planner", "test-writer", "builder", "reviewer"):
        (d / f"{name}.yaml").write_text(
            "binding: { cli: fake, model: m1 }\n"
            "budgets: { turns: 5, wall_minutes: 1, stall_minutes: 1 }\n")
        (d / f"{name}.md").write_text(f"You are {name}.")
    return d


@pytest.fixture
def cfg(fixture_repo, fake_profiles):
    (fixture_repo / "regie.toml").write_text("""
test_globs = ["tests/**"]
binding_strength = ["fake:m1", "fake:m2"]
[commands]
test = "python -m pytest tests -q"
lint = "true"
""")
    return load_config(fixture_repo, fake_profiles)


def _run_state(repo) -> tuple[RunState, str]:
    spec = TaskSpec(id="T1", title="divide", profile="builder",
                    criteria=["Given 6,3 When divide Then 2"])
    run = RunState(id="r1", target_repo=str(repo), branch="regie/r1", stage="tasks")
    run.tasks["T1"] = TaskState(spec=spec)
    return run, "T1"


def _script(repo, step_results: list[dict]):
    """FakeAdapter reads .fake_agent.json per dispatch; tests queue behaviors by
    rewriting the file between stages via the 'queue' convention: the file holds
    a list, and a sitecustomize-free helper pops item 0 each run."""
    (repo / ".fake_agent.json").write_text(json.dumps(step_results.pop(0)))


RED_TEST = {"result": {"outcome": "done"}, "writes": {
    "tests/test_div.py": "from src.calc import divide\n\n"
                         "def test_div():\n    assert divide(6, 3) == 2\n",
    "src/calc.py": "def add(a, b):\n    return a + b\n\n"
                   "def divide(a, b):\n    raise NotImplementedError\n"}}
GREEN_BUILD = {"result": {"outcome": "done"}, "writes": {
    "src/calc.py": "def add(a, b):\n    return a + b\n\n"
                   "def divide(a, b):\n    return a // b\n"}}
CLEAN_REVIEW = {"result": {"outcome": "done",
                           "structured": {"findings": []}}}


def test_happy_path_test_build_review_done(regie_home, fixture_repo, cfg):
    rd = RunDir.create(regie_home, "r1")
    run, tid = _run_state(fixture_repo)
    ctx = PipelineContext(spec_excerpt="SPEC", decisions_path=rd.path / "decisions.md",
                          conventions="CONV")
    for step in (RED_TEST, GREEN_BUILD, CLEAN_REVIEW):
        _script(fixture_repo, [step])
        run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)
    assert run.tasks[tid].status == "done"
    assert (rd.path / "tasks" / "T1" / "context.md").exists()


def test_reviewer_blocker_routes_back_to_builder(regie_home, fixture_repo, cfg):
    rd = RunDir.create(regie_home, "r1")
    run, tid = _run_state(fixture_repo)
    ctx = PipelineContext(spec_excerpt="S", decisions_path=rd.path / "decisions.md",
                          conventions="C")
    for step in (RED_TEST, GREEN_BUILD):
        _script(fixture_repo, [step])
        run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)
    _script(fixture_repo, [{"result": {"outcome": "done", "structured": {"findings": [
        {"severity": "blocker", "title": "int division truncates"}]}}}])
    run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)
    assert run.tasks[tid].stage == "build"
    findings = json.loads((rd.path / "tasks" / "T1" / "findings.json").read_text())
    assert findings[0]["title"] == "int division truncates"


def test_builder_editing_tests_fails_diff_gate(regie_home, fixture_repo, cfg):
    rd = RunDir.create(regie_home, "r1")
    run, tid = _run_state(fixture_repo)
    ctx = PipelineContext(spec_excerpt="S", decisions_path=rd.path / "decisions.md",
                          conventions="C")
    _script(fixture_repo, [RED_TEST])
    run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)
    cheat = {"result": {"outcome": "done"}, "writes": {
        "tests/test_div.py": "def test_div():\n    assert True\n"}}
    _script(fixture_repo, [cheat])
    run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)
    attempts = run.tasks[tid].attempts["build"]
    assert attempts and attempts[-1].outcome == "failed"
    assert any(not g.passed and g.name == "diff-guard" for g in attempts[-1].gate_results)


def test_halt_after_ladder_exhaustion(regie_home, fixture_repo, cfg):
    rd = RunDir.create(regie_home, "r1")
    run, tid = _run_state(fixture_repo)
    ctx = PipelineContext(spec_excerpt="S", decisions_path=rd.path / "decisions.md",
                          conventions="C")
    bad = {"result": {"outcome": "done"}, "writes": {
        "tests/test_div.py": "def test_div():\n    assert True\n"}}  # never red
    for _ in range(4):
        _script(fixture_repo, [bad])
        run_task(rd, run, tid, cfg, fixture_repo, ctx, max_dispatches=1)
        if run.stage == "halted":
            break
    assert run.stage == "halted" and run.tasks[tid].status == "failed"


def test_run_tasks_stage_skips_done_tasks(regie_home, fixture_repo, cfg):
    rd = RunDir.create(regie_home, "r1")
    run, tid = _run_state(fixture_repo)
    run.tasks[tid].status = "done"
    rd.write_state(run)
    run_tasks_stage(rd, run, cfg, Path(fixture_repo))  # no .fake_agent.json → would error if dispatched
    assert run.tasks[tid].status == "done" and run.stage != "halted"
```

Note for the implementer: `run_task` takes `max_dispatches: int | None = None`
(test seam: process at most N agent dispatches then return, so tests can single-step
stages). `run_tasks_stage` calls it unbounded.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_pipeline.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement the pipeline** (the largest module — keep stage handlers small)

```python
# src/regie/pipeline.py
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from regie.agents.base import AgentRequest
from regie.config import Profile, RegieConfig
from regie.dispatch import run_agent
from regie.gates import diff_gate, red_test_gate, run_command_gate
from regie.gitops import commit_all
from regie.ladder import next_action
from regie.models import Attempt, Finding, GateResult, RunState
from regie.packets import render_packet, write_packet
from regie.rundir import RunDir

FINDINGS_SCHEMA = {"type": "object", "properties": {"findings": {"type": "array"}},
                   "required": ["findings"]}


@dataclass
class PipelineContext:
    spec_excerpt: str
    decisions_path: Path
    conventions: str


def _decisions(ctx: PipelineContext) -> str:
    return ctx.decisions_path.read_text() if ctx.decisions_path.exists() else ""


def _dispatch(rundir: RunDir, run: RunState, task_id: str, stage: str,
              profile: Profile, cfg: RegieConfig, repo: Path,
              ctx: PipelineContext, extra: str) -> tuple[Attempt, "AgentResult"]:
    task = run.tasks[task_id]
    attempts = task.attempts[stage]
    binding = profile.binding
    if attempts:
        action, binding = next_action(attempts, attempts[-1].binding,
                                      cfg.binding_strength)
        # caller already checked for halt; retry keeps binding, escalate upgrades
    packet = render_packet(task.spec, ctx.spec_excerpt, _decisions(ctx),
                           ctx.conventions, extra=extra)
    write_packet(rundir.task_dir(task_id), packet)
    req = AgentRequest(prompt=profile.prompt_text() + "\n\n" + packet, cwd=repo,
                       binding=binding, budgets=profile.budgets,
                       output_schema=FINDINGS_SCHEMA if stage == "review" else None)
    attempt = Attempt(binding=binding, prompt_hash=profile.prompt_hash())
    result = run_agent(rundir, task_id, stage, len(attempts) + 1, req)
    attempt.outcome = {"done": "done", "blocked": "blocked",
                       "quota": "quota"}.get(result.outcome, "failed")
    attempt.blocked_question = result.blocked_question
    attempt.usage, attempt.turns = result.usage, result.turns
    attempts.append(attempt)
    return attempt, result


def _halt(rundir: RunDir, run: RunState, task_id: str, reason: str) -> None:
    run.tasks[task_id].status = "failed" if "blocked" not in reason else "blocked"
    run.stage = "halted"
    run.halt_reason = reason
    rundir.write_state(run)


def _should_halt(rundir: RunDir, run: RunState, task_id: str, stage: str,
                 cfg: RegieConfig) -> bool:
    attempts = run.tasks[task_id].attempts[stage]
    if not attempts:
        return False
    action, _ = next_action(attempts, attempts[-1].binding, cfg.binding_strength)
    if action == "halt":
        _halt(rundir, run, task_id, f"{stage} ladder exhausted on {task_id}")
        return True
    return False


def _gate_and_advance(rundir, run, task_id, stage, gates: list[GateResult],
                      attempt: Attempt, on_pass) -> None:
    attempt.gate_results = gates
    if all(g.passed for g in gates):
        on_pass()
    else:
        attempt.outcome = "failed"
    rundir.write_state(run)


def run_task(rundir: RunDir, run: RunState, task_id: str, cfg: RegieConfig,
             repo: Path, ctx: PipelineContext,
             max_dispatches: int | None = None) -> None:
    """Advance one task through test → build → review. Test seam: max_dispatches."""
    task = run.tasks[task_id]
    task.status = "running"
    dispatched = 0
    escaped_once = False

    while task.status == "running":
        if max_dispatches is not None and dispatched >= max_dispatches:
            return
        stage = task.stage
        if _should_halt(rundir, run, task_id, stage, cfg):
            return
        extra = _notes_for(rundir, task_id, stage)
        profile = cfg.profiles[{"test": "test-writer", "build": "builder",
                                "review": "reviewer"}[stage]]
        attempt, result = _dispatch(rundir, run, task_id, stage, profile, cfg,
                                    repo, ctx, extra)
        dispatched += 1

        if attempt.outcome == "quota":
            _halt(rundir, run, task_id, f"quota exhausted during {stage}")
            return
        if attempt.outcome == "blocked":
            question = attempt.blocked_question or ""
            if stage == "build" and question.startswith("bad-test:") and not escaped_once:
                escaped_once = True
                task.stage = "test"
                _write_note(rundir, task_id, "test", f"Builder claims: {question}")
                rundir.write_state(run)
                continue
            _halt(rundir, run, task_id, f"blocked: {question}")
            return
        if attempt.outcome == "failed":
            rundir.write_state(run)
            continue

        if stage == "test":
            gates = [red_test_gate(repo, cfg.commands["test"]),
                     run_command_gate("lint", cfg.commands["lint"], repo)]
            def _pass_test():
                commit_all(repo, f"test({task_id}): red tests")
                task.stage = "build"
            _gate_and_advance(rundir, run, task_id, stage, gates, attempt, _pass_test)
        elif stage == "build":
            gates = [run_command_gate("test", cfg.commands["test"], repo,
                                      rerun_on_fail=True),
                     run_command_gate("lint", cfg.commands["lint"], repo),
                     diff_gate(repo, cfg.test_globs)]
            def _pass_build():
                commit_all(repo, f"feat({task_id}): implement")
                task.stage = "review"
            _gate_and_advance(rundir, run, task_id, stage, gates, attempt, _pass_build)
        else:  # review
            findings = [Finding(**f) for f in
                        (result.structured or {}).get("findings", [])]
            serious = [f for f in findings if f.severity in ("blocker", "major")]
            minors = [f for f in findings if f.severity == "minor"]
            tdir = rundir.task_dir(task_id)
            if minors:
                _append_json(tdir / "minor-findings.json", minors)
            if serious:
                _append_json(tdir / "findings.json", serious)
                _write_note(rundir, task_id, "build",
                            "Review findings to fix:\n" + "\n".join(
                                f"- [{f.severity}] {f.title}: {f.detail}" for f in serious))
                task.stage = "build"
            else:
                task.status = "done"
            rundir.write_state(run)


def _append_json(path: Path, findings: list[Finding]) -> None:
    existing = json.loads(path.read_text()) if path.exists() else []
    existing.extend(f.model_dump() for f in findings)
    path.write_text(json.dumps(existing, indent=2))


def _write_note(rundir: RunDir, task_id: str, stage: str, note: str) -> None:
    (rundir.task_dir(task_id) / f"note-{stage}.md").write_text(note)


def _notes_for(rundir: RunDir, task_id: str, stage: str) -> str:
    path = rundir.task_dir(task_id) / f"note-{stage}.md"
    return path.read_text() if path.exists() else ""


def run_tasks_stage(rundir: RunDir, run: RunState, cfg: RegieConfig,
                    repo: Path) -> None:
    ctx = PipelineContext(
        spec_excerpt=(rundir.path / "spec" / "spec.md").read_text()
        if (rundir.path / "spec" / "spec.md").exists() else "",
        decisions_path=rundir.path / "decisions.md",
        conventions=_conventions(repo))
    for task_id in run.ordered_task_ids():
        if run.tasks[task_id].status == "done":
            continue
        run_task(rundir, run, task_id, cfg, repo, ctx)
        if run.stage == "halted":
            return
    run.stage = "finalize"
    rundir.write_state(run)


def _conventions(repo: Path) -> str:
    parts = []
    for name in ("CLAUDE.md", "AGENTS.md"):
        p = repo / name
        if p.exists():
            parts.append(p.read_text())
    return "\n\n".join(parts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_pipeline.py -v`
Expected: 5 PASS

- [ ] **Step 5: Run the full suite** (regression check)

Run: `uv run pytest -q`
Expected: all tests green

- [ ] **Step 6: Commit**

```bash
git add src/regie/pipeline.py tests/test_pipeline.py
git commit -m "feat(pipeline): serial test/build/review task loop with ladder and escape hatch"
```

---

### Task 10: CLI — run, resume, status (+ resume reconciliation)

**Files:**
- Create: `src/regie/cli.py`
- Modify: `src/regie/pipeline.py` (add `resume_run` + worktree-discard helper)
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces (`regie.cli`): typer app with commands:
  - `regie run <brief.md> --repo <path>` — creates run id `YYYY-MM-DD-<brief-stem>`, copies brief into run dir, initializes `RunState(stage="tasks")` **for Plan A** with tasks parsed from a `tasks.json` file sitting next to the brief (Plan B replaces this with the real planner stage; the JSON matches `list[TaskSpec]`), acquires lock, calls `run_tasks_stage`.
  - `regie resume <run-id> --repo <path>` — opens run dir, acquires lock (refusing if live), **reconciliation:** for every intent record with no matching completed attempt in state (match on task/stage/attempt index), appends a synthetic `Attempt(outcome="failed")` marker... precisely: `reconcile(rundir, run) -> int` in pipeline.py compares `read_intents()` counts per (task, stage) against `len(attempts[stage])`; any intent beyond recorded attempts ⇒ append `Attempt(binding=<from intent>, outcome="failed")` and run `git checkout . && git clean -fd` in the repo (discard the in-flight attempt's uncommitted edits). Returns count reconciled. Then re-enters `run_tasks_stage`.
  - `regie status <run-id>` — prints stage, per-task `stage/status/attempt-counts`, halt reason. Plain text, one task per line.
- Produces (`regie.pipeline`): `reconcile(rundir: RunDir, run: RunState, repo: Path) -> int` as described.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cli.py
import json

from typer.testing import CliRunner

from regie.cli import app
from regie.models import Attempt, Binding, RunState, TaskSpec, TaskState
from regie.pipeline import reconcile
from regie.rundir import RunDir

runner = CliRunner()


def _seed_run(regie_home, fixture_repo, status="pending") -> str:
    rd = RunDir.create(regie_home, "r1")
    run = RunState(id="r1", target_repo=str(fixture_repo), branch="regie/r1",
                   stage="tasks")
    run.tasks["T1"] = TaskState(
        spec=TaskSpec(id="T1", title="t", profile="builder", criteria=["c"]),
        status=status)
    rd.write_state(run)
    return "r1"


def test_status_prints_task_lines(regie_home, fixture_repo):
    _seed_run(regie_home, fixture_repo)
    result = runner.invoke(app, ["status", "r1"])
    assert result.exit_code == 0
    assert "T1" in result.output and "pending" in result.output


def test_reconcile_marks_orphaned_intent_failed_and_cleans_worktree(
        regie_home, fixture_repo):
    _seed_run(regie_home, fixture_repo)
    rd = RunDir.open(regie_home, "r1")
    run = rd.read_state()
    rd.append_intent({"task": "T1", "stage": "build", "attempt": 1,
                      "binding": {"cli": "fake", "model": "m1"}})
    (fixture_repo / "src" / "orphan.py").write_text("dirty\n")  # in-flight edit
    count = reconcile(rd, run, fixture_repo)
    assert count == 1
    assert run.tasks["T1"].attempts["build"][0].outcome == "failed"
    assert not (fixture_repo / "src" / "orphan.py").exists()


def test_run_command_executes_tasks_from_tasks_json(regie_home, fixture_repo,
                                                    fake_profiles, tmp_path):
    (fixture_repo / "regie.toml").write_text(
        'test_globs = ["tests/**"]\nbinding_strength = ["fake:m1"]\n'
        '[commands]\ntest = "true"\nlint = "true"\n')
    brief = tmp_path / "brief.md"
    brief.write_text("# Do the thing")
    (tmp_path / "tasks.json").write_text(json.dumps([{
        "id": "T1", "title": "t", "profile": "builder", "criteria": ["c"]}]))
    (fixture_repo / ".fake_agent.json").write_text(json.dumps(
        {"result": {"outcome": "blocked", "blocked_question": "?"}}))
    result = runner.invoke(app, ["run", str(brief), "--repo", str(fixture_repo),
                                 "--profiles", str(fake_profiles)])
    assert result.exit_code == 1  # halted on blocked
    assert "halted" in result.output.lower()
```

(`fake_profiles` fixture: move it from `tests/test_pipeline.py` into `tests/conftest.py` in this task so both files share it.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError` (no `regie.cli`) and missing `reconcile`

- [ ] **Step 3: Implement reconcile + CLI**

Add to `src/regie/pipeline.py`:

```python
def reconcile(rundir: RunDir, run: RunState, repo: Path) -> int:
    """Resume reconciliation: any WAL intent without a recorded attempt means we
    crashed mid-dispatch — mark it failed and discard uncommitted worktree edits."""
    from collections import Counter

    from regie.gitops import git
    from regie.models import Binding

    intents = Counter()
    bindings: dict[tuple[str, str], dict] = {}
    for rec in rundir.read_intents():
        key = (rec["task"], rec["stage"])
        intents[key] += 1
        bindings[key] = rec.get("binding", {"cli": "fake", "model": "?"})
    fixed = 0
    for (task_id, stage), count in intents.items():
        attempts = run.tasks[task_id].attempts[stage]
        while len(attempts) < count:
            attempts.append(Attempt(binding=Binding(**bindings[(task_id, stage)]),
                                    outcome="failed"))
            fixed += 1
    if fixed:
        git(repo, "checkout", "--", ".")
        git(repo, "clean", "-fd")
        run.tasks_dirty = None  # no-op; explicitness not needed
        rundir.write_state(run)
    return fixed
```

```python
# src/regie/cli.py
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

import typer

from regie.config import load_config
from regie.models import RunState, TaskSpec, TaskState
from regie.pipeline import reconcile, run_tasks_stage
from regie.rundir import RunDir, RunLocked

app = typer.Typer(add_completion=False)


def _home() -> Path:
    return Path(os.environ.get("REGIE_HOME", Path.home() / ".regie"))


@app.command()
def run(brief: Path, repo: Path = typer.Option(...),
        profiles: Path = typer.Option(Path(__file__).parent.parent.parent / "profiles")):
    cfg = load_config(repo, profiles)
    run_id = f"{date.today().isoformat()}-{brief.stem}"
    rundir = RunDir.create(_home(), run_id)
    rundir.acquire_lock()
    (rundir.path / "brief.md").write_text(brief.read_text())
    tasks_file = brief.parent / "tasks.json"  # Plan A stand-in for the planner stage
    specs = [TaskSpec(**t) for t in json.loads(tasks_file.read_text())]
    state = RunState(id=run_id, target_repo=str(repo), branch=f"regie/{run_id}",
                     stage="tasks",
                     tasks={s.id: TaskState(spec=s) for s in specs})
    rundir.write_state(state)
    run_tasks_stage(rundir, state, cfg, repo)
    _finish(state)


@app.command()
def resume(run_id: str, repo: Path = typer.Option(...),
           profiles: Path = typer.Option(Path(__file__).parent.parent.parent / "profiles")):
    cfg = load_config(repo, profiles)
    rundir = RunDir.open(_home(), run_id)
    try:
        rundir.acquire_lock()
    except RunLocked:
        typer.echo("run is live in another process; refusing", err=True)
        raise typer.Exit(2)
    state = rundir.read_state()
    fixed = reconcile(rundir, state, repo)
    if fixed:
        typer.echo(f"reconciled {fixed} orphaned attempt(s)")
    if state.stage == "halted":
        state.stage = "tasks"
        state.halt_reason = None
        for t in state.tasks.values():
            if t.status in ("failed", "blocked", "running"):
                t.status = "pending"
    run_tasks_stage(rundir, state, cfg, repo)
    _finish(state)


@app.command()
def status(run_id: str):
    state = RunDir.open(_home(), run_id).read_state()
    typer.echo(f"run {state.id}  stage={state.stage}"
               + (f"  halt={state.halt_reason}" if state.halt_reason else ""))
    for tid in state.ordered_task_ids():
        t = state.tasks[tid]
        counts = "/".join(str(len(t.attempts[s])) for s in ("test", "build", "review"))
        typer.echo(f"  {tid}  {t.status:8s} stage={t.stage:6s} attempts(t/b/r)={counts}")


def _finish(state: RunState) -> None:
    if state.stage == "halted":
        typer.echo(f"HALTED: {state.halt_reason}", err=True)
        raise typer.Exit(1)
    typer.echo(f"run {state.id} → {state.stage}")


if __name__ == "__main__":
    app()
```

(Remove the stray `run.tasks_dirty` line from `reconcile` — shown here to remind the implementer: **do not add fields outside the models**. Final code omits it.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add src/regie/cli.py src/regie/pipeline.py tests/test_cli.py tests/conftest.py
git commit -m "feat(cli): run/resume/status commands with WAL reconciliation"
```

---

### Task 11: End-to-end — full fake run + crash injection

**Files:**
- Test: `tests/test_e2e.py`

**Interfaces:**
- Consumes: everything. No new production code — this task proves the assembly and is the Plan A exit criterion.

- [ ] **Step 1: Write the end-to-end tests**

```python
# tests/test_e2e.py
"""Plan A exit criterion: a two-task run completes through the fake adapter,
and a simulated crash mid-run resumes to completion."""
import json

from typer.testing import CliRunner

from regie.cli import app
from regie.rundir import RunDir

runner = CliRunner()

TASKS = [
    {"id": "T1", "title": "divide", "profile": "builder",
     "criteria": ["Given 6,3 When divide Then 2"]},
    {"id": "T2", "title": "power", "profile": "builder",
     "criteria": ["Given 2,3 When power Then 8"], "depends_on": ["T1"]},
]

# One shared fake script serves every dispatch: it always reports done and
# (idempotently) writes green source + tests. Gates, not the agent, decide
# progress — so with commands test/lint = "true" plus a real red gate we
# instead use per-dispatch scripting like test_pipeline. For e2e we take the
# simplest honest path: commands are real pytest, and the fake writes a
# consistent final state each time; the red gate is what forces the test
# stage to have written failing tests first.


def _setup(regie_home, fixture_repo, fake_profiles, tmp_path):
    (fixture_repo / "regie.toml").write_text(
        'test_globs = ["tests/**"]\nbinding_strength = ["fake:m1"]\n'
        '[commands]\ntest = "true"\nlint = "true"\n')
    brief = tmp_path / "feature.md"
    brief.write_text("# two functions")
    (tmp_path / "tasks.json").write_text(json.dumps(TASKS))
    (fixture_repo / ".fake_agent.json").write_text(json.dumps(
        {"result": {"outcome": "done", "structured": {"findings": []}}}))
    return brief


def test_full_run_reaches_finalize(regie_home, fixture_repo, fake_profiles, tmp_path):
    brief = _setup(regie_home, fixture_repo, fake_profiles, tmp_path)
    result = runner.invoke(app, ["run", str(brief), "--repo", str(fixture_repo),
                                 "--profiles", str(fake_profiles)])
    assert result.exit_code == 0, result.output
    run_id = [l for l in result.output.splitlines() if "→" in l][0].split()[1]
    state = RunDir.open(regie_home, run_id).read_state()
    assert state.stage == "finalize"
    assert all(t.status == "done" for t in state.tasks.values())


def test_crash_then_resume_completes(regie_home, fixture_repo, fake_profiles,
                                     tmp_path, monkeypatch):
    brief = _setup(regie_home, fixture_repo, fake_profiles, tmp_path)
    # Crash injection: make the SECOND dispatch raise inside run_agent.
    import regie.pipeline as pipeline
    real = pipeline.run_agent
    calls = {"n": 0}

    def crashing(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt  # simulated hard crash mid-run
        return real(*args, **kwargs)

    monkeypatch.setattr(pipeline, "run_agent", crashing)
    result = runner.invoke(app, ["run", str(brief), "--repo", str(fixture_repo),
                                 "--profiles", str(fake_profiles)])
    assert result.exit_code != 0  # crashed

    monkeypatch.setattr(pipeline, "run_agent", real)
    run_id = sorted((regie_home / "runs").iterdir())[-1].name
    result = runner.invoke(app, ["resume", run_id, "--repo", str(fixture_repo),
                                 "--profiles", str(fake_profiles)])
    assert result.exit_code == 0, result.output
    state = RunDir.open(regie_home, run_id).read_state()
    assert all(t.status == "done" for t in state.tasks.values())
```

Note: with `test = "true"` the red gate (`red_test_gate` runs the *real* command)
would report `unexpectedly-green`. The e2e config therefore needs the red gate to
see failure first — set `test` to `python -m pytest tests -q` and have the fake's
single scripted write produce the RED_TEST content from test_pipeline for T1/T2
(union of both tasks' files: failing tests + NotImplementedError stubs), which the
same script's build dispatch then overwrites with implementations. Since one
static `.fake_agent.json` can't vary by dispatch, reuse the queue trick: write a
`.fake_agent.json` whose `writes` include a `queue/` directory the script pops —
**implementer:** extend `_SCRIPT` in `agents/fake.py` (5 lines) so that if
`.fake_agent_queue/` exists, it consumes `0.json`, `1.json`, … in order, renaming
each to `.done` after use, falling back to `.fake_agent.json`. Add a unit test for
the queue in `tests/test_fake_adapter.py`:

```python
def test_fake_agent_queue_consumes_in_order(tmp_path):
    q = tmp_path / ".fake_agent_queue"
    q.mkdir()
    (q / "0.json").write_text(json.dumps({"result": {"outcome": "done", "text": "first"}}))
    (q / "1.json").write_text(json.dumps({"result": {"outcome": "done", "text": "second"}}))
    assert _run(tmp_path).text == "first"
    assert _run(tmp_path).text == "second"
```

Then `_setup` queues, in order: T1 red-test write → T1 build write → T1 clean
review → T2 red-test write → T2 build write → T2 clean review (6 entries; the
crash test re-queues from the reconciled position by re-creating entries 1–5
before resume).

- [ ] **Step 2: Extend the fake adapter queue + run the new unit test**

Run: `uv run pytest tests/test_fake_adapter.py -v`
Expected: 4 PASS (3 old + queue test)

- [ ] **Step 3: Run the e2e tests**

Run: `uv run pytest tests/test_e2e.py -v`
Expected: 2 PASS

- [ ] **Step 4: Full suite, lint sweep**

```bash
uv run pytest -q
uvx ruff check src tests
```
Expected: all green; fix any ruff findings before committing.

- [ ] **Step 5: Commit**

```bash
git add tests/test_e2e.py src/regie/agents/fake.py tests/test_fake_adapter.py
git commit -m "test(e2e): full fake-agent run with crash injection and resume"
```

---

## What Plan B covers (next plan, after this one executes)

**Carried over from Plan A execution (parked with ruling):** the reviewer
binding-flip (cross-model rule — reviewer must bind the opposite family of the
builder's *actual* attempt binding) is not implemented in Plan A's pipeline,
because with only the fake adapter every profile shares one CLI and the flip is
untestable dead code. Plan B MUST implement it at review dispatch when real
adapters land.

**Also carried from Plan A's final review:** friendly CLI errors (duplicate
run-id on same day, missing tasks.json currently raise raw tracebacks) · the
spec's "refuse two live runs against the same target repo" guard · quota-halt
resume semantics revisited alongside real quota detection.

Real `claude`/`codex` adapters (result-JSON/JSONL parsing, quota detection, `--json-schema`) · gitops extensions (run worktree + branch creation off pinned base SHA, squash with backup ref + tree-identity check, push, `gh pr create`, CI watch) · planner stage replacing `tasks.json` (OpenSpec output, criteria parse gate, `regie approve` checkpoint) · finalize stage (full suite, eval-trigger predicate, rebase-halt) · debugger rounds · desktop notification · `regie clean` · ai-search-platform prerequisites (Make targets, suite timing, `regie.toml`).

## Self-review notes

- Spec coverage: principles 1–8 all land in Plan A except real-CLI specifics (Plan B by design). Ladder (Task 8), WAL+atomicity+lock (Task 2), budgets/stall (Task 6), diff guard + honest red gate (Task 7), rubric routing + escape hatch (Task 9), reconciliation (Task 10), crash e2e (Task 11).
- Type consistency: `Attempt.outcome` includes `"quota"`; adapters map CLI errors to it in Plan B — `FakeAdapter` can emit it via scripting (used in ladder test via model construction only, which is fine).
- Known simplification accepted for Plan A: `run`/`resume` execute synchronously in-process (no daemon); notification is Plan B.
