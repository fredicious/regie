# Phase 1a — Framework Core and Product Owner Intake (engine + CLI) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Régie's keyword-routed tiers and plan stage with the framework core: an explicit request/increment lifecycle, a Product Owner intake that interviews the stakeholder and produces a Ready spec, stakeholder decisions through `regie inbox` / `regie decide`, and execution of the spec's single increment by the existing direct-owner loop.

**Architecture:** A new `lifecycle` module owns every request and increment state change through transition tables. A `framework` module holds the mechanical rules (spec Definition of Ready, stable criterion IDs, spec rendering). An `intake` module runs the Product Owner: questions in batches with defaults, at most three question rounds, then a Ready spec that the stakeholder approves (or the fast path skips). `operations` records stakeholder decisions; the CLI drives the request until it waits for the stakeholder, halts, or is done. The plan stage, plan reviews, tiers, keyword routing, and checkpoints are deleted.

**Tech Stack:** Python 3.12, Typer, pydantic 2, Hypothesis (new dev dependency), pytest.

**Spec:** `docs/superpowers/specs/2026-09-28-regie-agile-framework-design.md` — §1 Intake, §7 Architecture (state model, framework rules, deleted), §8 Phase 1, §9 Testing.

**Scope:** Phase 1 is delivered in two plans. **1a (this plan):** engine, CLI, deletions, docs, and a real-agent smoke run. **1b (next):** the control room's Inbox tab and tab structure, the bench's stakeholder simulator and its recording of questions, defaults, and decisions, the bench's move off routes, and the Phase 1 bench measurement. This plan keeps the control room working with small changes (Task 6).

