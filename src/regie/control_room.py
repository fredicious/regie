"""Live terminal control room over Régie's authoritative run artifacts."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    MarkdownViewer,
    RichLog,
    Select,
    Static,
    TextArea,
)

from regie.models import Binding, RunState, TaskState
from regie.onboarding import (
    DEFAULT_PROVIDERS,
    ProjectDetection,
    detect,
    enabled_providers,
    initialize,
    update_providers,
)
from regie.operations import approve_waiting_state, record_clarification
from regie.providers import health
from regie.rundir import RunDir

_STAGE_STYLES = {
    "done": "bold #7ee787",
    "halted": "bold #ff7b72",
    "approve": "bold #d2a8ff",
    "checkpoint": "bold #d2a8ff",
    "pr": "bold #79c0ff",
    "reflect": "bold #79c0ff",
}
_STATUS_MARKS = {
    "pending": "○",
    "running": "◉",
    "done": "✓",
    "blocked": "!",
    "failed": "✗",
}


@dataclass(frozen=True)
class NewRunRequest:
    name: str
    repo: Path
    brief: str
    workflow: str
    autonomous: bool


@dataclass
class ManagedProcess:
    process: subprocess.Popen
    label: str
    log_path: Path
    log_offset: int


@dataclass(frozen=True)
class AgentActivity:
    task: str
    stage: str
    attempt: int
    provider: str
    model: str
    status: str
    elapsed_seconds: float
    last_activity: datetime
    turns: int
    fresh_tokens: int
    cached_tokens: int


@dataclass(frozen=True)
class UsageTotals:
    fresh_tokens: int
    cached_tokens: int
    cost_usd: float
    attempts: int


@dataclass(frozen=True)
class SetupRequest:
    detection: ProjectDetection
    enabled_providers: tuple[str, ...]


def _provider_health_text(provider: str) -> str:
    status = health(Binding(cli=provider, model="default"))
    return f"{status.status}: {status.detail}"


class SetupScreen(ModalScreen[SetupRequest | None]):
    """Review detected project commands before Régie creates its configuration."""

    CSS = """
    SetupScreen {
        align: center middle;
        background: #02050a 70%;
    }
    #setup-dialog {
        width: 88;
        height: 94%;
        max-height: 42;
        min-height: 22;
        padding: 1 2;
        background: #0d1626;
        border: tall #d2a8ff;
    }
    #setup-title {
        height: 2;
        color: #d2a8ff;
        text-style: bold;
    }
    #setup-summary {
        height: 3;
        color: #9fb3c8;
    }
    .setup-label {
        height: 1;
        margin-top: 1;
        color: #9fb3c8;
    }
    #setup-visual {
        height: 3;
        padding-top: 1;
    }
    #setup-provider-title {
        height: 1;
        margin-top: 1;
        color: #9fb3c8;
    }
    .setup-provider {
        height: 3;
    }
    #setup-error {
        height: 2;
        color: #ff7b72;
    }
    #setup-buttons {
        height: 3;
        align-horizontal: right;
    }
    #setup-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, repo: Path):
        super().__init__()
        self.repo = repo.resolve()
        self.detection = detect(self.repo)

    def compose(self) -> ComposeResult:
        detected = self.detection
        with VerticalScroll(id="setup-dialog"):
            yield Label("SET UP RÉGIE", id="setup-title")
            yield Static(
                f"No regie.toml found. Detected a {detected.language} project at\n"
                f"{self.repo}",
                id="setup-summary",
            )
            for label, command, widget_id in (
                ("Dependency setup (optional)", detected.setup or "", "setup-bootstrap"),
                ("Test command", detected.test, "setup-test"),
                ("Lint command", detected.lint, "setup-lint"),
                ("Typecheck command (optional)", detected.typecheck or "", "setup-typecheck"),
                ("Build command (optional)", detected.build or "", "setup-build"),
                ("Coverage command (optional)", detected.coverage or "", "setup-coverage"),
            ):
                yield Label(label, classes="setup-label")
                yield Input(value=command, id=widget_id)
            yield Label("Enabled agent providers", id="setup-provider-title")
            for provider, label in (("claude", "Claude Code"), ("codex", "Codex CLI")):
                yield Checkbox(
                    f"{label}  ·  {_provider_health_text(provider)}",
                    value=True,
                    id=f"setup-provider-{provider}",
                    classes="setup-provider",
                )
            yield Checkbox("Enable browser-based visual gate", value=detected.ui,
                           id="setup-visual")
            yield Static("", id="setup-error")
            with Horizontal(id="setup-buttons"):
                yield Button("Not now", id="cancel-setup")
                yield Button("Save & continue", variant="primary", id="save-setup")

    def on_mount(self) -> None:
        self.query_one("#setup-test", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-setup":
            self.dismiss(None)
            return
        test = self.query_one("#setup-test", Input).value.strip()
        lint = self.query_one("#setup-lint", Input).value.strip()
        if not test or not lint:
            self.query_one("#setup-error", Static).update(
                "Test and lint commands are required."
            )
            return
        providers = tuple(
            provider for provider in DEFAULT_PROVIDERS
            if self.query_one(f"#setup-provider-{provider}", Checkbox).value
        )
        if not providers:
            self.query_one("#setup-error", Static).update(
                "Enable at least one agent provider."
            )
            return

        def optional(widget_id: str) -> str | None:
            return self.query_one(widget_id, Input).value.strip() or None

        self.dismiss(SetupRequest(
            detection=ProjectDetection(
                language=self.detection.language,
                setup=optional("#setup-bootstrap"),
                test=test,
                lint=lint,
                typecheck=optional("#setup-typecheck"),
                build=optional("#setup-build"),
                coverage=optional("#setup-coverage"),
                test_globs=self.detection.test_globs,
                ui=self.query_one("#setup-visual", Checkbox).value,
            ),
            enabled_providers=providers,
        ))


class ProviderScreen(ModalScreen[tuple[str, ...] | None]):
    """Enable or disable project providers without editing TOML by hand."""

    CSS = """
    ProviderScreen {
        align: center middle;
        background: #02050a 70%;
    }
    #provider-dialog {
        width: 78;
        height: 25;
        padding: 1 2;
        background: #0d1626;
        border: tall #79c0ff;
    }
    #provider-title {
        height: 2;
        color: #79c0ff;
        text-style: bold;
    }
    #provider-copy {
        height: 4;
        color: #9fb3c8;
    }
    .provider-choice {
        height: 3;
        margin-top: 1;
    }
    #provider-error {
        height: 2;
        color: #ff7b72;
    }
    #provider-buttons {
        height: 3;
        align-horizontal: right;
    }
    #provider-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, repo: Path):
        super().__init__()
        self.repo = repo.resolve()
        self.current = set(enabled_providers(self.repo))

    def compose(self) -> ComposeResult:
        with Vertical(id="provider-dialog"):
            yield Label("AGENT PROVIDERS", id="provider-title")
            yield Static(
                f"Project: {self.repo}\n"
                "Changes apply to the next run or resume; active attempts keep their routing.",
                id="provider-copy",
            )
            for provider, label in (("claude", "Claude Code"), ("codex", "Codex CLI")):
                yield Checkbox(
                    f"{label}  ·  {_provider_health_text(provider)}",
                    value=provider in self.current,
                    id=f"provider-{provider}",
                    classes="provider-choice",
                )
            yield Static("", id="provider-error")
            with Horizontal(id="provider-buttons"):
                yield Button("Cancel", id="cancel-providers")
                yield Button("Save providers", variant="primary", id="save-providers")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-providers":
            self.dismiss(None)
            return
        selected = tuple(
            provider for provider in DEFAULT_PROVIDERS
            if self.query_one(f"#provider-{provider}", Checkbox).value
        )
        if not selected:
            self.query_one("#provider-error", Static).update(
                "Enable at least one agent provider."
            )
            return
        self.dismiss(selected)


class RunScreen(ModalScreen[str | None]):
    """Occasional run navigation without permanently consuming dashboard space."""

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [("escape", "close", "Close")]
    CSS = """
    RunScreen {
        align: center middle;
        background: #02050a 70%;
    }
    #run-dialog {
        width: 88;
        height: 70%;
        max-height: 32;
        min-height: 12;
        padding: 1 2;
        background: #0d1626;
        border: tall #79c0ff;
    }
    #run-picker-title {
        height: 2;
        color: #79c0ff;
        text-style: bold;
    }
    #run-picker {
        height: 1fr;
    }
    #run-picker-buttons {
        height: 3;
        align-horizontal: right;
    }
    """

    def __init__(self, runs: list[RunState], current_run_id: str | None):
        super().__init__()
        self.runs = runs
        self.current_run_id = current_run_id

    def compose(self) -> ComposeResult:
        with Vertical(id="run-dialog"):
            yield Label("RUNS  ·  current repository", id="run-picker-title")
            yield DataTable(id="run-picker", cursor_type="row", zebra_stripes=True)
            with Horizontal(id="run-picker-buttons"):
                yield Button("Close", id="close-runs")

    def on_mount(self) -> None:
        table = self.query_one("#run-picker", DataTable)
        table.add_columns("Run", "Stage", "Workflow", "Tasks")
        ids = []
        for run in self.runs:
            done = sum(task.status == "done" for task in run.tasks.values())
            table.add_row(
                run.id,
                run.stage,
                run.workflow,
                f"{done}/{len(run.tasks)}",
                key=run.id,
            )
            ids.append(run.id)
        if self.current_run_id in ids:
            table.move_cursor(row=ids.index(self.current_run_id))
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if (event.data_table.id == "run-picker" and event.row_key is not None
                and event.row_key.value is not None):
            self.dismiss(str(event.row_key.value))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close-runs":
            self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


def brief_name(brief: str) -> str:
    """Derive a stable, filesystem-safe run name from the first brief line."""
    first_line = next((line.strip().lstrip("#").strip()
                       for line in brief.splitlines() if line.strip()), "")
    first_line = first_line.replace("‘", "'").replace("’", "'")
    ascii_line = unicodedata.normalize("NFKD", first_line).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_line.lower()).strip("-")[:48].rstrip("-")
    return slug or f"run-{datetime.now(UTC).strftime('%H%M%S')}"


def process_failure_detail(log_path: Path, offset: int) -> str:
    """Return the last useful output line emitted by one managed process."""
    try:
        with log_path.open(errors="replace") as log:
            log.seek(offset)
            lines = log.read().splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        detail = line.strip()
        if detail and not detail.startswith(("╭", "╮", "│", "╰", "╯")):
            return detail[:300]
    return ""


class NewRunScreen(ModalScreen[NewRunRequest | None]):
    """Collect a new brief without leaving the control room."""

    CSS = """
    NewRunScreen {
        align: center middle;
        background: #02050a 70%;
    }
    #new-run-dialog {
        width: 82;
        height: 90%;
        max-height: 38;
        min-height: 24;
        padding: 1 2;
        background: #0d1626;
        border: tall #4582bd;
    }
    #new-run-title {
        height: 2;
        color: #79c0ff;
        text-style: bold;
    }
    .field-label {
        height: 1;
        margin-top: 1;
        color: #9fb3c8;
    }
    #brief-body {
        height: 1fr;
        min-height: 5;
        border: solid #29466b;
    }
    #new-run-options {
        height: 3;
        margin-top: 1;
    }
    #workflow {
        width: 30;
        margin-right: 2;
    }
    #autonomous {
        width: 1fr;
        padding-top: 1;
    }
    #new-run-error {
        height: 2;
        color: #ff7b72;
    }
    #new-run-buttons {
        height: 3;
        align-horizontal: right;
    }
    Button {
        margin-left: 1;
    }
    """

    def __init__(self, default_repo: Path, initial: NewRunRequest | None = None):
        super().__init__()
        self.default_repo = default_repo
        self.initial = initial

    def compose(self) -> ComposeResult:
        with Vertical(id="new-run-dialog"):
            yield Label("NEW RÉGIE RUN", id="new-run-title")
            yield Label("Product brief", classes="field-label")
            yield TextArea(
                self.initial.brief if self.initial else "",
                placeholder="Describe the outcome, constraints, and acceptance criteria…",
                id="brief-body",
                language="markdown",
            )
            yield Label("Run name (optional — inferred from the brief)", classes="field-label")
            yield Input(value=self.initial.name if self.initial else "",
                        placeholder="feature-name", id="run-name")
            yield Label("Target repository", classes="field-label")
            yield Input(value=str(self.initial.repo if self.initial else self.default_repo),
                        id="repo-path")
            with Horizontal(id="new-run-options"):
                yield Select(
                    [(name.title(), name) for name in ("auto", "fast", "standard", "critical")],
                    value=self.initial.workflow if self.initial else "auto",
                    allow_blank=False,
                    id="workflow",
                )
                yield Checkbox(
                    "Autonomous (skip spec approval)",
                    value=self.initial.autonomous if self.initial else False,
                    id="autonomous",
                )
            yield Static("", id="new-run-error")
            with Horizontal(id="new-run-buttons"):
                yield Button("Cancel", id="cancel-new-run")
                yield Button("Launch run", variant="primary", id="launch-new-run")

    def on_mount(self) -> None:
        self.query_one("#brief-body", TextArea).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-new-run":
            self.dismiss(None)
            return
        repo = Path(self.query_one("#repo-path", Input).value).expanduser().resolve()
        brief = self.query_one("#brief-body", TextArea).text.strip()
        name = self.query_one("#run-name", Input).value.strip() or brief_name(brief)
        workflow = str(self.query_one("#workflow", Select).value)
        autonomous = self.query_one("#autonomous", Checkbox).value
        error = self.query_one("#new-run-error", Static)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            error.update("Use letters, numbers, dots, underscores, or hyphens for the run name.")
            return
        if not repo.is_dir():
            error.update("The target repository does not exist.")
            return
        if not brief:
            error.update("The product brief cannot be empty.")
            return
        self.dismiss(NewRunRequest(name, repo, brief, workflow, autonomous))


class ClarificationScreen(ModalScreen[str | None]):
    """Answer one material question without leaving the control room."""

    CSS = """
    ClarificationScreen {
        align: center middle;
        background: #02050a 70%;
    }
    #clarification-dialog {
        width: 82;
        height: 24;
        padding: 1 2;
        background: #0d1626;
        border: tall #d2a8ff;
    }
    #clarification-title {
        height: 2;
        color: #d2a8ff;
        text-style: bold;
    }
    #clarification-question {
        height: 5;
        color: #d8e2f0;
    }
    #clarification-answer {
        height: 1fr;
        min-height: 6;
        border: solid #29466b;
    }
    #clarification-error {
        height: 2;
        color: #ff7b72;
    }
    #clarification-buttons {
        height: 3;
        align-horizontal: right;
    }
    #clarification-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, question: str):
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="clarification-dialog"):
            yield Label("MATERIAL CLARIFICATION", id="clarification-title")
            yield Static(self.question, id="clarification-question")
            yield TextArea(placeholder="Answer only what changes the implementation…",
                           id="clarification-answer")
            yield Static("", id="clarification-error")
            with Horizontal(id="clarification-buttons"):
                yield Button("Cancel", id="cancel-clarification")
                yield Button("Answer & resume", variant="primary", id="save-clarification")

    def on_mount(self) -> None:
        self.query_one("#clarification-answer", TextArea).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-clarification":
            self.dismiss(None)
            return
        answer = self.query_one("#clarification-answer", TextArea).text.strip()
        if not answer:
            self.query_one("#clarification-error", Static).update(
                "The answer cannot be empty.")
            return
        self.dismiss(answer)