**Phase 1 rulings carried by this plan** (the spec's Phase 1 made concrete):
- Planning poker arrives in Phase 4, so Phase 1's **fast path** is: the Product Owner asked no question, listed no assumption, and the stakeholder gave no feedback.
- Phase 1 specs have **exactly one increment**; its work is one direct-owner task (`T1`) whose criteria are the increment's criteria with their IDs.
- **`--autonomous`** means the stakeholder is not in the loop: questions take their defaults (recorded as assumptions) and spec approval is skipped. The bench uses this until 1b's simulator.
- Every Product Owner question has a default; one returned without a default gets "no preference — the Product Owner decides".
- A **developer's clarification question** and a recovery Product Owner's `ask_human` become inbox questions (`round = 0`, no default) and halt the run, as before.
- **`regie inbox` and `regie decide` arrive now** (the spec lists them under Phase 3), because this plan deletes `regie approve` and `regie answer` and the CLI still needs a way to answer and approve.
- Requested spec changes are shown as a diff (`spec/spec.diff`), as §1.6 asks.
- Increment states in Phase 1: `draft → ready → building → accepted`. Phases 2–3 insert review and stakeholder states.
- Deleting risk inference removes its two remaining uses: specialists now run only for explicit `review_lenses` (Phase 2 deletes them), and the recovery Product Owner's `accept` loses its keyword security guard (its prompt still forbids waiving security; Phase 2 deletes the arbiter). Coverage and gate plugins run whenever configured — tiers no longer filter them.
- Runs created before this plan cannot be resumed or opened (no users to migrate). `regie stats` keeps reading historical `planner_attempts` from old runs' raw JSON.

## Global Constraints

- All work is in `/Users/frederic/Code/regie`; commit on `main`, never push.
- Every commit message ends with:
  ```
  Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01RbtkGAHGVyLqrM25Tdxw2w
  ```
- Verification: `uv run pytest -q` and `uvx ruff check src tests`.
- Structured-output schemas stay strict: every object has `additionalProperties: false` and lists every property in `required` (Codex rejects anything else). No `minItems`; validate counts mechanically instead.
- No new runtime dependency. Hypothesis is a dev dependency only.
- Spec values: criterion IDs `INC-<n>/AC-<m>`; at most three question rounds; Given/When/Then criteria; stable criterion IDs (unchanged keep their ID, new get the next number, removed are retired and never reused).
- Imports go at the top of each file (ruff E402): when a step says "append these tests", add their imports to the file's import block.

## Review Focus

1. Resuming or opening a run created before this plan (old stage names, or a `state.json` that no longer validates) must refuse with a clear message, not crash. (Tasks 5, 7)
2. Continuing after questions takes the default for every open question that has one; a developer question without a default stays open and does not block a resume. (Tasks 4–5)
3. A Product Owner that repeats an already-asked question in a later round must not re-ask the stakeholder. (Task 3)
4. A budget already exhausted when intake starts halts before any Product Owner dispatch. (Task 3)
5. Two halts in a row (e.g. a crashing parallel worker after a sibling halted) keep the first halt reason and never raise. (Task 1)

---

### Task 1: Lifecycle module and state-model fields

**Files:**
- Create: `src/regie/lifecycle.py`
- Modify: `src/regie/models.py` (new `Criterion`, `Increment`, `Spec`, `Question`; new `RunState` fields; `RunStage` gains the new states)
- Modify: `pyproject.toml`, `uv.lock` (dev dependency `hypothesis`)
- Test: `tests/test_lifecycle.py` (create)

**Interfaces:**
- Produces: `IllegalTransition(ValueError)`; `REQUEST_TRANSITIONS: dict[tuple[str, str], str]`; `HALTABLE: frozenset[str]`; `REQUEST_STATES: frozenset[str]`; `INCREMENT_TRANSITIONS`; `transition(rundir: RunDir | None, run: RunState, event: str, *, reason: str | None = None) -> str`; `transition_increment(rundir: RunDir | None, run: RunState, increment_id: str, event: str) -> str`.
- Produces (models): `Criterion(id, text)`, `Increment(id, title, goal, criteria, state, task_ids)`, `Spec(goal, increments, out_of_scope, assumptions, retired_criteria, revision)`, `Question(id, round, question, why, default, answer, defaulted)` with property `open`; `RunState.schema_version`, `.spec`, `.questions`, `.spec_feedback`, `.halted_from`.

Request events: `questions_asked`, `answered`, `spec_ready`, `spec_ready_fast`, `spec_approved`, `changes_requested`, `increments_done`, `finalized_for_pr`, `finalized_locally`, `pr_ready`, `reflected`, plus `halt` and `resume`. Increment events: `spec_accepted`, `build_started`, `completed`.

- [ ] **Step 1: Add Hypothesis**

Run: `uv add --dev hypothesis`
Expected: `pyproject.toml`'s dev dependency group lists `hypothesis`; `uv.lock` updated.

- [ ] **Step 2: Write the failing tests** — `tests/test_lifecycle.py`:

```python
from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from regie.lifecycle import (
    HALTABLE,
    INCREMENT_TRANSITIONS,
    REQUEST_STATES,
    REQUEST_TRANSITIONS,
    IllegalTransition,
    transition,
    transition_increment,
)
from regie.models import Increment, RunState, Spec
from regie.rundir import RunDir

REQUEST_EVENTS = sorted({event for _, event in REQUEST_TRANSITIONS} | {"halt", "resume"})
INCREMENT_EVENTS = sorted({event for _, event in INCREMENT_TRANSITIONS})


def _run(stage: str = "intake") -> RunState:
    return RunState(id="r", target_repo="", branch="", stage=stage)


def _with_increment() -> RunState:
    run = _run("active")
    run.spec = Spec(goal="g", increments=[Increment(id="INC-1", title="t", goal="g")])
    return run


def test_a_request_walks_from_intake_to_done():
    run = _run()
    for event in ("questions_asked", "answered", "spec_ready", "spec_approved",
                  "increments_done", "finalized_for_pr", "pr_ready", "reflected"):
        transition(None, run, event)

    assert run.stage == "done"


def test_an_illegal_event_raises_and_changes_nothing():
    run = _run()

    with pytest.raises(IllegalTransition):
        transition(None, run, "spec_approved")

    assert run.stage == "intake"


def test_halt_remembers_where_to_resume():
    run = _run("active")

    transition(None, run, "halt", reason="build ladder exhausted on T1")
    assert (run.stage, run.halted_from, run.halt_reason) == (
        "halted", "active", "build ladder exhausted on T1")
    transition(None, run, "resume")

    assert (run.stage, run.halted_from, run.halt_reason) == ("active", None, None)


def test_a_second_halt_keeps_the_first_reason():
    run = _run("active")
    transition(None, run, "halt", reason="first")

    transition(None, run, "halt", reason="second")

    assert (run.stage, run.halted_from, run.halt_reason) == ("halted", "active", "first")


def test_done_is_terminal():
    for event in REQUEST_EVENTS:
        run = _run("done")
        with pytest.raises(IllegalTransition):
            transition(None, run, event)


def test_transitions_are_recorded_and_persisted(tmp_path):
    rundir = RunDir.create(tmp_path, "r")
    run = _run()

    transition(rundir, run, "questions_asked")

    event = json.loads((rundir.path / "events.jsonl").read_text().splitlines()[-1])
    assert {key: event[key] for key in ("kind", "entity", "event", "from", "to")} == {
        "kind": "transition", "entity": "request", "event": "questions_asked",
        "from": "intake", "to": "awaiting-answers"}
    assert rundir.read_state().stage == "awaiting-answers"


@given(st.lists(st.sampled_from(REQUEST_EVENTS), max_size=40))
def test_any_event_sequence_keeps_the_request_legal(events):
    run = _run()
    for event in events:
        before = (run.stage, run.halted_from, run.halt_reason)
        try:
            transition(None, run, event, reason="why")
        except IllegalTransition:
            assert (run.stage, run.halted_from, run.halt_reason) == before
            continue
        if event == "halt" and before[0] == "halted":
            assert (run.stage, run.halted_from, run.halt_reason) == before
        elif event == "halt":
            assert before[0] in HALTABLE and run.halted_from == before[0]
        elif event == "resume":
            assert before[0] == "halted" and run.stage == before[1]
        else:
            assert REQUEST_TRANSITIONS[(before[0], event)] == run.stage
        assert run.stage in REQUEST_STATES


def test_an_increment_walks_from_draft_to_accepted():
    run = _with_increment()
    for event in ("spec_accepted", "build_started", "completed"):
        transition_increment(None, run, "INC-1", event)

    assert run.spec.increments[0].state == "accepted"


@given(st.lists(st.sampled_from(INCREMENT_EVENTS), max_size=20))
def test_any_increment_sequence_follows_the_table(events):
    run = _with_increment()
    increment = run.spec.increments[0]
    for event in events:
        before = increment.state
        try:
            transition_increment(None, run, "INC-1", event)
        except IllegalTransition:
            assert increment.state == before
            continue
        assert INCREMENT_TRANSITIONS[(before, event)] == increment.state


def test_an_unknown_increment_is_illegal():
    with pytest.raises(IllegalTransition):
        transition_increment(None, _with_increment(), "INC-9", "spec_accepted")
```

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run pytest -q tests/test_lifecycle.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'regie.lifecycle'`.

- [ ] **Step 4: Implement**

In `src/regie/models.py`, extend `RunStage` (the old values stay until Task 7 deletes the code that uses them) and add `IncrementState`:

```python
RunStage = Literal[
    "intake", "research", "plan", "approve", "tasks", "checkpoint",
    "finalize", "pr", "reflect", "done", "halted",
    "awaiting-answers", "awaiting-spec-approval", "active",
]
IncrementState = Literal["draft", "ready", "building", "accepted"]
```

Add, above `RunState`:

```python
class Criterion(BaseModel):
    """One acceptance criterion with its stable ID (`INC-1/AC-2`)."""

    id: str
    text: str


class Increment(BaseModel):
    """A vertical slice of value the stakeholder can see and react to."""

    id: str
    title: str
    goal: str
    criteria: list[Criterion] = Field(default_factory=list)
    state: IncrementState = "draft"
    task_ids: list[str] = Field(default_factory=list)


class Spec(BaseModel):
    """The Product Owner's spec: what the stakeholder approves."""

    goal: str
    increments: list[Increment]
    out_of_scope: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    retired_criteria: list[str] = Field(default_factory=list)
    revision: int = 1


class Question(BaseModel):
    """A question only the stakeholder can answer. `round` 0 means a developer
    (or the recovery Product Owner) asked it while building; it has no default."""

    id: str
    round: int
    question: str
    why: str = ""
    default: str = ""
    answer: str | None = None
    defaulted: bool = False

    @property
    def open(self) -> bool:
        return self.answer is None and not self.defaulted
```

In `RunState`, add after `stage`:

```python
    schema_version: int = 2
    spec: Spec | None = None
    questions: list[Question] = Field(default_factory=list)
    spec_feedback: list[str] = Field(default_factory=list)
    # The state a halted request returns to on resume.
    halted_from: str | None = None
```

Create `src/regie/lifecycle.py`:

```python
"""The framework's lifecycle rules: which event may move a request or an
increment from one state to the next. Lifecycle fields change only here."""
from __future__ import annotations

from regie.models import RunState
from regie.rundir import RunDir


class IllegalTransition(ValueError):
    """An event the lifecycle table does not allow from the current state."""


REQUEST_TRANSITIONS: dict[tuple[str, str], str] = {
    ("intake", "questions_asked"): "awaiting-answers",
    ("awaiting-answers", "answered"): "intake",
    ("intake", "spec_ready"): "awaiting-spec-approval",
    ("intake", "spec_ready_fast"): "active",
    ("awaiting-spec-approval", "spec_approved"): "active",
    ("awaiting-spec-approval", "changes_requested"): "intake",
    ("active", "increments_done"): "finalize",
    ("finalize", "finalized_for_pr"): "pr",
    ("finalize", "finalized_locally"): "reflect",
    ("pr", "pr_ready"): "reflect",
    ("reflect", "reflected"): "done",
}
# States the engine works in; waiting for the stakeholder is not halting.
HALTABLE = frozenset({"intake", "active", "finalize", "pr", "reflect"})
REQUEST_STATES = frozenset(
    {state for (before, _), after in REQUEST_TRANSITIONS.items()
     for state in (before, after)} | {"halted"})

INCREMENT_TRANSITIONS: dict[tuple[str, str], str] = {
    ("draft", "spec_accepted"): "ready",
    ("ready", "build_started"): "building",
    ("building", "completed"): "accepted",
}


def _record(rundir: RunDir | None, run: RunState, entity: str, event: str,
            before: str, after: str, reason: str | None = None) -> None:
    if rundir is None:
        return
    record = {"kind": "transition", "entity": entity, "event": event,
              "from": before, "to": after}
    if reason:
        record["reason"] = reason
    rundir.append_event(record)
    rundir.write_state(run)


def transition(rundir: RunDir | None, run: RunState, event: str, *,
               reason: str | None = None) -> str:
    """Apply one request event and return the new state. `halt` from any
    working state remembers it for `resume`; a second halt keeps the first."""
    before = run.stage
    if event == "halt":
        if before == "halted":
            return before
        if before not in HALTABLE:
            raise IllegalTransition(f"request: cannot halt while {before!r}")
        run.halted_from, run.halt_reason, run.stage = before, reason, "halted"
    elif event == "resume":
        if before != "halted" or run.halted_from is None:
            raise IllegalTransition(f"request: nothing to resume from {before!r}")
        run.stage, run.halted_from, run.halt_reason = run.halted_from, None, None
    else:
        target = REQUEST_TRANSITIONS.get((before, event))
        if target is None:
            raise IllegalTransition(f"request: no {event!r} from {before!r}")
        run.stage = target
    _record(rundir, run, "request", event, before, run.stage, reason)
    return run.stage


def transition_increment(rundir: RunDir | None, run: RunState, increment_id: str,
                         event: str) -> str:
    """Apply one increment event and return the increment's new state."""
    increment = next((item for item in (run.spec.increments if run.spec else [])
                      if item.id == increment_id), None)
    if increment is None:
        raise IllegalTransition(f"increment: no {increment_id!r} in the spec")
    target = INCREMENT_TRANSITIONS.get((increment.state, event))
    if target is None:
        raise IllegalTransition(
            f"increment {increment_id}: no {event!r} from {increment.state!r}")
    before, increment.state = increment.state, target
    _record(rundir, run, f"increment:{increment_id}", event, before, target)
    return target
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest -q tests/test_lifecycle.py && uv run pytest -q && uvx ruff check src tests`
Expected: 10 passed in `test_lifecycle.py`; full suite green; `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/regie/lifecycle.py src/regie/models.py pyproject.toml uv.lock tests/test_lifecycle.py
git commit -F - <<'EOF'
feat(lifecycle): explicit request and increment transition tables

Every request and increment state change goes through one function that
rejects events the table does not allow and records the transition. A
halt remembers where to resume; a second halt keeps the first reason.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RbtkGAHGVyLqrM25Tdxw2w
EOF
```

---

### Task 2: Framework rules — Definition of Ready, stable criterion IDs, spec rendering

**Files:**
- Create: `src/regie/framework.py`
- Test: `tests/test_framework.py` (create)

**Interfaces:**
- Consumes: Task 1's `Spec`, `Increment`, `Criterion`, `Question`.
- Produces: `CRITERION_RE`; `MAX_INCREMENTS = 1`; `MAX_QUESTION_ROUNDS = 3`; `build_spec(raw: dict, previous: Spec | None) -> Spec`; `spec_problems(spec: Spec, questions: list[Question]) -> list[str]`; `all_assumptions(spec, questions) -> list[str]`; `render_spec(spec, questions) -> str`.

`raw` is the Product Owner's draft: `{"goal": str, "increments": [{"title", "goal", "criteria": [str]}], "out_of_scope": [str], "assumptions": [str]}`.

- [ ] **Step 1: Write the failing tests** — `tests/test_framework.py`:

```python
from __future__ import annotations

from regie.framework import all_assumptions, build_spec, render_spec, spec_problems
from regie.models import Criterion, Increment, Question

GWT = "Given a catalog of 25 titles, When I open page 3, Then I see 5 titles"


def _raw(criteria, **overrides):
    return {"goal": "Every title is reachable", "out_of_scope": [], "assumptions": [],
            "increments": [{"title": "Reach the last page", "goal": "Fix paging",
                            "criteria": criteria}], **overrides}


def test_criteria_get_increment_scoped_ids():
    spec = build_spec(_raw([GWT, "Given x, When y, Then z"]), None)

    assert [c.id for c in spec.increments[0].criteria] == ["INC-1/AC-1", "INC-1/AC-2"]
    assert (spec.increments[0].id, spec.revision) == ("INC-1", 1)


def test_ids_stay_stable_and_retired_ids_are_never_reused():
    first = build_spec(_raw(["Given a, When b, Then c", "Given d, When e, Then f"]), None)
    second = build_spec(_raw(["Given  D, when e, then F", "Given g, When h, Then i"]), first)
    third = build_spec(_raw(["Given d, When e, Then f", "Given j, When k, Then l"]), second)

    assert [c.id for c in second.increments[0].criteria] == ["INC-1/AC-2", "INC-1/AC-3"]
    assert second.retired_criteria == ["INC-1/AC-1"]
    assert [c.id for c in third.increments[0].criteria] == ["INC-1/AC-2", "INC-1/AC-4"]
    assert third.retired_criteria == ["INC-1/AC-1", "INC-1/AC-3"]
    assert third.revision == 3


def test_a_ready_spec_has_no_problems():
    assert spec_problems(build_spec(_raw([GWT]), None), []) == []


def test_definition_of_ready_names_every_problem():
    spec = build_spec(_raw(["It works"], goal=" "), None)
    spec.increments.append(Increment(id="INC-2", title="More", goal="g", criteria=[
        Criterion(id="INC-2/AC-1", text=GWT)]))
    open_question = Question(id="Q1", round=1, question="Which?", default="This")

    assert spec_problems(spec, [open_question]) == [
        "the spec has no product goal",
        "this version of Régie delivers one increment per request; the spec has 2",
        "INC-1/AC-1 is not written as Given/When/Then: It works",
        "open questions: Q1",
    ]


def test_an_increment_without_criteria_is_not_ready():
    assert spec_problems(build_spec(_raw([]), None), []) == [
        "INC-1 has no acceptance criteria"]


def test_defaulted_questions_become_visible_assumptions():
    spec = build_spec(_raw([GWT], assumptions=["Page size stays 20"]), None)
    questions = [Question(id="Q1", round=1, question="Clamp past the end?",
                          default="No, return an empty page", defaulted=True),
                 Question(id="Q2", round=1, question="Keep the pager?", answer="Yes")]

    assert all_assumptions(spec, questions) == [
        "Page size stays 20",
        "Q1: Clamp past the end? — assumed: No, return an empty page"]


def test_the_rendered_spec_shows_ids_assumptions_and_decisions():
    spec = build_spec(_raw([GWT], out_of_scope=["New page sizes"]), None)
    questions = [Question(id="Q1", round=1, question="Keep the pager?", answer="Yes"),
                 Question(id="Q2", round=1, question="Clamp?", default="No",
                          defaulted=True)]

    text = render_spec(spec, questions)

    assert text.startswith("# Every title is reachable\n")
    assert f"- **INC-1/AC-1** {GWT}" in text
    assert "## Out of scope\n\n- New page sizes" in text
    assert "## Assumptions — confirm or correct\n\n- Q2: Clamp? — assumed: No" in text
    assert "## Decisions\n\n- Q1: Keep the pager? — Yes" in text
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest -q tests/test_framework.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'regie.framework'`.

- [ ] **Step 3: Implement** — `src/regie/framework.py`:

```python
"""The framework's mechanical rules: Definition of Ready and spec identity."""
from __future__ import annotations

import re
from typing import Any

from regie.models import Criterion, Increment, Question, Spec

CRITERION_RE = re.compile(r"\bgiven\b.+\bwhen\b.+\bthen\b", re.IGNORECASE | re.DOTALL)
# Multi-increment specs arrive with increment review and feedback (Phase 3).
MAX_INCREMENTS = 1
MAX_QUESTION_ROUNDS = 3


def _normalized(text: str) -> str:
    return " ".join(text.split()).casefold()


def build_spec(raw: dict[str, Any], previous: Spec | None) -> Spec:
    """Turn the Product Owner's draft into a Spec with stable criterion IDs:
    an unchanged criterion keeps its ID, a new one takes the next number, and
    a removed ID is retired and never reused."""
    retired = list(previous.retired_criteria) if previous else []
    increments = []
    for position, raw_increment in enumerate(raw["increments"], 1):
        increment_id = f"INC-{position}"
        prior: dict[str, str] = {}
        if previous is not None and position <= len(previous.increments):
            prior = {_normalized(c.text): c.id
                     for c in previous.increments[position - 1].criteria}
        prefix = f"{increment_id}/AC-"
        numbers = [int(cid.removeprefix(prefix)) for cid in [*prior.values(), *retired]
                   if cid.startswith(prefix)]
        next_number = max(numbers, default=0) + 1
        criteria: list[Criterion] = []
        kept: set[str] = set()
        for text in raw_increment["criteria"]:
            text = text.strip()
            if not text:
                continue
            cid = prior.get(_normalized(text))
            if cid is None or cid in kept:
                cid = f"{prefix}{next_number}"
                next_number += 1
            kept.add(cid)
            criteria.append(Criterion(id=cid, text=text))
        retired += [cid for cid in prior.values() if cid not in kept and cid not in retired]
        increments.append(Increment(id=increment_id, title=raw_increment["title"].strip(),
                                    goal=raw_increment["goal"].strip(), criteria=criteria))
    return Spec(
        goal=raw["goal"].strip(),
        increments=increments,
        out_of_scope=[item.strip() for item in raw["out_of_scope"] if item.strip()],
        assumptions=[item.strip() for item in raw["assumptions"] if item.strip()],
        retired_criteria=retired,
        revision=previous.revision + 1 if previous else 1,
    )


def spec_problems(spec: Spec, questions: list[Question]) -> list[str]:
    """The spec Definition of Ready; an empty list means the spec is Ready."""
    problems = []
    if not spec.goal:
        problems.append("the spec has no product goal")
    if not spec.increments:
        problems.append("the spec has no increment")
    if len(spec.increments) > MAX_INCREMENTS:
        problems.append("this version of Régie delivers one increment per request; "
                        f"the spec has {len(spec.increments)}")
    for increment in spec.increments:
        if not increment.title:
            problems.append(f"{increment.id} has no title")
        if not increment.criteria:
            problems.append(f"{increment.id} has no acceptance criteria")
        for criterion in increment.criteria:
            if not CRITERION_RE.search(criterion.text):
                problems.append(
                    f"{criterion.id} is not written as Given/When/Then: {criterion.text}")
    open_ids = [question.id for question in questions if question.open]
    if open_ids:
        problems.append("open questions: " + ", ".join(open_ids))
    return problems


def all_assumptions(spec: Spec, questions: list[Question]) -> list[str]:
    """The Product Owner's assumptions plus every default the stakeholder left."""
    defaults = [f"{q.id}: {q.question} — assumed: {q.default}"
                for q in questions if q.defaulted]
    return [*spec.assumptions, *defaults]


def render_spec(spec: Spec, questions: list[Question]) -> str:
    """The stakeholder-facing spec; it also travels with the PR."""
    title = (spec.goal.splitlines() or ["Spec"])[0]
    lines = [f"# {title}", "", "## Goal", "", spec.goal, ""]
    for increment in spec.increments:
        lines += [f"## {increment.id} — {increment.title}", "", increment.goal, "",
                  "### Acceptance criteria", ""]
        lines += [f"- **{c.id}** {c.text}" for c in increment.criteria]
        lines.append("")
    if spec.out_of_scope:
        lines += ["## Out of scope", "", *[f"- {item}" for item in spec.out_of_scope], ""]
    assumptions = all_assumptions(spec, questions)
    if assumptions:
        lines += ["## Assumptions — confirm or correct", "",
                  *[f"- {item}" for item in assumptions], ""]
    answered = [q for q in questions if q.answer is not None]
    if answered:
        lines += ["## Decisions", "",
                  *[f"- {q.id}: {q.question} — {q.answer}" for q in answered], ""]
    return "\n".join(lines).rstrip() + "\n"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q tests/test_framework.py && uvx ruff check src tests`
Expected: 7 passed; `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add src/regie/framework.py tests/test_framework.py
git commit -F - <<'EOF'
feat(framework): spec Definition of Ready and stable criterion IDs

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RbtkGAHGVyLqrM25Tdxw2w
EOF
```

---

### Task 3: Product Owner intake

**Files:**
- Create: `src/regie/intake.py`
- Modify: `profiles/product-owner.md` (intake becomes the Product Owner's main job; recovery stays until Phase 2)
- Modify: `tests/conftest.py` (fixture `po_profiles`)
- Test: `tests/test_intake.py` (create)

**Interfaces:**
- Consumes: `transition` (Task 1); `build_spec`, `spec_problems`, `render_spec`, `MAX_QUESTION_ROUNDS` (Task 2); from `regie.pipeline`: `_attempt_from(result, binding, profile) -> Attempt`, `_budget_reason(run, cfg) -> str | None`, `_conventions(repo) -> str`, `_failure_signature(kind, text) -> str`, `_PRODUCT_OWNER_TASK_ID`; `dispatch.run_agent(rundir, task_id, stage, attempt_no, req)`; `research.research_repository(rundir, repo)`; `knowledge.prime(rundir, repo, None, work_type)`; `packets.write_packet(task_dir, content)`.
- Produces: `INTAKE_SCHEMA`; `MAX_DRAFTS = 3`; `NO_DEFAULT = "no preference — the Product Owner decides"`; `intake_stage(rundir, run, cfg, worktree) -> None` (ends in `awaiting-answers`, `awaiting-spec-approval`, `active`, or `halted`; writes `spec/spec.md` when the spec is Ready and `spec/spec.diff` when a revision changed it). Dispatches use task `PRODUCT-OWNER`, stage `product-owner`, and are recorded in `run.product_owner_attempts` (shared numbering with recovery, so attempt files never collide).

- [ ] **Step 1: Add the profile fixture** — append to `tests/conftest.py`:

```python
@pytest.fixture
def po_profiles(fake_profiles) -> Path:
    """fake_profiles plus a Product Owner on the fake adapter."""
    (fake_profiles / "product-owner.yaml").write_text(
        "bindings:\n  - { cli: fake, model: po }\n"
        "budgets: { turns: 5, wall_minutes: 1, stall_minutes: 1 }\n")
    (fake_profiles / "product-owner.md").write_text("You are the Product Owner.")
    return fake_profiles
```

- [ ] **Step 2: Write the failing tests** — `tests/test_intake.py`:

```python
from __future__ import annotations

import pytest

from regie.agents.base import AgentResult
from regie.config import load_config
from regie.intake import intake_stage
from regie.models import Attempt, Binding, Question, RunState, UsageMetrics
from regie.rundir import RunDir

GWT = "Given a catalog of 25 titles, When I open page 3, Then I see the last 5"
TOTAL = "Given a catalog of 25 titles, When I open any page, Then I see the total"
QUESTION = {"question": "Clamp a page past the end?", "why": "Changes the API",
            "default": "No, return an empty page"}


def _draft(criteria=(GWT,), assumptions=()):
    return {"goal": "Every title is reachable", "out_of_scope": [],
            "assumptions": list(assumptions),
            "increments": [{"title": "Reach the last page", "goal": "Fix paging",
                            "criteria": list(criteria)}]}


def _answer(questions=(), **draft):
    return {"outcome": "done", "structured": {"questions": list(questions),
                                              "spec": _draft(**draft)}}


@pytest.fixture
def setup(regie_home, fixture_repo, po_profiles, monkeypatch):
    (fixture_repo / "regie.toml").write_text(
        'test_globs = ["tests/**"]\n[workflow]\nknowledge = false\n'
        '[commands]\ntest = "true"\nlint = "true"\n')
    cfg = load_config(fixture_repo, po_profiles)
    rundir = RunDir.create(regie_home, "r1")
    (rundir.path / "brief.md").write_text("The last catalog page can't be reached.\n")
    run = RunState(id="r1", target_repo=str(fixture_repo), branch="regie/r1")
    packets: list[str] = []
    queue: list[dict] = []

    def fake_run_agent(_rundir, task_id, stage, _attempt, req):
        assert (task_id, stage) == ("PRODUCT-OWNER", "product-owner")
        packets.append(req.prompt)
        return AgentResult(**queue.pop(0))

    monkeypatch.setattr("regie.intake.run_agent", fake_run_agent)
    return rundir, run, cfg, fixture_repo, queue, packets


def test_questions_wait_for_the_stakeholder(setup):
    rundir, run, cfg, repo, queue, _ = setup
    queue.append(_answer([QUESTION]))

    intake_stage(rundir, run, cfg, repo)

    assert run.stage == "awaiting-answers"
    assert [(q.id, q.round, q.default) for q in run.questions] == [
        ("Q1", 1, "No, return an empty page")]
    assert run.spec.increments[0].criteria[0].id == "INC-1/AC-1"
    assert len(run.product_owner_attempts) == 1


def test_a_question_without_a_default_still_gets_one(setup):
    rundir, run, cfg, repo, queue, _ = setup
    queue.append(_answer([{**QUESTION, "default": " "}]))

    intake_stage(rundir, run, cfg, repo)

    assert run.questions[0].default == "no preference — the Product Owner decides"


def test_answers_reach_the_next_draft_and_the_spec_waits_for_approval(setup):
    rundir, run, cfg, repo, queue, packets = setup
    queue += [_answer([QUESTION]), _answer()]
    intake_stage(rundir, run, cfg, repo)
    run.questions[0].answer = "Yes, clamp to the last page"
    run.stage = "intake"

    intake_stage(rundir, run, cfg, repo)

    assert "Q1: Clamp a page past the end? — answer: Yes, clamp to the last page" in packets[1]
    assert run.stage == "awaiting-spec-approval"
    assert "INC-1/AC-1" in (rundir.path / "spec" / "spec.md").read_text()


def test_a_clear_request_takes_the_fast_path(setup):
    rundir, run, cfg, repo, queue, _ = setup
    queue.append(_answer())

    intake_stage(rundir, run, cfg, repo)

    assert run.stage == "active"


def test_an_assumption_means_the_stakeholder_approves(setup):
    rundir, run, cfg, repo, queue, _ = setup
    queue.append(_answer(assumptions=["Page size stays 20"]))

    intake_stage(rundir, run, cfg, repo)

    assert run.stage == "awaiting-spec-approval"


def test_feedback_revises_the_spec_and_shows_the_change(setup):
    rundir, run, cfg, repo, queue, packets = setup
    queue += [_answer(assumptions=["Page size stays 20"]),
              _answer(criteria=(GWT, TOTAL), assumptions=["Page size stays 20"])]
    intake_stage(rundir, run, cfg, repo)
    run.spec_feedback = ["Also show the total"]
    run.stage = "intake"

    intake_stage(rundir, run, cfg, repo)

    assert "## Stakeholder feedback on the spec\n\n- Also show the total" in packets[1]
    assert run.stage == "awaiting-spec-approval"
    assert f"+- **INC-1/AC-2** {TOTAL}" in (rundir.path / "spec" / "spec.diff").read_text()


def test_autonomous_runs_take_defaults_and_skip_approval(setup):
    rundir, run, cfg, repo, queue, _ = setup
    run.autonomous = True
    queue.append(_answer([QUESTION]))

    intake_stage(rundir, run, cfg, repo)

    assert run.stage == "active"
    assert run.questions[0].defaulted is True
    assert "assumed: No, return an empty page" in (rundir.path / "spec" / "spec.md").read_text()


def test_question_rounds_are_capped(setup):
    rundir, run, cfg, repo, queue, _ = setup
    run.questions = [Question(id=f"Q{n}", round=n, question=f"q{n}", answer="a")
                     for n in (1, 2, 3)]
    queue.append(_answer([QUESTION]))

    intake_stage(rundir, run, cfg, repo)

    assert run.stage == "awaiting-spec-approval"
    assert (run.questions[-1].id, run.questions[-1].defaulted) == ("Q4", True)


def test_a_repeated_question_is_not_asked_again(setup):
    rundir, run, cfg, repo, queue, _ = setup
    run.questions = [Question(id="Q1", round=1, question="Clamp a page past the end?",
                              answer="No")]
    queue.append(_answer([QUESTION]))

    intake_stage(rundir, run, cfg, repo)

    assert [q.id for q in run.questions] == ["Q1"]
    assert run.stage == "awaiting-spec-approval"


def test_an_unready_spec_is_redrafted_with_its_problems(setup):
    rundir, run, cfg, repo, queue, packets = setup
    queue += [_answer(criteria=["It works"]), _answer()]

    intake_stage(rundir, run, cfg, repo)

    assert "INC-1/AC-1 is not written as Given/When/Then: It works" in packets[1]
    assert run.stage == "active"


def test_three_unready_drafts_halt(setup):
    rundir, run, cfg, repo, queue, _ = setup
    queue += [_answer(criteria=["It works"])] * 3

    intake_stage(rundir, run, cfg, repo)

    assert run.stage == "halted"
    assert run.halt_reason.startswith("the Product Owner's spec is not Ready after 3 drafts")


def test_an_unavailable_product_owner_halts(setup):
    rundir, run, cfg, repo, queue, _ = setup
    queue.append({"outcome": "error", "text": "boom"})

    intake_stage(rundir, run, cfg, repo)

    assert (run.stage, run.halt_reason) == ("halted", "the Product Owner is unavailable (error)")
    assert run.product_owner_attempts[0].outcome == "failed"


def test_a_malformed_response_counts_as_a_contract_failure(setup):
    rundir, run, cfg, repo, queue, _ = setup
    queue.append({"outcome": "done", "structured": {"questions": "none", "spec": {}}})

    intake_stage(rundir, run, cfg, repo)

    assert run.halt_reason == "the Product Owner is unavailable (malformed response)"
    attempt = run.product_owner_attempts[0]
    assert (attempt.outcome, attempt.failure_kind) == ("failed", "contract")


def test_an_exhausted_budget_halts_before_dispatch(setup):
    rundir, run, cfg, repo, queue, packets = setup
    cfg.workflow.max_run_usd = 1.0
    run.retired_attempts = [Attempt(binding=Binding(cli="fake", model="m"), outcome="done",
                                    metrics=UsageMetrics(cost_usd=2.0))]

    intake_stage(rundir, run, cfg, repo)

    assert run.stage == "halted"
    assert "budget" in run.halt_reason
    assert packets == []
```

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run pytest -q tests/test_intake.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'regie.intake'`.

- [ ] **Step 4: Implement** — `src/regie/intake.py`:

```python
"""Product Owner intake: a stakeholder request becomes a Ready spec."""
from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any

from regie.agents.base import AgentRequest
from regie.config import RegieConfig
from regie.dispatch import run_agent
from regie.framework import MAX_QUESTION_ROUNDS, build_spec, render_spec, spec_problems
from regie.knowledge import prime as prime_knowledge
from regie.lifecycle import transition
from regie.models import Question, RunState, Spec
from regie.packets import write_packet
from regie.pipeline import (
    _PRODUCT_OWNER_TASK_ID,
    _attempt_from,
    _budget_reason,
    _conventions,
    _failure_signature,
)
from regie.research import research_repository
from regie.rundir import RunDir

MAX_DRAFTS = 3  # Product Owner responses per intake pass before halting
NO_DEFAULT = "no preference — the Product Owner decides"

_STRING = {"type": "string", "maxLength": 800}
INTAKE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["questions", "spec"],
    "properties": {
        "questions": {"type": "array", "maxItems": 6, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["question", "why", "default"],
            "properties": {"question": _STRING, "why": _STRING, "default": _STRING}}},
        "spec": {
            "type": "object", "additionalProperties": False,
            "required": ["goal", "increments", "out_of_scope", "assumptions"],
            "properties": {
                "goal": _STRING,
                "increments": {"type": "array", "maxItems": 1, "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["title", "goal", "criteria"],
                    "properties": {
                        "title": {"type": "string", "maxLength": 160},
                        "goal": _STRING,
                        "criteria": {"type": "array", "maxItems": 12, "items": _STRING},
                    }}},
                "out_of_scope": {"type": "array", "items": _STRING},
                "assumptions": {"type": "array", "items": _STRING},
            }},
    },
}

_RULES = """## What to return

- `questions`: only what the stakeholder alone can answer and what would change the
  product. Never ask what the repository, the request, or an earlier answer already
  tells you. Each question has a `why` and the `default` you would choose. Ask
  everything in one batch, and ask nothing when the request is clear.
- `spec`: always your best current draft — one product `goal`; exactly one increment
  (a vertical slice of value) with a `title`, a `goal`, and acceptance `criteria`
  written as Given/When/Then that a reviewer can check against the code; an
  `out_of_scope` list; and an `assumptions` list naming every assumption you made.
"""


def _strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _well_formed(structured: Any) -> bool:
    if not isinstance(structured, dict):
        return False
    questions, spec = structured.get("questions"), structured.get("spec")
    if not isinstance(questions, list) or not isinstance(spec, dict):
        return False
    if not all(isinstance(q, dict) and all(isinstance(q.get(key), str)
                                           for key in ("question", "why", "default"))
               for q in questions):
        return False
    increments = spec.get("increments")
    return (isinstance(spec.get("goal"), str) and isinstance(increments, list)
            and all(isinstance(i, dict) and isinstance(i.get("title"), str)
                    and isinstance(i.get("goal"), str) and _strings(i.get("criteria"))
                    for i in increments)
            and _strings(spec.get("out_of_scope")) and _strings(spec.get("assumptions")))


def _draft(spec: Spec) -> dict[str, Any]:
    return {"goal": spec.goal, "out_of_scope": spec.out_of_scope,
            "assumptions": spec.assumptions,
            "increments": [{"title": i.title, "goal": i.goal,
                            "criteria": [c.text for c in i.criteria]}
                           for i in spec.increments]}


def _qa_line(question: Question) -> str:
    if question.answer is not None:
        outcome = f"answer: {question.answer}"
    elif question.defaulted:
        outcome = f"no answer; default taken: {question.default}"
    else:
        outcome = "waiting for the stakeholder"
    return f"- {question.id}: {question.question} — {outcome}"


def _packet(rundir: RunDir, run: RunState, worktree: Path, problems: list[str]) -> str:
    sections = ["# Stakeholder request\n\n" + (rundir.path / "brief.md").read_text().strip()]
    research = Path(run.research_path) if run.research_path else None
    if research is not None and research.is_file():
        sections.append("## Repository research\n\n" + research.read_text()[:6000])
    knowledge = rundir.path / "knowledge-prime.md"
    if knowledge.is_file():
        sections.append("## Project knowledge\n\n" + knowledge.read_text()[:3000])
    conventions = _conventions(worktree)
    if conventions:
        sections.append("## Repository conventions\n\n" + conventions[:6000])
    if run.spec is not None:
        sections.append("## Your current draft\n\n```json\n"
                        + json.dumps(_draft(run.spec), indent=2) + "\n```")
    if run.questions:
        sections.append("## Questions so far\n\n"
                        + "\n".join(_qa_line(q) for q in run.questions))
    if run.spec_feedback:
        sections.append("## Stakeholder feedback on the spec\n\n"
                        + "\n".join(f"- {item}" for item in run.spec_feedback))
    if problems:
        sections.append("## Your last draft is not Ready\n\n"
                        + "\n".join(f"- {item}" for item in problems))
    sections.append(_RULES)
    return "\n\n".join(sections) + "\n"


def _ask(rundir: RunDir, run: RunState, cfg: RegieConfig, worktree: Path,
         packet: str) -> tuple[dict[str, Any] | None, str]:
    profile = cfg.profiles.get("product-owner")
    if profile is None:
        return None, "the product-owner profile is not configured"
    write_packet(rundir.task_dir(_PRODUCT_OWNER_TASK_ID), packet)
    last = "unavailable"
    for binding in profile.bindings:
        result = run_agent(rundir, _PRODUCT_OWNER_TASK_ID, "product-owner",
                           len(run.product_owner_attempts) + 1,
                           AgentRequest(prompt=packet, instructions=profile.prompt_text(),
                                        cwd=worktree, binding=binding,
                                        budgets=profile.budgets,
                                        token_policy=profile.token_policy,
                                        output_schema=INTAKE_SCHEMA))
        attempt = _attempt_from(result, binding, profile)
        run.product_owner_attempts.append(attempt)
        if result.outcome == "done" and _well_formed(result.structured):
            rundir.write_state(run)
            return result.structured, ""
        if result.outcome == "done":
            attempt.outcome, attempt.failure_kind = "failed", "contract"
            attempt.failure_signature = _failure_signature(
                "contract", json.dumps(result.structured, sort_keys=True))
            last = "malformed response"
        else:
            last = result.outcome
        rundir.write_state(run)
    return None, f"the Product Owner is unavailable ({last})"