def _artifact_files(rundir: RunDir) -> list[tuple[str, Path]]:
    candidates = [
        ("Product brief", rundir.path / "brief.md"),
        ("Accepted spec", rundir.path / "spec" / "spec.md"),
        ("Repository research", rundir.path / "research.md"),
        ("Product Owner decision", rundir.path / "product-owner-decision.md"),
        ("Current checkpoint", rundir.path / "checkpoint.md"),
        ("Handoff", rundir.path / "handoff.md"),
        ("PR body", rundir.path / "pr-body.md"),
        ("Knowledge candidates", rundir.path / "knowledge-candidates.json"),
    ]
    return [(label, path) for label, path in candidates if path.is_file()]


def _artifact_text(path: Path) -> str:
    text = path.read_text(errors="replace")
    if path.suffix == ".json":
        return f"```json\n{text}\n```"
    return text


class ArtifactScreen(ModalScreen[None]):
    """Read run artifacts without dropping back to the shell or Finder."""

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [("escape", "close", "Close")]
    CSS = """
    ArtifactScreen {
        align: center middle;
        background: #02050a 70%;
    }
    #artifact-dialog {
        width: 92%;
        height: 92%;
        padding: 1 2;
        background: #0d1626;
        border: tall #4582bd;
    }
    #artifact-title {
        height: 2;
        color: #79c0ff;
        text-style: bold;
    }
    #artifact-select {
        height: 3;
        margin-bottom: 1;
    }
    #artifact-viewer {
        height: 1fr;
        border: solid #29466b;
        background: #080d18;
    }
    #artifact-close-row {
        height: 3;
        align-horizontal: right;
    }
    """

    def __init__(self, rundir: RunDir):
        super().__init__()
        self.rundir = rundir
        self.artifacts = _artifact_files(rundir)

    def compose(self) -> ComposeResult:
        first = self.artifacts[0] if self.artifacts else None
        with Vertical(id="artifact-dialog"):
            yield Label(f"RUN ARTIFACTS  ·  {self.rundir.path.name}", id="artifact-title")
            yield Select(
                [(label, str(path)) for label, path in self.artifacts]
                or [("No readable artifacts yet", "")],
                value=str(first[1]) if first else "",
                allow_blank=False,
                id="artifact-select",
            )
            yield MarkdownViewer(
                _artifact_text(first[1]) if first else "*No readable artifacts yet.*",
                show_table_of_contents=False,
                open_links=True,
                id="artifact-viewer",
            )
            with Horizontal(id="artifact-close-row"):
                yield Button("Close", id="close-artifacts")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "artifact-select" or not event.value:
            return
        path = Path(str(event.value))
        self.query_one("#artifact-viewer", MarkdownViewer).document.update(_artifact_text(path))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close-artifacts":
            self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