def _new_questions(run: RunState, raw_questions: list[dict[str, str]]) -> list[Question]:
    asked = {" ".join(q.question.split()).casefold() for q in run.questions}
    round_number = len({q.round for q in run.questions if q.round > 0}) + 1
    fresh = []
    for raw in raw_questions:
        text = raw["question"].strip()
        key = " ".join(text.split()).casefold()
        if not text or key in asked:
            continue
        asked.add(key)
        fresh.append(Question(id=f"Q{len(run.questions) + len(fresh) + 1}",
                              round=round_number, question=text, why=raw["why"].strip(),
                              default=raw["default"].strip() or NO_DEFAULT))
    return fresh


def _write_spec(rundir: RunDir, run: RunState) -> None:
    spec_dir = rundir.path / "spec"
    spec_dir.mkdir(parents=True, exist_ok=True)
    path = spec_dir / "spec.md"
    rendered = render_spec(run.spec, run.questions)
    if path.exists() and path.read_text() != rendered:
        (spec_dir / "spec.diff").write_text("".join(difflib.unified_diff(
            path.read_text().splitlines(keepends=True), rendered.splitlines(keepends=True),
            "previous spec", "revised spec")))
    path.write_text(rendered)


def intake_stage(rundir: RunDir, run: RunState, cfg: RegieConfig, worktree: Path) -> None:
    """Ask the Product Owner for a Ready spec. Ends waiting for the
    stakeholder's answers or approval, in `active` on the fast path, or halted."""
    if not run.research_path:
        research_repository(rundir, worktree)
        run.research_path = str(rundir.path / "research.md")
    if cfg.workflow.knowledge and not run.knowledge_snapshot:
        selected = prime_knowledge(rundir, worktree, None, "planning")
        run.knowledge_snapshot = [entry.id for entry in selected]
    rundir.write_state(run)
    problems: list[str] = []
    for _ in range(MAX_DRAFTS):
        budget = _budget_reason(run, cfg)
        if budget:
            transition(rundir, run, "halt", reason=budget)
            return
        structured, error = _ask(rundir, run, cfg, worktree,
                                 _packet(rundir, run, worktree, problems))
        if structured is None:
            transition(rundir, run, "halt", reason=error)
            return
        fresh = _new_questions(run, structured["questions"])
        run.spec = build_spec(structured["spec"], run.spec)
        rounds = len({q.round for q in run.questions if q.round > 0})
        if fresh and not run.autonomous and rounds < MAX_QUESTION_ROUNDS:
            run.questions.extend(fresh)
            transition(rundir, run, "questions_asked")
            return
        for question in fresh:
            question.defaulted = True
        run.questions.extend(fresh)
        problems = spec_problems(run.spec, run.questions)
        if problems:
            continue
        _write_spec(rundir, run)
        fast = not run.questions and not run.spec.assumptions and not run.spec_feedback
        transition(rundir, run, "spec_ready_fast" if fast or run.autonomous else "spec_ready")
        return
    transition(rundir, run, "halt", reason=(
        f"the Product Owner's spec is not Ready after {MAX_DRAFTS} drafts: "
        + "; ".join(problems)))
```

Replace `profiles/product-owner.md` with:

```markdown
You are Régie's Product Owner. A stakeholder brought a request; your job is to make
sure the team builds exactly what they want, and nothing they did not ask for.

## Intake (your main job)

Read the request, the repository research, and the repository itself before asking
anything. Then return:

- **Questions only the stakeholder can answer** — product choices that would change
  what gets built. Never ask what the code, the request, or an earlier answer already
  tells you. Give each question a short `why` and the `default` you would pick if the
  stakeholder does not answer. Ask everything at once; ask nothing when the request is
  clear.
- **Your best current spec**, every time: one product goal; exactly one increment —
  a vertical slice the stakeholder can see working — with a title, a goal, and
  acceptance criteria written as Given/When/Then that a reviewer can check against
  the code; what is out of scope; and every assumption you made, stated plainly.

Prefer the smallest increment that fully satisfies the request. Keep the
stakeholder's words and explicit non-goals. You cannot edit the repository.

## Recovery (only when the packet says so)

When implementation review fails to converge, you may be asked to resolve the
dispute. The original request and the accepted spec are the product authority.
Consolidate duplicate or contradictory findings and separate required scope from
optional improvement. Choose exactly one action:

- `revise`: concrete, non-conflicting directives for one final revision.
- `accept`: only when every remaining serious finding is advisory and you can reject
  it against the request and repository evidence — name and justify each. Failed
  acceptance criteria, mechanical failures, and reviewer unavailability cannot be
  accepted.
- `ask_human`: only when product intent, credentials, destructive effects, security
  posture, provider policy, or budget authority requires the stakeholder. Ask one
  concise question.
- `halt`: only when no permitted recovery can make progress.

You cannot waive tests, gates, security requirements, destructive-operation
approval, credentials, configured providers, or budgets. You cannot dispatch other
agents.

Return only the requested structured object.
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest -q tests/test_intake.py && uv run pytest -q && uvx ruff check src tests`
Expected: 14 passed in `test_intake.py`; full suite green; `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/regie/intake.py profiles/product-owner.md tests/conftest.py tests/test_intake.py
git commit -F - <<'EOF'
feat(intake): Product Owner interviews the stakeholder into a Ready spec

Questions arrive in batches with defaults (at most three rounds, never
repeated), every response carries a full draft, and a Ready spec either
waits for approval or takes the fast path. Autonomous runs take defaults;
a revised spec records its diff.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RbtkGAHGVyLqrM25Tdxw2w
EOF
```

---

### Task 4: Stakeholder decisions — `regie inbox` and `regie decide`

**Files:**
- Modify: `src/regie/operations.py` (add `InboxItem`, `pending_items`, `decide`, `continue_after_answers`; the old functions stay until Task 7)
- Modify: `src/regie/cli.py` (commands `inbox`, `decide`)
- Test: `tests/test_operations.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `transition` (Task 1); `Question`, `RunState`.
- Produces: `InboxItem(id: str, kind: str, title: str, detail: str)`; `pending_items(run) -> list[InboxItem]`; `decide(rundir, run, item: str, decision: str, text: str | None = None) -> str` (raises `ValueError` with a message for any invalid decision); `continue_after_answers(rundir, run) -> list[str]` (ids that took their default); CLI `regie inbox RUN` and `regie decide RUN ITEM DECISION [TEXT]`.

Decisions: a question `Qn` takes `answer TEXT` or `default`; `spec` takes `approve` or `changes TEXT`. Every question decision is appended to the run's `decisions.md` (developers see it in their packets) and every decision is recorded as a `stakeholder_decision` event.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_operations.py` (keep the existing tests; Task 7 removes them with the old functions):

```python
def _waiting(regie_home, stage="awaiting-answers"):
    rundir = RunDir.create(regie_home, "r1")
    run = RunState(id="r1", target_repo="", branch="", stage=stage, questions=[
        Question(id="Q1", round=1, question="Clamp?", why="API", default="No"),
        Question(id="Q2", round=0, question="Which file?", why="dev")])
    return rundir, run


def test_open_questions_and_the_spec_are_inbox_items(regie_home):
    _rundir, run = _waiting(regie_home, stage="awaiting-spec-approval")

    assert [(i.id, i.kind) for i in pending_items(run)] == [
        ("Q1", "question"), ("Q2", "question"), ("spec", "spec")]


def test_an_answer_is_recorded_for_the_team(regie_home):
    rundir, run = _waiting(regie_home)

    decide(rundir, run, "Q1", "answer", "Yes, clamp")

    assert run.questions[0].answer == "Yes, clamp"
    assert "Question: Clamp?\n\nAnswer: Yes, clamp" in (rundir.path / "decisions.md").read_text()


def test_a_default_can_be_taken_only_when_there_is_one(regie_home):
    rundir, run = _waiting(regie_home)

    decide(rundir, run, "Q1", "default")

    assert run.questions[0].defaulted is True
    with pytest.raises(ValueError, match="has no default"):
        decide(rundir, run, "Q2", "default")


@pytest.mark.parametrize("item,decision,text,message", [
    ("Q9", "answer", "x", "no inbox item"),
    ("Q1", "answer", " ", "needs text"),
    ("Q1", "maybe", None, "take 'answer' or 'default'"),
    ("spec", "approve", None, "not awaiting approval"),
])
def test_invalid_decisions_are_refused(regie_home, item, decision, text, message):
    rundir, run = _waiting(regie_home)

    with pytest.raises(ValueError, match=message):
        decide(rundir, run, item, decision, text)


def test_approving_the_spec_activates_the_request(regie_home):
    rundir, run = _waiting(regie_home, stage="awaiting-spec-approval")

    decide(rundir, run, "spec", "approve")

    assert rundir.read_state().stage == "active"


def test_requested_changes_go_back_to_the_product_owner(regie_home):
    rundir, run = _waiting(regie_home, stage="awaiting-spec-approval")

    decide(rundir, run, "spec", "changes", "Also show the total")

    assert (run.stage, run.spec_feedback) == ("intake", ["Also show the total"])


def test_continuing_takes_defaults_but_leaves_developer_questions_open(regie_home):
    rundir, run = _waiting(regie_home)

    assert continue_after_answers(rundir, run) == ["Q1"]
    assert (run.stage, run.questions[0].defaulted, run.questions[1].open) == (
        "intake", True, True)
```