def _same_repo(state: RunState, repo: Path | None) -> bool:
    if repo is None:
        return True
    return Path(state.target_repo).expanduser().resolve() == repo.expanduser().resolve()


def resolve_run_id(home: Path, requested: str | None, repo: Path | None = None) -> str:
    """Resolve an explicit run or the latest run for a target repository."""
    runs = home / "runs"
    if requested:
        if not (runs / requested / "state.json").is_file():
            raise FileNotFoundError(requested)
        return requested
    candidates = []
    if runs.is_dir():
        for path in runs.iterdir():
            state_path = path / "state.json"
            if not path.is_dir() or not state_path.is_file():
                continue
            try:
                state = RunState.model_validate_json(state_path.read_text())
            except (OSError, ValueError):
                continue
            if _same_repo(state, repo):
                candidates.append(path)
    if not candidates:
        raise FileNotFoundError("no Régie runs found")
    return max(candidates, key=lambda path: (path / "state.json").stat().st_mtime).name


def available_runs(home: Path, repo: Path | None = None) -> list[RunState]:
    """Load valid runs for one repository in most-recently-updated order."""
    runs_dir = home / "runs"
    if not runs_dir.is_dir():
        return []
    loaded: list[tuple[int, RunState]] = []
    for path in runs_dir.iterdir():
        state_path = path / "state.json"
        if not state_path.is_file():
            continue
        try:
            state = RunState.model_validate_json(state_path.read_text())
            if _same_repo(state, repo):
                loaded.append((state_path.stat().st_mtime_ns, state))
        except (OSError, ValueError):
            continue
    return [state for _, state in sorted(loaded, key=lambda item: item[0], reverse=True)]