with these imports in the file's import block: `import pytest`, `from regie.models import Question, RunState`, `from regie.operations import continue_after_answers, decide, pending_items`, `from regie.rundir import RunDir` (merge with what is already imported).

Append to `tests/test_cli.py` (add `Question` to its `regie.models` import):

```python
def test_inbox_and_decide_from_the_command_line(regie_home):
    rundir = RunDir.create(regie_home, "r1")
    run = RunState(id="r1", target_repo="/repo", branch="", stage="awaiting-answers",
                   questions=[Question(id="Q1", round=1, question="Clamp?", why="API",
                                       default="No")])
    rundir.write_state(run)

    listed = runner.invoke(app, ["inbox", "r1"])
    answered = runner.invoke(app, ["decide", "r1", "Q1", "answer", "Yes"])
    refused = runner.invoke(app, ["decide", "r1", "Q1", "answer", "Again"])

    assert listed.exit_code == 0 and "Q1" in listed.output and "default: No" in listed.output
    assert answered.exit_code == 0 and "Q1: answer recorded" in answered.output
    assert "regie resume r1 --repo /repo" in answered.output
    assert refused.exit_code == 2 and "already decided" in refused.output
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest -q tests/test_operations.py tests/test_cli.py::test_inbox_and_decide_from_the_command_line`
Expected: FAIL — `ImportError: cannot import name 'continue_after_answers'`.

- [ ] **Step 3: Implement** — append to `src/regie/operations.py` (add `from dataclasses import dataclass` and `from regie.lifecycle import transition` to the imports; add `Question` to the `regie.models` import):

```python
@dataclass(frozen=True)
class InboxItem:
    """Something waiting for the stakeholder."""

    id: str
    kind: str
    title: str
    detail: str = ""


def pending_items(run: RunState) -> list[InboxItem]:
    items = [InboxItem(q.id, "question", q.question,
                       f"why: {q.why}\ndefault: {q.default or '(none — please answer)'}")
             for q in run.questions if q.open]
    if run.stage == "awaiting-spec-approval":
        detail = "read spec/spec.md in the run directory"
        if run.spec_feedback:
            detail += "; spec/spec.diff shows what changed after your feedback"
        items.append(InboxItem("spec", "spec", "Approve the spec or request changes",
                               detail))
    return items


def _record_answer(rundir: RunDir, question: Question) -> None:
    path = rundir.path / "decisions.md"
    prior = path.read_text().rstrip() if path.exists() else "# Stakeholder decisions"
    outcome = (f"Answer: {question.answer}" if question.answer is not None
               else f"Default taken: {question.default}")
    path.write_text(f"{prior}\n\n## {question.id} ({datetime.now(UTC).isoformat()})\n\n"
                    f"Question: {question.question}\n\n{outcome}\n")


def decide(rundir: RunDir, run: RunState, item: str, decision: str,
           text: str | None = None) -> str:
    """Record one stakeholder decision and return a confirmation line."""
    if item == "spec":
        if run.stage != "awaiting-spec-approval":
            raise ValueError(f"the spec is not awaiting approval (stage={run.stage})")
        if decision == "approve":
            transition(rundir, run, "spec_approved")
        elif decision == "changes":
            if not (text and text.strip()):
                raise ValueError("requesting changes needs feedback text")
            run.spec_feedback.append(text.strip())
            transition(rundir, run, "changes_requested")
        else:
            raise ValueError(f"the spec takes 'approve' or 'changes', not {decision!r}")
    else:
        question = next((q for q in run.questions if q.id == item), None)
        if question is None:
            raise ValueError(f"no inbox item {item!r}")
        if not question.open:
            raise ValueError(f"{item} is already decided")
        if decision == "answer":
            if not (text and text.strip()):
                raise ValueError("an answer needs text")
            question.answer = text.strip()
        elif decision == "default":
            if not question.default:
                raise ValueError(f"{item} has no default; answer it")
            question.defaulted = True
        else:
            raise ValueError(f"questions take 'answer' or 'default', not {decision!r}")
        _record_answer(rundir, question)
        rundir.write_state(run)
    rundir.append_event({"kind": "stakeholder_decision", "item": item, "decision": decision})
    return f"{item}: {decision} recorded"


def continue_after_answers(rundir: RunDir, run: RunState) -> list[str]:
    """Resume intake: every still-open question with a default takes it."""
    defaulted = []
    for question in run.questions:
        if question.open and question.default:
            question.defaulted = True
            _record_answer(rundir, question)
            defaulted.append(question.id)
    transition(rundir, run, "answered")
    return defaulted
```

In `src/regie/cli.py` add the commands (after `answer`):

```python
@app.command()
def inbox(run_id: str):
    """List what is waiting for you on a run."""
    from regie.operations import pending_items

    state = _open_rundir(_home(), run_id).read_state()
    items = pending_items(state)
    if not items:
        typer.echo(f"nothing waiting for you (stage={state.stage})")
        return
    for item in items:
        typer.echo(f"{item.id}  {item.title}")
        for line in item.detail.splitlines():
            typer.echo(f"      {line}")


@app.command()
def decide(run_id: str, item: str, decision: str,
           text: Annotated[str | None, typer.Argument()] = None):
    """Record a decision: `Qn answer TEXT`, `Qn default`, `spec approve`,
    or `spec changes TEXT`."""
    from regie.operations import decide as record_decision

    rundir = _open_rundir(_home(), run_id)
    state = rundir.read_state()
    try:
        confirmation = record_decision(rundir, state, item, decision, text)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    typer.echo(confirmation)
    typer.echo(f"continue with `regie resume {run_id} --repo {state.target_repo}`")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q && uvx ruff check src tests`
Expected: all pass; `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add src/regie/operations.py src/regie/cli.py tests/test_operations.py tests/test_cli.py
git commit -F - <<'EOF'
feat(cli): regie inbox and regie decide for stakeholder decisions

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RbtkGAHGVyLqrM25Tdxw2w
EOF
```

---

### Task 5: Switch over — requests go through intake; the plan stage goes