def usage_totals(rundir: RunDir) -> UsageTotals:
    """Return lifetime fresh/cache usage from the append-only event ledger."""
    events = [
        event for event in read_events(rundir, limit=None)
        if event.get("kind") == "attempt"
    ]
    fresh_fields = ("new_input_tokens", "cache_write_input_tokens", "output_tokens")
    fresh = sum(
        sum(int((event.get("metrics") or {}).get(name) or 0) for name in fresh_fields)
        for event in events
    )
    cached = sum(
        int((event.get("metrics") or {}).get("cached_input_tokens") or 0)
        for event in events
    )
    cost = sum(
        float((event.get("metrics") or {}).get("cost_usd") or 0)
        for event in events
    )
    return UsageTotals(fresh, cached, cost, len(events))


def read_events(rundir: RunDir, limit: int | None = 200) -> list[dict]:
    path = rundir.path / "events.jsonl"
    if not path.is_file():
        return []
    events = []
    lines = path.read_text(errors="replace").splitlines()
    if limit is not None:
        lines = lines[-limit:]
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _event_time(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _output_times(path: Path) -> tuple[datetime, datetime] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    created = getattr(stat, "st_birthtime", stat.st_ctime)
    return (
        datetime.fromtimestamp(created, UTC),
        datetime.fromtimestamp(stat.st_mtime, UTC),
    )


def agent_activities(
    rundir: RunDir,
    *,
    now: datetime | None = None,
) -> list[AgentActivity]:
    """Project dispatch ledgers and output heartbeats into operator activity rows."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    recorded_statuses: dict[tuple[str, str, int], str] = {}
    try:
        state = rundir.read_state()
    except (OSError, ValueError):
        state = None
    if state is not None:
        for number, attempt_state in enumerate(state.planner_attempts, 1):
            recorded_statuses[("PLAN", "plan", number)] = (
                attempt_state.outcome or "finished"
            )
        for number, attempt_state in enumerate(state.product_owner_attempts, 1):
            recorded_statuses[("PRODUCT-OWNER", "product-owner", number)] = (
                attempt_state.outcome or "finished"
            )
        for number, attempt_state in enumerate(state.final_review_attempts, 1):
            recorded_statuses[("FINAL-REVIEW", "final-review", number)] = (
                attempt_state.outcome or "finished"
            )
        for task_id, task_state in state.tasks.items():
            for stage, attempts in task_state.attempts.items():
                for number, attempt_state in enumerate(attempts, 1):
                    recorded_statuses[(task_id, stage, number)] = (
                        attempt_state.outcome or "finished"
                    )
            for name, attempts in task_state.specialist_attempts.items():
                agent_id = f"{task_id}-{name.upper()}"
                stage = f"review:{name}"
                for number, attempt_state in enumerate(attempts, 1):
                    recorded_statuses[(agent_id, stage, number)] = (
                        attempt_state.outcome or "finished"
                    )
    completions: dict[tuple[str, str, int], list[dict]] = {}
    for event in read_events(rundir, limit=10_000):
        if event.get("kind") != "attempt":
            continue
        try:
            key = (str(event["task"]), str(event["stage"]), int(event["attempt"]))
        except (KeyError, TypeError, ValueError):
            continue
        completions.setdefault(key, []).append(event)

    consumed: dict[tuple[str, str, int], int] = {}
    rows = []
    for intent in rundir.read_intents():
        if intent.get("reset"):
            continue
        try:
            task = str(intent["task"])
            stage = str(intent["stage"])
            attempt = int(intent["attempt"])
        except (KeyError, TypeError, ValueError):
            continue
        key = (task, stage, attempt)
        index = consumed.get(key, 0)
        matches = completions.get(key, [])
        event = matches[index] if index < len(matches) else None
        if event is not None:
            consumed[key] = index + 1

        binding = intent.get("binding") or (event or {}).get("binding") or {}
        provider = str(binding.get("cli") or (event or {}).get("provider") or "unknown")
        model = str(binding.get("model") or "unknown")
        output = rundir.path / "tasks" / task / f"attempt-{attempt}.out"
        output_times = _output_times(output)
        started = _event_time(intent.get("ts"))
        if started is None and output_times is not None:
            started = output_times[0]
        started = started or now

        if event is None:
            status = "running"
            finished = None
            elapsed = max(0.0, (now - started).total_seconds())
            last_activity = output_times[1] if output_times is not None else started
            turns = 0
            fresh_tokens = 0
            cached_tokens = 0
        else:
            quota = event.get("quota") or {}
            if event.get("outcome") == "quota" and quota.get("synthetic"):
                status = "skipped"
            else:
                status = recorded_statuses.get(
                    key,
                    str(event.get("outcome") or "finished"),
                )
            finished = _event_time(event.get("ts")) or now
            elapsed = float(
                event.get("duration_seconds")
                or max(0.0, (finished - started).total_seconds())
            )
            last_activity = finished
            turns = int(event.get("turns") or 0)
            metrics = event.get("metrics") or {}
            fresh_tokens = sum(int(metrics.get(name) or 0) for name in (
                "new_input_tokens", "cache_write_input_tokens", "output_tokens"))
            cached_tokens = int(metrics.get("cached_input_tokens") or 0)
        rows.append(AgentActivity(
            task=task,
            stage=stage,
            attempt=attempt,
            provider=provider.split(":", 1)[0],
            model=model,
            status=status,
            elapsed_seconds=elapsed,
            last_activity=last_activity,
            turns=turns,
            fresh_tokens=fresh_tokens,
            cached_tokens=cached_tokens,
        ))
    return sorted(
        rows,
        key=lambda row: (row.status == "running", row.last_activity),
        reverse=True,
    )


def _duration(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def _activity_age(activity: AgentActivity, now: datetime) -> str:
    if activity.status != "running":
        return activity.last_activity.astimezone().strftime("%H:%M:%S")
    return f"{_duration((now - activity.last_activity).total_seconds())} ago"


def format_event(event: dict) -> Text:
    timestamp = str(event.get("ts", ""))
    try:
        timestamp = datetime.fromisoformat(timestamp).astimezone().strftime("%H:%M:%S")
    except ValueError:
        timestamp = timestamp[11:19] or "--:--:--"
    kind = str(event.get("kind", "event")).replace("_", " ")
    location = "/".join(
        str(value) for value in (event.get("task"), event.get("stage")) if value)
    outcome = event.get("outcome") or event.get("provider") or ""
    rendered = Text(timestamp, style="dim")
    rendered.append("  ")
    rendered.append(kind, style="bold")
    if location:
        rendered.append("  ")
        rendered.append(location, style="cyan")
    if outcome:
        color = "green" if outcome == "done" else "yellow"
        rendered.append("  ")
        rendered.append(str(outcome), style=color)
    return rendered


def _task_detail(task_id: str, task: TaskState) -> Text:
    spec = task.spec
    attempt_count = sum(len(items) for items in task.attempts.values())
    detail = Text()
    detail.append(task_id, style="bold cyan")
    detail.append(f"  {spec.title}\n")
    detail.append("Status", style="bold")
    detail.append(f"  {task.status} / {task.stage}    ")
    detail.append("Profile", style="bold")
    detail.append(f"  {spec.profile}\n")
    for label, value in (
        ("Depends on", ", ".join(spec.depends_on) or "—"),
        ("Scope", ", ".join(spec.file_scope) or "—"),
        ("Risk", ", ".join(spec.risk_tags) or "none"),
        ("Attempts", str(attempt_count)),
    ):
        detail.append(label, style="bold")
        detail.append(f"  {value}\n")
    detail.append("\nAcceptance evidence\n", style="bold")
    if task.criterion_evidence:
        for item in task.criterion_evidence:
            detail.append("✓" if item.passed else "✗",
                          style="green" if item.passed else "red")
            location = f" ({item.file}:{item.line})" if item.file else ""
            detail.append(f" {item.criterion} — {item.evidence}{location}\n")
    else:
        for criterion in spec.criteria:
            detail.append(f"○ {criterion}\n", style="dim")
    findings = [
        finding
        for attempts in task.specialist_attempts.values()
        for attempt in attempts
        for finding in attempt.gate_results
        if not finding.passed
    ]
    if findings:
        detail.append("\nFailing specialist gates\n", style="bold red")
        for item in findings[-5:]:
            detail.append("!", style="red")
            detail.append(f" {item.name} — {item.detail[:180]}\n")
    return detail


def _provider_summary(
    home: Path,
    state: RunState,
    rundir: RunDir,
) -> Text:
    usage = usage_totals(rundir)
    summary = Text("RUN TELEMETRY\n", style="bold #79c0ff")
    summary.append("USAGE  ", style="bold")
    summary.append(
        f"{usage.attempts} attempts · {usage.fresh_tokens:,} fresh · "
        f"{usage.cached_tokens:,} cached · ${usage.cost_usd:,.4f}\n",
        style="cyan",
    )
    summary.append("PR     ", style="bold")
    summary.append(
        f"{state.pr_state.status} · CI {state.pr_state.ci} · "
        f"{state.pr_state.unresolved_threads} threads\n"
    )
    summary.append("REVIEW ", style="bold")
    summary.append(f"{state.pr_state.review_decision or '—'}\n")
    summary.append("MODELS ", style="bold")
    enabled = set(enabled_providers(Path(state.target_repo)))
    provider_parts: list[tuple[str, str]] = []
    for provider, label in (("claude", "Claude"), ("codex", "Codex")):
        if provider not in enabled:
            provider_parts.append((f"○ {label} disabled", "dim"))
            continue
        readiness = health(Binding(cli=provider, model="default"))
        style = "green" if readiness.status == "ready" else "red"
        provider_parts.append((f"● {label} {readiness.status}", style))
    for index, (label, style) in enumerate(provider_parts):
        if index:
            summary.append(" · ")
        summary.append(label, style=style)
    summary.append("\n")
    health_path = home / "provider-health.json"
    try:
        entries = json.loads(health_path.read_text()).get("providers", {})
    except (OSError, json.JSONDecodeError, AttributeError):
        entries = {}
    if entries:
        summary.append("QUOTA  ", style="bold yellow")
        for key, entry in sorted(entries.items()):
            reset = str(entry.get("unavailable_until") or "unknown")
            summary.append(f"! {key} until {reset[:19]}  ", style="yellow")
        summary.append("\n")
    return summary


class ControlRoom(App[None]):
    """Live operator application for creating, switching, and steering runs."""

    TITLE = "Régie Control Room"
    CSS = """
    Screen {
        background: #080d18;
        color: #d8e2f0;
    }
    Header, Footer {
        background: #111a2c;
    }
    #overview {
        height: 7;
        border-bottom: solid #29466b;
    }
    #run-summary {
        width: 62%;
        height: 1fr;
        padding: 0 2;
        background: #0d1626;
        border-right: solid #29466b;
    }
    #operations {
        width: 38%;
        height: 1fr;
        padding: 0 2;
        background: #0d1626;
        overflow-y: auto;
    }
    #main {
        height: 1fr;
    }
    #tasks-pane {
        width: 58%;
        border-right: solid #29466b;
    }
    #detail-pane {
        width: 42%;
    }
    #task-detail {
        height: 1fr;
        padding: 1 2;
        overflow-y: auto;
    }
    .panel-title {
        height: 3;
        padding: 1 2;
        color: #79c0ff;
        text-style: bold;
        background: #0d1626;
    }
    DataTable {
        height: 1fr;
        background: #080d18;
    }
    DataTable > .datatable--header {
        background: #14213a;
        color: #79c0ff;
        text-style: bold;
    }
    DataTable > .datatable--cursor {
        background: #243b63;
    }
    #bottom {
        height: 30%;
        min-height: 7;
        max-height: 15;
        border-top: solid #29466b;
    }
    #events-pane {
        width: 42%;
        border-right: solid #29466b;
    }
    #events {
        height: 1fr;
        padding: 0 1;
        background: #080d18;
    }
    #agents-pane {
        width: 58%;
    }
    #agents {
        height: 1fr;
    }
    """
    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("q", "quit", "Quit"),
        ("r", "refresh_now", "Refresh"),
        ("n", "new_run", "New run"),
        ("l", "runs", "Runs"),
        ("a", "approve", "Approve"),
        ("c", "clarify", "Clarify"),
        ("s", "resume", "Resume"),
        ("o", "open_artifacts", "Artifacts"),
        ("p", "providers", "Providers"),
    ]

    def __init__(self, home: Path, run_id: str | None = None,
                 *, refresh_interval: float = 1.0, default_repo: Path | None = None,
                 auto_compose: bool = True, setup_if_missing: bool = True):
        super().__init__()
        self.home = home
        self.run_id: str | None = None
        self.rundir: RunDir | None = None
        self.refresh_interval = refresh_interval
        self.default_repo = (default_repo or Path.cwd()).resolve()
        self.scope_repo = self.default_repo
        self.auto_compose = auto_compose
        self.setup_if_missing = setup_if_missing
        self._explicit_run = run_id is not None
        self.state: RunState | None = None
        self.selected_task: str | None = None
        self._last_state_json = ""
        self._last_events_mtime = -1
        self._last_agents_signature = ""
        self._agent_row_ids: list[str] = []
        self._rendering_tables = False
        self._pending_run_id: str | None = None
        self._processes: list[ManagedProcess] = []
        self._activity: list[str] = []
        if run_id is not None:
            selected = RunDir.open(home, run_id).read_state()
            self.scope_repo = Path(selected.target_repo).expanduser().resolve()
            self.default_repo = self.scope_repo
            self._select_run(run_id)
        else:
            try:
                self._select_run(resolve_run_id(home, None, self.scope_repo))
            except FileNotFoundError:
                pass
        self.sub_title = self.run_id or "No runs yet"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="overview"):
            yield Static(id="run-summary")
            yield Static(id="operations", markup=True)
        with Horizontal(id="main"):
            with Vertical(id="tasks-pane"):
                yield Static("DEPENDENCY DAG", classes="panel-title")
                yield DataTable(id="tasks", cursor_type="row", zebra_stripes=True)
            with Vertical(id="detail-pane"):
                yield Static("TASK EVIDENCE", classes="panel-title")
                yield Static("No task selected", id="task-detail", markup=True)
        with Horizontal(id="bottom"):
            with Vertical(id="events-pane"):
                yield Static("LIVE EVENT LEDGER", classes="panel-title")
                yield RichLog(id="events", markup=True, wrap=True, highlight=False)
            with Vertical(id="agents-pane"):
                yield Static("AGENT ACTIVITY", classes="panel-title")
                yield DataTable(id="agents", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#tasks", DataTable)
        table.add_columns("Task", "State", "Stage", "Dependencies", "Attempts", "Title")
        self.query_one("#agents", DataTable).add_columns(
            "Agent", "Stage", "Provider", "Model", "State",
            "Runtime", "Heartbeat", "Turns", "Fresh", "Cached",
        )
        self.refresh_data(force=True)
        self.set_interval(self.refresh_interval, self.refresh_data)
        if (not self._explicit_run and self.setup_if_missing
                and not (self.scope_repo / "regie.toml").is_file()):
            self.call_after_refresh(
                lambda: self._open_setup(
                    self.scope_repo,
                    compose_after=self.rundir is None and self.auto_compose,
                )
            )
        elif self.rundir is None and self.auto_compose:
            self.call_after_refresh(self.action_new_run)

    def refresh_data(self, *, force: bool = False) -> None:
        self._poll_processes()
        self._render_runs(force=force)
        if self.rundir is None:
            self._render_empty()
            return
        try:
            state = self.rundir.read_state()
        except (OSError, ValueError) as exc:
            message = Text("State unavailable  ", style="bold red")
            message.append(str(exc))
            self.query_one("#run-summary", Static).update(message)
            return
        serialized = state.model_dump_json()
        self.state = state
        if force or serialized != self._last_state_json:
            self._last_state_json = serialized
            self._render_state(state)
        events_path = self.rundir.path / "events.jsonl"
        try:
            mtime = events_path.stat().st_mtime_ns
        except OSError:
            mtime = 0
        if force or mtime != self._last_events_mtime:
            self._last_events_mtime = mtime
            self._render_events()
        self._render_agents(force=force)

    def _select_run(self, run_id: str) -> None:
        self.run_id = run_id
        self.rundir = RunDir.open(self.home, run_id)
        self.state = None
        self.selected_task = None
        self._last_state_json = ""
        self._last_events_mtime = -1
        self._last_agents_signature = ""
        self._agent_row_ids = []
        self.sub_title = run_id

    def _render_runs(self, *, force: bool = False) -> None:
        del force
        runs = available_runs(self.home, self.scope_repo)
        if self._pending_run_id and any(run.id == self._pending_run_id for run in runs):
            self._select_run(self._pending_run_id)
            self._pending_run_id = None
        if self.run_id is None and runs:
            self._select_run(runs[0].id)

    def _render_empty(self) -> None:
        welcome = Text("RÉGIE CONTROL ROOM\n", style="bold #79c0ff")
        welcome.append("No runs for ")
        welcome.append(str(self.scope_repo), style="bold")
        welcome.append(". Press N to compose one.\n")
        welcome.append("Existing CLI subcommands remain available for scripts and CI.",
                       style="dim")
        self.query_one("#run-summary", Static).update(welcome)
        self.query_one("#tasks", DataTable).clear()
        self.query_one("#agents", DataTable).clear()
        self._agent_row_ids = []
        self.query_one("#task-detail", Static).update(
            "[dim]Create a run to see its dependency DAG and acceptance evidence.[/]")
        self.query_one("#operations", Static).update(
            "[bold]READY[/]\nN  New run\nQ  Quit")
        log = self.query_one("#events", RichLog)
        log.clear()
        log.write("[dim]The event ledger will stream here.[/]")
        for message in self._activity[-10:]:
            activity = Text("CONTROL", style="magenta")
            activity.append(f"  {message}")
            log.write(activity)

    def _render_state(self, state: RunState) -> None:
        done = sum(task.status == "done" for task in state.tasks.values())
        total = len(state.tasks)
        summary = Text()
        summary.append(f"{state.id}\n", style="bold #79c0ff")
        summary.append("STAGE ", style="dim")
        summary.append(state.stage.upper(), style=_STAGE_STYLES.get(state.stage, "bold #f2cc60"))
        summary.append(
            f"    WORKFLOW {state.workflow.upper()}"
            f" → {state.execution_route.upper()}    TASKS {done}/{total}\n"
        )
        summary.append(f"{state.target_repo}    {state.branch}", style="dim")
        if state.route_reason:
            summary.append(f"\nROUTE  {state.route_reason}", style="dim")
        if state.halt_reason:
            summary.append(f"\nHALT  {state.halt_reason}", style="bold #ff7b72")
        else:
            if state.product_owner_decision:
                summary.append(
                    "\nPO RECOVERY  "
                    f"{state.product_owner_decision.action.upper()} — "
                    f"{state.product_owner_decision.summary}",
                    style="bold #d2a8ff",
                )
            if state.stage in {"approve", "checkpoint"}:
                summary.append("\nAWAITING APPROVAL  Press A, then S to continue",
                               style="bold #d2a8ff")
            elif state.stage == "done" and state.pr_url:
                summary.append(f"\nPR READY  {state.pr_url}", style="bold #7ee787")
        self.query_one("#run-summary", Static).update(summary)

        table = self.query_one("#tasks", DataTable)
        self._rendering_tables = True
        try:
            table.clear()
            order = state.ordered_task_ids()
            if self.selected_task not in state.tasks:
                self.selected_task = order[0] if order else None
            for task_id in order:
                task = state.tasks[task_id]
                attempt_count = sum(len(items) for items in task.attempts.values())
                table.add_row(
                    task_id,
                    f"{_STATUS_MARKS.get(task.status, '?')} {task.status}",
                    task.stage,
                    ", ".join(task.spec.depends_on) or "—",
                    str(attempt_count),
                    task.spec.title,
                    key=task_id,
                )
            if self.selected_task and self.selected_task in order:
                table.move_cursor(row=order.index(self.selected_task))
        finally:
            self._rendering_tables = False
        self._render_detail()
        self.query_one("#operations", Static).update(
            _provider_summary(self.home, state, self.rundir)
        )

    def _render_events(self) -> None:
        log = self.query_one("#events", RichLog)
        log.clear()
        events = read_events(self.rundir)
        if not events:
            log.write("[dim]No run events recorded yet.[/]")
        for event in events:
            log.write(format_event(event))
        for message in self._activity[-10:]:
            activity = Text("CONTROL", style="magenta")
            activity.append(f"  {message}")
            log.write(activity)

    def _render_agents(self, *, force: bool = False) -> None:
        if self.rundir is None:
            return
        now = datetime.now(UTC)
        activities = agent_activities(self.rundir, now=now)
        signature = "|".join(
            f"{item.task}:{item.stage}:{item.attempt}:{item.status}:"
            f"{int(item.elapsed_seconds)}:{int(item.last_activity.timestamp())}:"
            f"{item.turns}:{item.fresh_tokens}:{item.cached_tokens}"
            for item in activities
        )
        if not force and signature == self._last_agents_signature:
            return
        self._last_agents_signature = signature
        table = self.query_one("#agents", DataTable)
        status_styles = {
            "running": "bold yellow",
            "done": "bold green",
            "quota": "bold magenta",
            "skipped": "dim magenta",
            "blocked": "bold red",
            "error": "bold red",
            "failed": "bold red",
        }
        rows = []
        row_ids = []
        for index, item in enumerate(activities):
            status = Text(item.status, style=status_styles.get(item.status, ""))
            row_ids.append(f"{item.task}:{item.stage}:{item.attempt}:{index}")
            rows.append((
                f"{item.task} #{item.attempt}",
                item.stage,
                item.provider,
                item.model,
                status,
                _duration(item.elapsed_seconds),
                _activity_age(item, now),
                str(item.turns) if item.turns else "—",
                f"{item.fresh_tokens:,}" if item.fresh_tokens else "—",
                f"{item.cached_tokens:,}" if item.cached_tokens else "—",
            ))

        if row_ids == self._agent_row_ids and table.row_count == len(rows):
            # The one-second clock normally changes only runtime/heartbeat.
            # Updating cells in place preserves DataTable cursor and viewport.
            for row_index, values in enumerate(rows):
                for column_index, value in enumerate(values):
                    table.update_cell_at(
                        Coordinate(row_index, column_index), value, update_width=True
                    )
            return

        cursor_row = table.cursor_row
        cursor_column = table.cursor_column
        selected_id = (
            self._agent_row_ids[cursor_row]
            if 0 <= cursor_row < len(self._agent_row_ids)
            else None
        )
        scroll = table.scroll_offset
        table.clear()
        for row_id, values in zip(row_ids, rows, strict=True):
            table.add_row(*values, key=row_id)
        self._agent_row_ids = row_ids
        if rows:
            restored_row = (
                row_ids.index(selected_id)
                if selected_id in row_ids
                else min(cursor_row, len(rows) - 1)
            )
            table.move_cursor(
                row=max(0, restored_row),
                column=max(0, cursor_column),
                scroll=False,
            )
            table.scroll_to(
                x=scroll.x,
                y=scroll.y,
                animate=False,
                immediate=True,
                force=True,
            )

    def _render_detail(self) -> None:
        if self.state and self.selected_task in self.state.tasks:
            content = _task_detail(self.selected_task, self.state.tasks[self.selected_task])
        else:
            content = "[dim]No task selected.[/]"
        self.query_one("#task-detail", Static).update(content)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if (not self.is_running or not event.data_table.is_mounted
                or self._rendering_tables or event.row_key is None
                or event.row_key.value is None):
            return
        if event.data_table.id == "tasks":
            self.selected_task = str(event.row_key.value)
            self._render_detail()

    def action_refresh_now(self) -> None:
        self.refresh_data(force=True)
        self.notify("Control room refreshed", severity="information", timeout=1)

    def action_approve(self) -> None:
        if self.state is None or self.rundir is None:
            return
        try:
            detail = approve_waiting_state(self.rundir, self.state)
        except ValueError as exc:
            self.notify(str(exc), severity="warning")
            return
        self.refresh_data(force=True)
        self.notify(detail, severity="information")

    def action_clarify(self) -> None:
        if self.state is None or self.rundir is None:
            return
        reason = self.state.halt_reason or ""
        marker = "clarify:"
        if self.state.stage != "halted" or marker not in reason.lower():
            self.notify("The selected run is not awaiting clarification",
                        severity="warning")
            return
        index = reason.lower().index(marker)
        question = reason[index + len(marker):].strip()
        self.push_screen(ClarificationScreen(question), self._finish_clarification)

    def _finish_clarification(self, answer: str | None) -> None:
        if answer is None or self.state is None or self.rundir is None:
            return
        try:
            record_clarification(self.rundir, self.state, answer)
        except ValueError as exc:
            self.notify(str(exc), severity="warning")
            return
        self._activity.append(f"answered clarification for {self.state.id}")
        self.action_resume()

    def action_new_run(self) -> None:
        if not (self.default_repo / "regie.toml").is_file():
            self._open_setup(self.default_repo, compose_after=True)
            return
        self.push_screen(NewRunScreen(self.default_repo), self._start_new_run)

    def action_runs(self) -> None:
        runs = available_runs(self.home, self.scope_repo)
        if not runs:
            self.notify("No runs for this repository", severity="information")
            return
        self.push_screen(RunScreen(runs, self.run_id), self._choose_run)

    def _choose_run(self, run_id: str | None) -> None:
        if run_id is None or run_id == self.run_id:
            return
        self._select_run(run_id)
        self.refresh_data(force=True)

    def _open_setup(
        self,
        repo: Path,
        *,
        deferred_request: NewRunRequest | None = None,
        compose_after: bool = False,
    ) -> None:
        target = repo.resolve()
        self.push_screen(
            SetupScreen(target),
            lambda detection: self._finish_setup(
                target,
                detection,
                deferred_request=deferred_request,
                compose_after=compose_after,
            ),
        )

    def _finish_setup(
        self,
        repo: Path,
        request: SetupRequest | None,
        *,
        deferred_request: NewRunRequest | None,
        compose_after: bool,
    ) -> None:
        if request is None:
            self._activity.append(f"setup deferred for {repo}")
            self.refresh_data(force=True)
            if deferred_request is not None:
                self.push_screen(
                    NewRunScreen(deferred_request.repo, deferred_request),
                    self._start_new_run,
                )
            return
        try:
            initialize(repo, request.detection, request.enabled_providers)
        except OSError as exc:
            message = f"Could not initialize {repo / 'regie.toml'}: {exc}"
            self._activity.append(message)
            self.notify(message, severity="error")
            self.refresh_data(force=True)
            if deferred_request is not None:
                self.push_screen(
                    NewRunScreen(deferred_request.repo, deferred_request),
                    self._start_new_run,
                )
            return
        self._activity.append(
            f"configured {request.detection.language} project with "
            f"{', '.join(request.enabled_providers)} "
            f"(test: {request.detection.test})"
        )
        self.notify("Project setup saved to regie.toml", severity="information")
        self.refresh_data(force=True)
        if deferred_request is not None:
            self._start_new_run(deferred_request)
        elif compose_after:
            self.call_after_refresh(self.action_new_run)

    def _start_new_run(self, request: NewRunRequest | None) -> None:
        if request is None:
            return
        request_repo = request.repo.resolve()
        if request_repo != self.scope_repo:
            self.scope_repo = request_repo
            self.default_repo = request_repo
            self.run_id = None
            self.rundir = None
            self.state = None
            self.selected_task = None
            self._last_state_json = ""
            self._last_events_mtime = -1
        run_id = f"{datetime.now(UTC).date().isoformat()}-{request.name}"
        if (self.home / "runs" / run_id).exists():
            self.notify(f"Run {run_id} already exists", severity="error")
            return
        config_path = request_repo / "regie.toml"
        if not config_path.exists():
            self._open_setup(
                request_repo,
                deferred_request=request,
            )
            return
        intake = self.home / "intake"
        intake.mkdir(parents=True, exist_ok=True)
        brief_path = intake / f"{request.name}.md"
        brief_path.write_text(request.brief.rstrip() + "\n")
        args = [
            "run", str(brief_path), "--repo", str(request.repo),
            "--workflow", request.workflow,
        ]
        if request.autonomous:
            args.append("--autonomous")
        self._pending_run_id = run_id
        self._spawn_engine(args, request.repo, f"launching {run_id}")

    def action_resume(self) -> None:
        if self.state is None:
            self.notify("Select a run first", severity="warning")
            return
        if self.state.stage in {"done", "approve", "checkpoint"}:
            action = "approve it first" if self.state.stage in {"approve", "checkpoint"} else ""
            self.notify(
                f"Run is {self.state.stage}" + (f"; {action}" if action else ""),
                severity="warning",
            )
            return
        if any(managed.process.poll() is None and self.state.id in managed.label
               for managed in self._processes):
            self.notify(f"{self.state.id} is already running", severity="warning")
            return
        self._spawn_engine(
            ["resume", self.state.id, "--repo", self.state.target_repo],
            Path(self.state.target_repo),
            f"resuming {self.state.id}",
        )

    def _spawn_engine(self, args: list[str], cwd: Path, label: str) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        log_path = self.home / "control-room.log"
        env = os.environ.copy()
        env["REGIE_NOTIFICATIONS"] = "0"
        with log_path.open("a") as log:
            log.write(f"\n[{datetime.now(UTC).isoformat()}] {label}\n")
            log.flush()
            log_offset = log.tell()
            try:
                process = subprocess.Popen(
                    [sys.executable, "-m", "regie.cli", *args],
                    cwd=cwd,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    env=env,
                )
            except OSError as exc:
                self.notify(f"Could not start Régie: {exc}", severity="error")
                return
        self._processes.append(ManagedProcess(process, label, log_path, log_offset))
        self._activity.append(label)
        self.notify(f"{label}; watching state and events", severity="information")
        self.set_timer(0.25, lambda: self.refresh_data(force=True))

    def _poll_processes(self) -> None:
        active = []
        for managed in self._processes:
            return_code = managed.process.poll()
            if return_code is None:
                active.append(managed)
                continue
            outcome = "completed" if return_code == 0 else f"stopped (exit {return_code})"
            message = f"{managed.label}: {outcome}"
            if return_code != 0:
                detail = process_failure_detail(managed.log_path, managed.log_offset)
                if detail:
                    message = f"{message} — {detail}"
            self._activity.append(message)
            severity = "information" if return_code == 0 else "error"
            self.notify(message, severity=severity)
        self._processes = active

    def action_open_artifacts(self) -> None:
        if self.rundir is None:
            self.notify("Select a run first", severity="warning")
            return
        self.push_screen(ArtifactScreen(self.rundir))

    def action_providers(self) -> None:
        repo = self.default_repo
        if not (repo / "regie.toml").is_file():
            self._open_setup(repo, compose_after=self.rundir is None and self.auto_compose)
            return
        self.push_screen(ProviderScreen(repo), self._save_providers)

    def _save_providers(self, providers: tuple[str, ...] | None) -> None:
        if providers is None:
            return
        try:
            update_providers(self.default_repo, providers)
        except (OSError, ValueError) as exc:
            self.notify(f"Could not save provider settings: {exc}", severity="error")
            return
        names = ", ".join(providers)
        self._activity.append(f"enabled providers: {names}")
        self.notify(
            f"Enabled providers: {names}. Applies to the next run or resume.",
            severity="information",
        )
        self.refresh_data(force=True)