**Files:**
- Modify: `src/regie/pipeline.py` (lifecycle transitions, `start_increment`, `apply_baseline_brief`, stakeholder questions from building, no `needs_planning`; delete the plan stage)
- Modify: `src/regie/cli.py` (`run`, `resume`, `_advance`, `_read_state`, `_print_inbox`; remove `--workflow`)
- Modify: `profiles/implementer.md` (every request's developer, no `needs_planning`)
- Delete: `tests/test_plan_stage.py`
- Test: `tests/test_e2e_full.py` (rewrite), plus the updates listed in Step 5

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces: `pipeline.DIRECT_CHECKLIST: tuple[str, ...]`; `pipeline.apply_baseline_brief(rundir, run, brief_text) -> None` (replaces `apply_direct_brief`); `pipeline.start_increment(rundir, run) -> None`; `pipeline._ask_stakeholder(run, question, why) -> None`; `cli._read_state(rundir) -> RunState` (refuses runs from before the redesign). The CLI request loop: `regie run` → intake (default), `--baseline`, or `--tasks-file`; `regie resume` resumes a halted request at `halted_from`, continues `awaiting-answers` with defaults, and points `awaiting-spec-approval` to the inbox.

- [ ] **Step 1: Rewrite the end-to-end test first** — replace `tests/test_e2e_full.py` entirely:

```python
"""The whole request lifecycle through the CLI: a Product Owner question,
the stakeholder's answer, spec approval, the build, and the PR — plus the
autonomous and fast paths.

Dispatches are canned by patching run_agent in both the intake and the
pipeline, deliberately NOT the fake adapter's file-based queue: the review
stage never commits, so finalize's discard step would revert the queue's
bookkeeping file and resurrect consumed entries (see test_finalize_pr.py)."""
import gc
import os
import stat
from datetime import UTC, datetime

from typer.testing import CliRunner

from regie import intake, pipeline
from regie.agents.base import AgentResult
from regie.cli import app
from regie.rundir import RunDir

runner = CliRunner()

ADD = "def add(a, b):\n    return a + b\n"
DIVIDE = "def divide(a, b):\n    return a // b\n"
TEST_DIV = "from src.calc import divide\n\ndef test_div():\n    assert divide(6, 3) == 2\n"
CRITERION = "Given 6 and 3, When divide is called, Then it returns 2"
QUESTION = {"question": "Integer or float division?", "why": "Changes results",
            "default": "Integer division"}


def _po(questions=()):
    return {"result": {"outcome": "done", "structured": {
        "questions": list(questions),
        "spec": {"goal": "Divide numbers", "out_of_scope": [], "assumptions": [],
                 "increments": [{"title": "divide", "goal": "Add divide",
                                 "criteria": [CRITERION]}]}}}}


BUILD = {"result": {"outcome": "done"},
         "writes": {"src/calc.py": ADD + "\n" + DIVIDE, "tests/test_div.py": TEST_DIV}}
REVIEW = {"result": {"outcome": "done", "structured": {"findings": [], "criterion_results": [
    {"criterion": f"INC-1/AC-1: {CRITERION}", "passed": True, "evidence": "test_div",
     "file": "src/calc.py", "line": 4}]}}}


def _stub_gh_green(tmp_path, monkeypatch):
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir(parents=True, exist_ok=True)
    gh.write_text("#!/bin/sh\n"
                  'if [ "$1 $2" = "pr create" ]; then echo "https://github.com/x/y/pull/1"; exit 0; fi\n'
                  'if [ "$1 $2" = "pr checks" ]; then echo "SUCCESS"; exit 0; fi\n'
                  'exit 1\n')
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{gh.parent}:{os.environ['PATH']}")


def _queue(monkeypatch, worktree, specs):
    queue = list(specs)

    def fake_run_agent(rundir, task_id, stage, attempt_no, req):
        spec = queue.pop(0)
        for rel, content in spec.get("writes", {}).items():
            path = worktree / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        return AgentResult(**spec["result"])

    monkeypatch.setattr(intake, "run_agent", fake_run_agent)
    monkeypatch.setattr(pipeline, "run_agent", fake_run_agent)
    monkeypatch.setattr(pipeline, "CI_POLL_SECONDS", 0)
    return queue


def _setup(fixture_repo, tmp_path, regie_home):
    (fixture_repo / "regie.toml").write_text(
        'test_globs = ["tests/**"]\n[workflow]\nknowledge = false\nreflection = false\n'
        '[commands]\ntest = "python -m pytest tests -q"\nlint = "true"\n')
    brief = tmp_path / "divide.md"
    brief.write_text("# Add a divide helper\n")
    run_id = f"{datetime.now(tz=UTC).date().isoformat()}-divide"
    return brief, run_id, regie_home / "worktrees" / run_id


def test_question_answer_approval_build_and_pr(regie_home, fixture_repo, remote_repo,
                                               po_profiles, tmp_path, monkeypatch):
    _stub_gh_green(tmp_path, monkeypatch)
    brief, run_id, worktree = _setup(fixture_repo, tmp_path, regie_home)
    queue = _queue(monkeypatch, worktree, [_po([QUESTION]), _po(), BUILD, REVIEW])
    args = ["--repo", str(fixture_repo), "--profiles", str(po_profiles)]

    result = runner.invoke(app, ["run", str(brief), *args])
    assert result.exit_code == 0, result.output
    assert "Q1" in result.output and "regie decide" in result.output
    # Drop the finished invocation before the next one takes the run's lock.
    del result
    gc.collect()
    assert runner.invoke(app, ["decide", run_id, "Q1", "answer", "Integer"]).exit_code == 0

    result = runner.invoke(app, ["resume", run_id, *args])
    assert result.exit_code == 0, result.output
    assert RunDir.open(regie_home, run_id).read_state().stage == "awaiting-spec-approval"
    del result
    gc.collect()
    assert runner.invoke(app, ["decide", run_id, "spec", "approve"]).exit_code == 0

    result = runner.invoke(app, ["resume", run_id, *args])
    assert result.exit_code == 0, result.output
    state = RunDir.open(regie_home, run_id).read_state()
    assert (state.stage, state.pr_url) == ("done", "https://github.com/x/y/pull/1")
    assert state.spec.increments[0].state == "accepted"
    assert state.tasks["T1"].spec.criteria == [f"INC-1/AC-1: {CRITERION}"]
    assert "INC-1/AC-1" in (RunDir.open(regie_home, run_id).path / "pr-body.md").read_text()
    assert queue == []


def test_autonomous_requests_run_in_one_invocation(regie_home, fixture_repo, remote_repo,
                                                   po_profiles, tmp_path, monkeypatch):
    _stub_gh_green(tmp_path, monkeypatch)
    brief, run_id, worktree = _setup(fixture_repo, tmp_path, regie_home)
    _queue(monkeypatch, worktree, [_po([QUESTION]), BUILD, REVIEW])

    result = runner.invoke(app, ["run", str(brief), "--repo", str(fixture_repo),
                                 "--profiles", str(po_profiles), "--autonomous"])

    assert result.exit_code == 0, result.output
    state = RunDir.open(regie_home, run_id).read_state()
    assert (state.stage, state.questions[0].defaulted) == ("done", True)


def test_a_clear_request_takes_the_fast_path(regie_home, fixture_repo, remote_repo,
                                             po_profiles, tmp_path, monkeypatch):
    _stub_gh_green(tmp_path, monkeypatch)
    brief, run_id, worktree = _setup(fixture_repo, tmp_path, regie_home)
    _queue(monkeypatch, worktree, [_po(), BUILD, REVIEW])

    result = runner.invoke(app, ["run", str(brief), "--repo", str(fixture_repo),
                                 "--profiles", str(po_profiles)])

    assert result.exit_code == 0, result.output
    assert RunDir.open(regie_home, run_id).read_state().stage == "done"


def test_a_run_from_before_the_redesign_is_refused(regie_home, fixture_repo, po_profiles,
                                                   tmp_path):
    _setup(fixture_repo, tmp_path, regie_home)
    rundir = RunDir.create(regie_home, "legacy")
    (rundir.path / "state.json").write_text(
        '{"id": "legacy", "target_repo": "/x", "branch": "b", "stage": "approve"}')

    result = runner.invoke(app, ["resume", "legacy", "--repo", str(fixture_repo),
                                 "--profiles", str(po_profiles)])

    assert result.exit_code == 2
    assert "created before Régie's framework redesign" in result.output
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest -q tests/test_e2e_full.py`
Expected: FAIL — the run starts in the old keyword router (no `Q1` in the output), and the legacy state is resumed instead of refused.

- [ ] **Step 3: Implement the pipeline changes** — in `src/regie/pipeline.py`:

1. Imports: add `from regie.lifecycle import transition, transition_increment` and `Question` to the `regie.models` import. After the deletions in item 8, remove imports ruff reports unused.

2. `DIRECT_OWNER_SCHEMA`: the `status` enum becomes `["completed", "clarify"]` and the `evidence` property goes (remove it from `required` and `properties`). Delete `BASELINE_OWNER_SCHEMA` (keep `BASELINE_NOTE`). In `_dispatch`, the schema choice becomes:

```python
    schema = None
    if stage == "build" and task.spec.execution == "direct":
        schema = DIRECT_OWNER_SCHEMA
```

and delete the `elif status == "needs_planning":` branch.

3. Halts go through the lifecycle:

```python
def _halt(rundir: RunDir, run: RunState, task_id: str, reason: str) -> None:
    run.tasks[task_id].status = "failed" if "blocked" not in reason else "blocked"
    transition(rundir, run, "halt", reason=reason)
```

```python
def _halt_run(rundir: RunDir, run: RunState, reason: str) -> None:
    transition(rundir, run, "halt", reason=reason)
```

In `_gate_and_advance`'s infrastructure branch, replace the `run.stage = "halted"` / `run.halt_reason = (...)` / event / `write_state` lines with (keep whatever the block does to the task first):

```python
            reason = ("infrastructure gate failed; implementation preserved: "
                      + ", ".join(gate.name for gate in infrastructure))
            rundir.append_event({
                "kind": "infrastructure_blocked",
                "task": task_id,
                "stage": stage,
                "gates": [gate.name for gate in infrastructure],
                "reason": reason,
            })
            transition(rundir, run, "halt", reason=reason)
            return
```

In `_prepare_environment`'s failure branch, replace the stage/reason/write lines with:

```python
        transition(rundir, run, "halt", reason=(
            f"worktree setup failed before agent dispatch: {gate.detail[-500:]}"))
        return False
```

4. Stakeholder questions from building. Add next to `_halt`:

```python
def _ask_stakeholder(run: RunState, question: str, why: str) -> None:
    """A question raised while building joins the inbox (no default)."""
    run.questions.append(Question(id=f"Q{len(run.questions) + 1}", round=0,
                                  question=question, why=why))
```

In `run_task`'s blocked branch, register a developer's clarification before any halt and delete the whole `needs-planning:` escalation block:

```python
            question = attempt.blocked_question or ""
            if question.startswith("clarify:"):
                _ask_stakeholder(run, question.removeprefix("clarify:").strip(),
                                 "The developer needs this to continue building.")
            if run.baseline:
```

(the rest — the baseline halt, the `bad-test:` escape, the final `_halt` — is unchanged.) In `_handle_serious_findings`'s `ask_human` branch, call `_ask_stakeholder(run, decision.human_question or decision.summary, "The Product Owner needs your decision to finish the review.")` before its `_halt`.

5. Replace `apply_direct_brief` with a shared checklist, the baseline compiler, and the increment starter:

```python
DIRECT_CHECKLIST = (
    "Every explicit requirement is covered by implementation evidence.",
    "Focused regression tests prove the changed behavior.",
    "The solution reuses existing code or platform capabilities before adding machinery.",
    "No unrelated refactor, speculative abstraction, or unnecessary dependency was added.",
)


def apply_baseline_brief(rundir: RunDir, run: RunState, brief_text: str) -> None:
    """Baseline mode: the raw brief becomes one developer's task, no Product Owner."""
    first = next((line.strip().lstrip("#").strip() for line in brief_text.splitlines()
                  if line.strip()), "Implement the requested change")
    spec_dir = rundir.path / "spec"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.md").write_text("# Baseline brief\n\n" + brief_text.strip() + "\n")
    spec = TaskSpec(id="T1", title=first[:120], profile="implementer",
                    criteria=[brief_text.strip()],
                    planned_tests=["Add or adapt the smallest focused regression tests "
                                   "needed by the brief."],
                    checklist=list(DIRECT_CHECKLIST), execution="direct")
    run.tasks = {spec.id: TaskState(spec=spec, stage="build")}
    run.stage = "active"
    rundir.write_state(run)


def start_increment(rundir: RunDir, run: RunState) -> None:
    """Turn the spec's increment into its direct-owner task (Phase 1: one)."""
    if run.spec is None:
        raise RuntimeError("an active request needs a spec or tasks")
    increment = run.spec.increments[0]
    spec = TaskSpec(id="T1", title=increment.title[:120], profile="implementer",
                    criteria=[f"{c.id}: {c.text}" for c in increment.criteria],
                    planned_tests=["Add or adapt the smallest focused tests that prove "
                                   "each acceptance criterion."],
                    checklist=list(DIRECT_CHECKLIST), execution="direct")
    run.tasks = {spec.id: TaskState(spec=spec, stage="build")}
    increment.task_ids = [spec.id]
    transition_increment(rundir, run, increment.id, "spec_accepted")
    transition_increment(rundir, run, increment.id, "build_started")
```

(`apply_baseline_brief` sets `run.stage` directly: it creates the request in its starting state, before any lifecycle event.)

6. `run_tasks_stage`: start the increment when there are no tasks yet, work in `active`, drop checkpoints, and finish through the lifecycle:

```python
def run_tasks_stage(rundir: RunDir, run: RunState, cfg: RegieConfig,
                    repo: Path) -> None:
    """Build the request's increment (Phase 1: one direct-owner task)."""
    if not run.tasks:
        start_increment(rundir, run)
    ctx = PipelineContext(
        spec_excerpt=(rundir.path / "spec" / "spec.md").read_text()
        if (rundir.path / "spec" / "spec.md").exists() else "",
        spec_path=rundir.path / "spec" / "spec.md",
        decisions_path=rundir.path / "decisions.md",
        conventions=_conventions(repo),
        convention_paths=_convention_paths(repo))
    for layer in run.task_layers():
        pending = [task_id for task_id in layer
                   if run.tasks[task_id].status != "done"]
        if not pending:
            continue
        batch_specs = [run.tasks[task_id].spec for task_id in pending]
        can_parallel = (cfg.workflow.max_parallel_tasks > 1 and len(pending) > 1
                        and not scopes_overlap(batch_specs))
        if can_parallel:
            _run_parallel_batch(rundir, run, cfg, repo, ctx, pending)
        else:
            for task_id in pending:
                run_task(rundir, run, task_id, cfg, repo, ctx)
                if run.stage != "active":
                    return
        if run.stage != "active":
            return
    for increment in run.spec.increments if run.spec else []:
        if increment.state == "building":
            transition_increment(rundir, run, increment.id, "completed")
    transition(rundir, run, "increments_done")
```

7. `finalize_stage`'s end becomes:

```python
    run.base_sha = git(worktree, "rev-parse", base_ref).strip()
    transition(rundir, run,
               "finalized_for_pr" if cfg.workflow.submit_pr else "finalized_locally")
```

In `_ci_loop`, both `run.stage = "reflect" if cfg.workflow.reflection else "done"` / `rundir.write_state(run)` pairs become `transition(rundir, run, "pr_ready")`. In `reflect_stage`, replace `run.stage = "done"` / `rundir.write_state(run)` with `transition(rundir, run, "reflected")` (the parent-run update after it is unchanged). `reflect_stage` already proposes learnings only when reflection is enabled, so every request now passes through `reflect`.

8. Delete the plan stage and everything only it used: `PLAN_REVIEW_SCHEMA`, `_STR_ARRAY`, `PLAN_SCHEMA`, `_plan_schema`, the module-level `CRITERION_RE`, `_MAX_PLAN_CONTRACT_ATTEMPTS`, `_MAX_PRODUCT_OWNER_REVISIONS`, `_render_plan_packet`, `_validate_plan`, `_PLAN_LENSES`, `_plan_review_names`, `_run_plan_reviews`, `_run_product_owner`, `_apply_plan`, `plan_stage`. Keep `_ask_product_owner`, `_run_execution_product_owner`, `_write_product_owner_decision`, `PRODUCT_OWNER_SCHEMA`, and `_PRODUCT_OWNER_TASK_ID` (execution recovery, until Phase 2). Keep `_PLAN_TASK_ID` (reconcile still maps old `PLAN` intents until Task 7).

9. `profiles/implementer.md`:
   - The first paragraph becomes: "You are the developer who owns one increment of the stakeholder's spec. Understand it, implement it, add or adapt the focused tests it needs, and leave the repository ready for the harness's independent gates and review."
   - Delete the two bullets that start "If repository evidence shows the change needs decomposition" and "Do not request planning merely".
   - The outcome contract becomes:
     ```markdown
     Return the requested JSON object with exactly one status:

     - `completed`: implementation and focused tests are ready; set `question` to null.
     - `clarify`: one material product choice is unresolved; put the single question
       in `question`.
     ```

- [ ] **Step 4: Implement the CLI changes** — in `src/regie/cli.py`:

Imports: the pipeline import list becomes `apply_baseline_brief, finalize_stage, pr_stage, reconcile, reflect_stage, run_tasks_stage`; add `from pydantic import ValidationError`, `from regie.intake import intake_stage`, `from regie.lifecycle import transition`, `from regie.operations import continue_after_answers, pending_items`; remove `from regie.workflow import route_brief`.

Replace `_print_approve_hint`, `_advance`, and `_after_plan_stage` with:

```python
_LEGACY_STAGES = {"research", "plan", "approve", "tasks", "checkpoint"}


def _read_state(rundir: RunDir) -> RunState:
    try:
        state = rundir.read_state()
    except ValidationError:
        state = None
    if state is None or state.stage in _LEGACY_STAGES:
        typer.echo(f"run {rundir.path.name} was created before Régie's framework "
                   f"redesign and can no longer be opened; start a new run "
                   f"(delete {rundir.path} to remove it)", err=True)
        raise typer.Exit(2)
    return state


def _print_inbox(state: RunState) -> None:
    items = pending_items(state)
    noun = "item" if len(items) == 1 else "items"
    typer.echo(f"waiting for you ({len(items)} {noun}):")
    for item in items:
        typer.echo(f"  {item.id:5} {item.title}")
    typer.echo(f"decide with `regie decide {state.id} <item> <decision> [text]` "
               f"(see `regie inbox {state.id}`), then "
               f"`regie resume {state.id} --repo {state.target_repo}`")


def _advance(rundir: RunDir, state: RunState, cfg: RegieConfig, worktree: Path) -> None:
    """Drive the request through every engine-owned state until it waits for
    the stakeholder, halts, or is done."""
    steps = {
        "intake": lambda: intake_stage(rundir, state, cfg, worktree),
        "active": lambda: run_tasks_stage(rundir, state, cfg, worktree),
        "finalize": lambda: finalize_stage(rundir, state, cfg, worktree),
        "pr": lambda: pr_stage(rundir, state, cfg, worktree),
        "reflect": lambda: reflect_stage(rundir, state, cfg),
    }
    while state.stage in steps:
        before = state.stage
        steps[before]()
        if state.stage == before:
            break
    if state.stage in {"awaiting-answers", "awaiting-spec-approval"}:
        _print_inbox(state)
        return
    if state.stage == "done":
        typer.echo(f"PR ready: {state.pr_url}" if state.pr_url else f"run {state.id} → done")
        notify("regie run complete", f"{state.id}: {state.pr_url or 'done'}")
        return
    _finish(state)
```

In `run`: delete the `workflow` option; the option check becomes `if baseline and tasks_file is not None:` with message `--baseline cannot be combined with --tasks-file`; construct the state with `stage="intake"` and no `workflow=`; replace the routing block (from `if tasks_file is not None:` to the end of its `else`) with:

```python
    if tasks_file is not None:
        specs = [TaskSpec(**t) for t in json.loads(tasks_file.read_text())]
        state.tasks = {s.id: TaskState(spec=s) for s in specs}
        state.stage = "active"
        rundir.write_state(state)
    elif baseline:
        apply_baseline_brief(rundir, state, brief_text)
    else:
        rundir.write_state(state)
```

In `resume`, replace `state = rundir.read_state()` with `state = _read_state(rundir)`, and replace everything from `if state.stage == "halted":` to the end of the function with:

```python
    if state.stage == "halted":
        for task in state.tasks.values():
            if task.status in ("failed", "blocked", "running"):
                # A human-mediated resume deserves a fresh ladder; attempts are
                # kept (no WAL reset marker), so budgets and reconcile count them.
                task.reset_for_resume()
        if state.halted_from is None:
            state.halted_from = "pr" if state.pushed else ("active" if state.tasks else "intake")
        transition(rundir, state, "resume")
    if state.stage == "awaiting-answers":
        defaulted = continue_after_answers(rundir, state)
        if defaulted:
            typer.echo("took the default for " + ", ".join(defaulted))
    if state.stage == "awaiting-spec-approval":
        _print_inbox(state)
        return
    _advance(rundir, state, cfg, worktree)
```

- [ ] **Step 5: Update the existing tests**

- Delete `tests/test_plan_stage.py`.
- Everywhere in `tests/` except the tests of the old `approve` command and `approve_waiting_state` (they go in Task 7): `stage="tasks"` → `stage="active"`, and assertions on `stage == "tasks"` → `"active"`.
- `tests/test_metaswarm_features.py`: delete `test_standard_single_task_uses_one_generic_plan_lens`, `test_explicit_critical_workflow_keeps_full_plan_panel`, `test_auto_single_migration_uses_only_risk_design_lens`, `test_plan_review_fails_over_when_primary_provider_has_quota`, `test_post_product_owner_plan_review_does_not_expand_panel`, `test_checkpoint_is_a_blocking_state_transition`, and helpers only they use.
- `tests/test_pipeline.py`:
  - delete `test_direct_owner_escalates_to_plan_only_with_repository_evidence`, `_escalate_direct_owner`, `test_direct_owner_escalation_keeps_its_spend_in_run_cost`, `test_direct_owner_escalation_retires_its_dispatch_log`;
  - rename `test_direct_brief_creates_one_owner_task_without_plan` to `test_baseline_brief_creates_one_developer_task`: construct the run with `baseline=True` (no `execution_route`/`route_reason`), call `apply_baseline_brief`, and assert `run.stage == "active"` plus the existing T1 and spec-file assertions;
  - in `test_direct_owner_structured_clarification_halts_before_gates`, add `assert [(q.round, q.default) for q in run.questions] == [(0, "")]` and assert the recorded question's text equals the question that test's agent asks (a developer question joins the inbox).
- `tests/test_cli.py`: delete `test_auto_run_routes_low_risk_brief_directly_without_planner` and `test_resume_after_plan_halt_keeps_prior_planner_spend`; `test_baseline_rejects_other_routing_options` keeps only the `--tasks-file` case (drop the parametrize) and expects `--baseline cannot be combined with --tasks-file`; in `test_baseline_run_finishes_after_gates_without_review` replace the `execution_route` and `planner_attempts` assertions with `assert state.spec is None and state.product_owner_attempts == []`; in `test_baseline_halts_instead_of_escalating` drop the `needs-planning:` parameter (keep `bad-test:`) and make the same assertion replacement.
- `tests/test_finalize_pr.py::test_finalize_can_complete_locally_without_submitting_pr`: a local finalize now ends in `reflect` (the request loop runs `reflect_stage` next) — expect `run.stage == "reflect"`.
- Tests that resume a `stage="halted"` state without `halted_from` keep working (resume infers it).

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest -q tests/test_e2e_full.py && uv run pytest -q && uvx ruff check src tests`
Expected: 4 passed in `test_e2e_full.py`; full suite green; `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add -A src/regie/pipeline.py src/regie/cli.py profiles/implementer.md tests
git commit -F - <<'EOF'
feat: requests go through Product Owner intake; the plan stage is gone

`regie run` starts with the Product Owner, waits for the stakeholder in
the inbox, and builds the spec's increment with the direct-owner loop.
Halts and stage changes go through the lifecycle; resume returns to the
state a run halted in. The planner, plan reviews, plan-side recovery, and
direct-owner escalation to planning are removed.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RbtkGAHGVyLqrM25Tdxw2w
EOF
```

---

### Task 6: Control room follows the new lifecycle

**Files:**
- Modify: `src/regie/control_room.py`
- Test: `tests/test_control_room.py`

**Interfaces:**
- Consumes: `operations.pending_items`, `operations.decide` (Task 4); the new stage names.
- Produces: `_waiting_line(state) -> str | None`; `_launch_args(brief_path, repo, *, autonomous) -> list[str]`; keys `A` approves the spec, `C` answers the first open question, `X` requests spec changes, `S` resumes (taking defaults when questions are open).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_control_room.py` (add `Question` to its `regie.models` import and `_launch_args`, `_waiting_line` to its `regie.control_room` import):

```python
def _bare(stage, questions=()):
    return RunState(id="r", target_repo="", branch="", stage=stage,
                    questions=list(questions))


def test_overview_shows_what_waits_for_the_stakeholder():
    question = Question(id="Q1", round=1, question="Clamp?", default="No")
    developer = Question(id="Q1", round=0, question="Which file?")

    assert _waiting_line(_bare("awaiting-answers", [question])) == (
        "WAITING FOR YOU  1 question — press C to answer or S to take the defaults")
    assert _waiting_line(_bare("halted", [developer])) == (
        "WAITING FOR YOU  1 question — press C to answer")
    assert _waiting_line(_bare("awaiting-spec-approval")) == (
        "WAITING FOR YOU  the spec — press A to approve or X to request changes")
    assert _waiting_line(_bare("active")) is None


def test_new_runs_launch_without_a_workflow_tier():
    assert _launch_args(Path("/b.md"), Path("/repo"), autonomous=True) == [
        "run", "/b.md", "--repo", "/repo", "--autonomous"]


@pytest.mark.asyncio
async def test_spec_changes_are_requested_inside_control_room(regie_home, fixture_repo):
    _seed(regie_home, "spec-run", fixture_repo, stage="awaiting-spec-approval")
    app = ControlRoom(regie_home, "spec-run", refresh_interval=60)
    spawned = []
    app._spawn_engine = lambda args, cwd, label: spawned.append((args, cwd, label))

    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        await pilot.press("x")
        await pilot.pause()
        assert isinstance(app.screen, ClarificationScreen)
        app.screen.query_one("#clarification-answer", TextArea).text = "Also show the total"
        app.screen.query_one("#save-clarification", Button).press()
        await pilot.pause()

    state = RunDir.open(regie_home, "spec-run").read_state()
    assert (state.stage, state.spec_feedback) == ("intake", ["Also show the total"])
    assert spawned[0][0] == ["resume", "spec-run", "--repo", str(fixture_repo)]
```

Update the existing tests:
- `test_control_room_renders_run_and_approves`: seed `stage="awaiting-spec-approval"`; expect `"STAGE AWAITING-SPEC-APPROVAL"` in the summary; after pressing `a`, expect `rundir.read_state().stage == "active"` and an event with `kind == "stakeholder_decision"`.
- `test_empty_control_room_opens_new_run_composer`: `assert args[0] == "run" and "--workflow" not in args`.
- `test_clarification_is_answered_and_resumed_inside_control_room`: also seed `state.questions = [Question(id="Q1", round=0, question="Should selection use Shift-click?", why="The developer needs this to continue building.")]` before `write_state`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest -q tests/test_control_room.py`
Expected: FAIL — `ImportError: cannot import name '_launch_args'`.

- [ ] **Step 3: Implement** — in `src/regie/control_room.py`:

- Import `decide, pending_items` from `regie.operations` (drop `approve_waiting_state`, `record_clarification`).
- `_STAGE_STYLES`: remove `approve` and `checkpoint`; add `"intake": "bold #79c0ff"`, `"awaiting-answers": "bold #d2a8ff"`, `"awaiting-spec-approval": "bold #d2a8ff"` (`active` uses the default style).
- Add module-level helpers:

```python
def _waiting_line(state: RunState) -> str | None:
    """The overview's call to action when a run waits for the stakeholder."""
    if state.stage == "awaiting-spec-approval":
        return "WAITING FOR YOU  the spec — press A to approve or X to request changes"
    open_questions = [q for q in state.questions if q.open]
    if not open_questions:
        return None
    noun = "question" if len(open_questions) == 1 else "questions"
    line = f"WAITING FOR YOU  {len(open_questions)} {noun} — press C to answer"
    if state.stage == "awaiting-answers":
        line += " or S to take the defaults"
    return line


def _launch_args(brief_path: Path, repo: Path, *, autonomous: bool) -> list[str]:
    args = ["run", str(brief_path), "--repo", str(repo)]
    if autonomous:
        args.append("--autonomous")
    return args
```

- `NewRunRequest`: remove `workflow`. `NewRunScreen`: remove the `#workflow` `Select` and its CSS rule; the checkbox label becomes `"Autonomous (take default answers, skip spec approval)"`; `dismiss(NewRunRequest(name, repo, brief, autonomous))`. `_start_new_run` builds `args = _launch_args(brief_path, request.repo, autonomous=request.autonomous)`.
- `RunScreen`: columns `"Run", "Stage", "Tasks"` (drop `run.workflow`).
- `_artifact_files`: label `"Spec"` for `spec/spec.md`; remove `"Current checkpoint"`; add `("Spec changes", rundir.path / "spec" / "spec.diff")` and `("Stakeholder decisions", rundir.path / "decisions.md")`.
- Task detail: delete the `("Risk", ...)` row.
- Activity projection: delete the `planner_attempts` loop.
- `_render_state`: the second summary line becomes `f"    TASKS {done}/{total}\n"`, followed by `    INCREMENT {inc.id} {inc.state.upper()}` when `state.spec` has an increment; delete the `ROUTE` line and the `approve`/`checkpoint` hint; after the halt / Product Owner / PR lines, append `"\n" + line` in `bold #d2a8ff` when `line := _waiting_line(state)` is not `None`.
- `BINDINGS`: `("c", "clarify", "Answer")`, add `("x", "request_changes", "Changes")`.
- `action_approve`: `decide(self.rundir, self.state, "spec", "approve")`, then notify `"spec approved — press S to start building"`; show a `ValueError` as a warning.
- `action_clarify`: take the first item of `pending_items(self.state)` whose kind is `"question"`; none → warn `"Nothing is waiting for an answer"`; otherwise remember `self._answering = item.id` and push `ClarificationScreen(f"{item.title}\n\n{item.detail}")`. `_finish_clarification(answer)`: `decide(self.rundir, self.state, self._answering, "answer", answer)`, log `f"answered {self._answering} for {self.state.id}"`; when no open question remains, `self.action_resume()`, else notify `"press C for the next question"`.
- `action_request_changes`: outside `awaiting-spec-approval` warn `"The spec is not awaiting approval"`; otherwise push `ClarificationScreen("What should change in the spec?")` with a callback that calls `decide(self.rundir, self.state, "spec", "changes", text)` and then `self.action_resume()`.
- `action_resume`: refuse in `done`, and in `awaiting-spec-approval` with `"decide the spec first (A to approve, X to request changes)"`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q tests/test_control_room.py tests/test_cli_pty.py && uv run pytest -q && uvx ruff check src tests`
Expected: all pass; `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add src/regie/control_room.py tests/test_control_room.py
git commit -F - <<'EOF'
feat(control-room): answer questions and approve specs in the new lifecycle

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RbtkGAHGVyLqrM25Tdxw2w
EOF
```

---

### Task 7: Delete tiers, keyword routing, checkpoints, and the old commands

**Files:**
- Modify: `src/regie/workflow.py`, `src/regie/pipeline.py`, `src/regie/models.py`, `src/regie/config.py`, `src/regie/providers.py`, `src/regie/knowledge.py`, `src/regie/onboarding.py`, `src/regie/cli.py`, `src/regie/operations.py`
- Delete: `profiles/planner.*`, `profiles/plan-completeness.*`, `profiles/plan-feasibility.*`, `profiles/plan-scope.*`, `profiles/security-design-reviewer.*`, `profiles/ux-design-reviewer.*`, `profiles/architecture-design-reviewer.*`
- Test: `tests/test_token_governor.py`, `tests/test_workflow.py`, and the sweep in Step 4

**Interfaces:**
- Produces: `workflow.active_gate_plugins(cfg, stage, changed)` (no tier); `GatePlugin` without `tiers`; `WorkflowConfig` without `default_tier`, `direct_execution`, `plan_reviews`, `design_reviews`; `RunStage` = the request states only; `TaskSpec` without `risk_tags`, `external_dependencies`, `checkpoint`; `RunState` without `workflow`, `execution_route`, `route_reason`, `planner_attempts`, `plan_reviews`, `plan_review_attempts`, `checkpoints`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_token_governor.py`, replace `test_triggered_specialist_records_evidence_and_findings` with:

```python
def _security_setup(fixture_repo, regie_home, tmp_path, **task_fields):
    base = head_sha(fixture_repo)
    (fixture_repo / "src" / "auth.py").write_text("TOKEN = 'x'\n")
    commit_all(fixture_repo, "feat: auth token")
    prompt = tmp_path / "security.md"
    prompt.write_text("Review security. Return JSON.")
    profile = Profile(
        name="security-reviewer", bindings=[Binding(cli="fake", model="m1")],
        prompt_path=prompt, budgets=Budgets(),
        token_policy=TokenPolicy(tools=["list", "read", "search"], sandbox="read-only"))
    cfg = SimpleNamespace(profiles={"security-reviewer": profile},
                          workflow=WorkflowConfig())
    task = TaskSpec(
        id="T1", title="Handle auth token", profile="builder",
        criteria=["Given a token, When checked, Then authorization is enforced"],
        **task_fields)
    run = RunState(id="r1", target_repo=str(fixture_repo), branch="regie/r1")
    run.tasks["T1"] = TaskState(spec=task, start_sha=base)
    rd = RunDir.create(regie_home, "r1")
    (fixture_repo / ".fake_agent.json").write_text(json.dumps({"result": {
        "outcome": "done", "structured": {"findings": [{
            "severity": "major", "title": "token bypass", "detail": "missing check",
            "file": "src/auth.py"}]}}}))
    ctx = PipelineContext(spec_excerpt="", decisions_path=rd.path / "decisions.md")
    return rd, run, cfg, ctx


def test_risk_words_no_longer_summon_specialists(fixture_repo, regie_home, tmp_path):
    rd, run, cfg, ctx = _security_setup(fixture_repo, regie_home, tmp_path)

    findings, error = _run_specialist_reviews(rd, run, "T1", cfg, fixture_repo, ctx)

    assert (findings, error, run.tasks["T1"].specialist_attempts) == ([], None, {})


def test_requested_specialist_records_evidence_and_findings(
        fixture_repo, regie_home, tmp_path):
    rd, run, cfg, ctx = _security_setup(fixture_repo, regie_home, tmp_path,
                                        review_lenses=["security-reviewer"])

    findings, error = _run_specialist_reviews(rd, run, "T1", cfg, fixture_repo, ctx)

    assert error is None and findings[0].title == "token bypass"
    assert run.tasks["T1"].specialist_attempts["security-reviewer"][0].outcome == "done"
```

In `tests/test_workflow.py`, replace `test_gate_plugins_filter_by_stage_tier_and_changed_path` with:

```python
def test_gate_plugins_filter_by_stage_and_changed_path():
    plugin = GatePlugin(name="visual", command="playwright test",
                        trigger_globs=["**/*.tsx"], stages=["finalize"])
    cfg = SimpleNamespace(gate_plugins=[plugin])
    assert active_gate_plugins(cfg, "finalize", ["web/app.tsx"]) == [plugin]
    assert active_gate_plugins(cfg, "build", ["web/app.tsx"]) == []
    assert active_gate_plugins(cfg, "finalize", ["api/app.py"]) == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest -q tests/test_token_governor.py tests/test_workflow.py -k "no_longer_summon or filter_by_stage_and"`
Expected: FAIL — the auth keyword still summons the security reviewer (`token bypass` found), and `active_gate_plugins` is missing its `changed` argument.

- [ ] **Step 3: Delete**

- `workflow.py`: delete `_PLANNING_SIGNALS`, `route_brief`, `infer_risks`, `resolve_tier`, `plan_preflight`, `checkpoint_report`, `RISK_LENSES`, `VALID_RISK_TAGS`; `TASK_REVIEW_PROFILES = frozenset({"security-reviewer", "migration-reviewer", "api-reviewer", "ui-reviewer", "architecture-reviewer"})`; keep `review_profiles`, `scopes_overlap`, `_paths_overlap`; `active_gate_plugins(cfg, stage, changed)` drops the tier filter.
- `pipeline.py`: delete `_specialist_profiles`; `_run_specialist_reviews` drops the `design_reviews`/tier check and selects `review_profiles(task.spec, cfg)`; `_policy_gates` adds `coverage` whenever configured and calls `active_gate_plugins(cfg, stage, changed)`; `_run_final_review` drops the tier condition; `_handle_serious_findings` drops the `"security" in infer_risks(...)` clause; `reconcile` drops the `_PLAN_TASK_ID` branch; delete `_PLAN_TASK_ID` and any import ruff then reports unused.
- `models.py`: delete `WorkflowTier`, `ExecutionRoute`, `PlanReview`, `CheckpointState`; from `RunState` delete `workflow`, `execution_route`, `route_reason`, `planner_attempts`, `plan_reviews`, `plan_review_attempts`, `checkpoints`; `RunStage = Literal["intake", "awaiting-answers", "awaiting-spec-approval", "active", "finalize", "pr", "reflect", "done", "halted"]`; from `TaskSpec` delete `risk_tags`, `external_dependencies`, `checkpoint`.
- `config.py`: drop the `WorkflowTier` import; `WorkflowConfig` drops `default_tier`, `direct_execution`, `plan_reviews`, `design_reviews` (pydantic ignores those keys in existing `regie.toml` files); `GatePlugin` drops `tiers`.
- `providers.total_cost`: drop `planner_attempts` and `plan_review_attempts`.
- `knowledge.prime`: drop `terms.update(task.risk_tags)`.
- `onboarding.py`: the generated `[workflow]` section drops `default_tier`, `direct_execution`, `plan_reviews`, `design_reviews`; the visual gate drops its `tiers` line.
- `cli.py`: delete the `approve` and `answer` commands; every other command that reads a run's state (`status`, `inbox`, `decide`, `knowledge-approve`, `handoff`, `doctor`, `clean`) uses `_read_state(rundir)`; the `spec` command's docstring becomes "Print the run's spec (the Product Owner's spec you approve)." and its missing-spec message `run {run_id} has no spec yet (the Product Owner is still drafting it)`.
- `operations.py`: delete `approve_waiting_state` and `record_clarification`.
- Profiles: delete the seven listed profile pairs.

- [ ] **Step 4: Sweep the tests**

- `tests/test_workflow.py`: delete `test_risk_inference_and_auto_tier_are_selective`, `test_auto_tier_keeps_one_low_risk_task_fast`, `test_brief_router_defaults_to_direct_and_respects_risk_and_policy`, `test_brief_router_can_disable_direct_execution`, `test_plan_preflight_requires_checkpoint_for_external_dependency`, `test_plan_preflight_rejects_unnecessary_checkpoint`.
- `tests/test_token_governor.py`: delete `test_specialists_activate_from_committed_risk_evidence`, `test_hard_task_activates_architecture_review_without_keyword`, `test_risk_labels_do_not_manufacture_specialist_evidence`, `test_internal_routing_language_does_not_trigger_api_review`, `test_post_recovery_specialists_are_limited_to_explicit_lenses` (all exercise `_specialist_profiles`).
- `tests/test_cli.py`: delete `test_approve_flips_stage_to_tasks`, `test_approve_on_non_approve_run_is_an_error`, `test_approve_on_nonexistent_run_is_a_friendly_error`.
- `tests/test_operations.py`: delete the `record_clarification` tests and import.
- `tests/test_config.py`: the shipped-profile assertions drop `"planner"`.
- `tests/test_stats.py`: drop the `planner_specs` argument and the planner expectations from `test_collect_aggregates_across_runs` (stats still reads historical `planner_attempts`; the test no longer seeds them because `RunState` no longer has the field).
- `tests/test_onboarding.py`: update the rendered `[workflow]` expectations.
- `tests/conftest.py`: remove `"planner"` from `fake_profiles`.
- Every remaining test: remove the deleted fields from constructors (`workflow=`, `execution_route=`, `route_reason=`, `risk_tags=`, `external_dependencies=`, `checkpoint=`, `WorkflowConfig(default_tier=...)`) — pydantic would silently ignore them, so they only mislead.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest -q && uvx ruff check src tests && grep -rn "route_brief\|resolve_tier\|infer_risks\|plan_stage\|planner_attempts\|checkpoint\|risk_tags\|default_tier" src || echo clean`
Expected: all pass; `All checks passed!`; the grep prints only `src/regie/stats.py`'s historical `planner_attempts` read, or `clean`.

- [ ] **Step 6: Commit**

```bash
git add -A src profiles tests
git commit -F - <<'EOF'
refactor: delete workflow tiers, keyword routing, checkpoints, old commands

Risk words no longer summon reviewers or pick a tier; the stakeholder's
spec approval replaces checkpoints; `regie decide` replaces approve and
answer; runs from before the redesign are refused with a clear message.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RbtkGAHGVyLqrM25Tdxw2w
EOF
```

---

### Task 8: Documentation

**Files:**
- Modify: `README.md`, `docs/ARCHITECTURE.md`, `docs/USAGE.md`, `docs/ROADMAP.md`

**Interfaces:**
- Consumes: the behavior of Tasks 1–7.

- [ ] **Step 1: Rewrite the user-facing description**

- `README.md`: replace the opening paragraphs and "Status" with the framework model — the stakeholder brings a request; a Product Owner agent interviews them and writes a spec with Given/When/Then criteria; the stakeholder approves it (unless the fast path applies); a developer builds the increment; gates and an adversarial review hold the result; Régie opens the PR. State that this version delivers one increment per request and that increments with feedback, planning poker, and the Inbox tab follow (link the spec). In the "Run Régie" command block, replace the `--workflow` line with `regie inbox <run>` and `regie decide <run> <item> <decision> [text]`; remove `regie approve` and `regie answer`.
- `docs/ARCHITECTURE.md`: replace the tier/plan description and the flow diagram with the request lifecycle (`intake → awaiting-answers → intake → awaiting-spec-approval → active → finalize → pr → reflect → done`, and `halted` with resume to where it stopped), the intake rules (questions in batches with defaults, at most three rounds, defaults become visible assumptions, the spec Definition of Ready, the fast path), and the increment lifecycle (`draft → ready → building → accepted`); add `spec/spec.md` (rendered spec), `spec/spec.diff` (changes after feedback), and `decisions.md` (the stakeholder's decisions) to the sources-of-truth list.
- `docs/USAGE.md`: the daily loop becomes `regie run request.md --repo …` → `regie inbox <run>` → `regie decide <run> Q1 answer "…"` (or `default`) → `regie resume` → `regie decide <run> spec approve` (or `changes "…"`) → `regie resume`; document `--autonomous` (defaults taken, no approval) and `--baseline`; remove every `--workflow` and tier mention.
- `docs/ROADMAP.md`: add "Done 2026-10 — Phase 1a" listing the lifecycle, intake, inbox/decide, and the deletions; note Phase 1b next.

- [ ] **Step 2: Verify no stale references**

Run: `grep -rn -- "--workflow\|default_tier\|regie approve\|regie answer\|planner\b" README.md docs/ARCHITECTURE.md docs/USAGE.md || echo clean`
Expected: `clean`.

- [ ] **Step 3: Commit**

```bash
git add README.md docs/ARCHITECTURE.md docs/USAGE.md docs/ROADMAP.md
git commit -F - <<'EOF'
docs: describe the stakeholder, Product Owner, and request lifecycle

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RbtkGAHGVyLqrM25Tdxw2w
EOF
```

---

### Task 9: Real-agent smoke run

Unit and fake-adapter tests cannot show that real Product Owners accept the intake schema and write usable specs. Four autonomous trials can: both providers, one Taskboard and one Reelog case each.

**Files:** none (results land in regie-bench's git-ignored `results/`).

- [ ] **Step 1: Run the trials** (in `/Users/frederic/Code/regie-bench`, each in its own background shell):

```bash
uv run regie-bench run reject-blank-title --regie-root ../regie --provider codex  --label phase1a-smoke
uv run regie-bench run reject-blank-title --regie-root ../regie --provider claude --label phase1a-smoke
uv run regie-bench run reelog-runtime     --regie-root ../regie --provider codex  --label phase1a-smoke
uv run regie-bench run reelog-runtime     --regie-root ../regie --provider claude --label phase1a-smoke
```

Expected: every trial reaches `stage=done` with `acceptance_passed: true`. The bench still reports `passed: False` on cases with an `expected_route`, because runs no longer record a route (Plan 1b moves the bench off routes); judge by stage and acceptance.

- [ ] **Step 2: Inspect the Product Owner's work**

For each trial, read `regie-home/runs/*/spec/spec.md` and the `product-owner` rows of `result.json`'s `stage_breakdown`. Expected: a goal, one increment, Given/When/Then criteria with `INC-1/AC-n` IDs, and every defaulted question listed as an assumption.

- [ ] **Step 3: Report**

Report the four result lines, one spec excerpt per provider, and the Product Owner's time and token cost against the build's. If a provider rejects the schema or a trial fails for a reason other than the agent's work, stop and debug (superpowers:systematic-debugging) before Plan 1b.
