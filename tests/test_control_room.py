from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import Button, Checkbox, DataTable, Input, Static, TextArea

from regie.control_room import (
    ArtifactScreen,
    ClarificationScreen,
    ControlRoom,
    NewRunScreen,
    ProviderScreen,
    RunScreen,
    SetupScreen,
    agent_activities,
    available_runs,
    brief_name,
    format_event,
    process_failure_detail,
    read_events,
    resolve_run_id,
    usage_totals,
)
from regie.models import (
    Attempt,
    Binding,
    ProductOwnerDecision,
    RunState,
    TaskSpec,
    TaskState,
    UsageMetrics,
)
from regie.rundir import RunDir


def _seed(home: Path, run_id: str, repo: Path, *, stage="tasks") -> RunDir:
    rundir = RunDir.create(home, run_id)
    state = RunState(
        id=run_id,
        target_repo=str(repo),
        branch=f"regie/{run_id}",
        stage=stage,
        workflow="standard",
    )
    state.tasks["T1"] = TaskState(
        spec=TaskSpec(
            id="T1",
            title="Build the control room",
            profile="builder",
            criteria=[
                'Given `bindings`, expose `[Binding(cli="claude", model="sonnet")]`.'
            ],
            risk_tags=["ui"],
        )
    )
    rundir.write_state(state)
    return rundir


def test_resolve_and_list_runs_by_latest_update(tmp_path):
    home = tmp_path / "home"
    old = _seed(home, "old", tmp_path)
    new = _seed(home, "new", tmp_path)
    now = datetime.now(UTC).timestamp()
    os.utime(old.path / "state.json", (now - 30, now - 30))
    os.utime(new.path / "state.json", (now, now))

    assert resolve_run_id(home, None) == "new"
    assert resolve_run_id(home, "old") == "old"
    assert [state.id for state in available_runs(home)] == ["new", "old"]


def test_resolve_and_list_runs_can_be_scoped_to_repo(tmp_path):
    home = tmp_path / "home"
    repo = tmp_path / "current-project"
    other_repo = tmp_path / "other-project"
    repo.mkdir()
    other_repo.mkdir()
    current = _seed(home, "current", repo)
    foreign = _seed(home, "foreign", other_repo)
    now = datetime.now(UTC).timestamp()
    os.utime(current.path / "state.json", (now - 30, now - 30))
    os.utime(foreign.path / "state.json", (now, now))

    assert resolve_run_id(home, None, repo) == "current"
    assert [state.id for state in available_runs(home, repo)] == ["current"]
    # Explicit navigation is intentionally allowed to cross workspace scopes.
    assert resolve_run_id(home, "foreign", repo) == "foreign"


def test_brief_name_is_inferred_from_first_meaningful_line():
    assert brief_name("\n# Ajouter l’écran de contrôle\n\nMore detail") == (
        "ajouter-l-ecran-de-controle"
    )


def test_process_failure_detail_reads_only_the_current_launch(tmp_path):
    log_path = tmp_path / "control-room.log"
    log_path.write_text("old failure\n")
    offset = log_path.stat().st_size
    with log_path.open("a") as log:
        log.write("╭─ traceback ─╮\n│ details\n╰─╯\nConfigError: missing regie.toml\n")

    assert process_failure_detail(log_path, offset) == "ConfigError: missing regie.toml"


def test_event_rendering():
    rendered = format_event({
        "ts": "2026-08-17T10:00:00+00:00",
        "kind": "attempt",
        "task": "T1",
        "stage": "review",
        "outcome": "done",
    })
    assert "attempt" in rendered.plain
    assert "T1/review" in rendered.plain
    assert "done" in rendered.plain

    failed = format_event({
        "ts": "2026-08-17T10:00:00+00:00",
        "kind": "attempt",
        "task": "T1",
        "stage": "review",
        "outcome": "error",
        "failure_kind": "infrastructure",
    })
    assert "error:infrastructure" in failed.plain


def test_usage_totals_use_lifetime_event_ledger_after_state_reset(
        regie_home, fixture_repo):
    rundir = _seed(regie_home, "lifetime-usage", fixture_repo)
    # This attempt is no longer in state after a human-mediated ladder reset.
    rundir.append_event({
        "kind": "attempt",
        "task": "PLAN-PLAN-FEASIBILITY",
        "stage": "plan-review:plan-feasibility",
        "attempt": 1,
        "outcome": "done",
        "metrics": {
            "new_input_tokens": 10,
            "cached_input_tokens": 20,
            "cache_write_input_tokens": 30,
            "output_tokens": 40,
            "reasoning_output_tokens": 25,
            "cost_usd": 0.5,
        },
    })

    usage = usage_totals(rundir)
    assert (usage.fresh_tokens, usage.cached_tokens, usage.cost_usd, usage.attempts) == (
        80, 20, 0.5, 1)


def test_product_owner_attempt_is_counted_and_projected(regie_home, fixture_repo):
    rundir = _seed(regie_home, "po-activity", fixture_repo)
    state = rundir.read_state()
    state.product_owner_attempts.append(Attempt(
        binding=Binding(cli="codex", model="gpt-5.6-sol"),
        outcome="done",
        metrics=UsageMetrics(new_input_tokens=80, output_tokens=20, cost_usd=0.1),
    ))
    state.product_owner_decision = ProductOwnerDecision(
        action="revise",
        summary="Resolve conflicting scope feedback.",
        directives=["Keep the brief's explicit non-goal."],
    )
    rundir.write_state(state)
    rundir.append_intent({
        "task": "PRODUCT-OWNER",
        "stage": "product-owner",
        "attempt": 1,
        "binding": {"cli": "codex", "model": "gpt-5.6-sol"},
    })
    rundir.append_event({
        "kind": "attempt",
        "task": "PRODUCT-OWNER",
        "stage": "product-owner",
        "attempt": 1,
        "outcome": "done",
        "duration_seconds": 4,
        "metrics": {
            "new_input_tokens": 80,
            "output_tokens": 20,
            "cost_usd": 0.1,
        },
    })

    usage = usage_totals(rundir)
    assert (usage.fresh_tokens, usage.cached_tokens, usage.cost_usd, usage.attempts) == (
        100, 0, 0.1, 1)
    activity = agent_activities(rundir)[0]
    assert (activity.task, activity.stage, activity.status) == (
        "PRODUCT-OWNER", "product-owner", "done"
    )


def test_agent_activity_projects_running_and_completed_attempts(regie_home, fixture_repo):
    rundir = _seed(regie_home, "activity", fixture_repo)
    rundir.append_intent({
        "task": "PLAN",
        "stage": "plan",
        "attempt": 1,
        "binding": {"cli": "claude", "model": "opus", "auth": "subscription"},
    })
    output = rundir.task_dir("PLAN") / "attempt-1.out"
    output.write_text('{"type":"system","subtype":"thinking_tokens"}\n')

    running = agent_activities(rundir)

    assert len(running) == 1
    assert running[0].status == "running"
    assert (running[0].provider, running[0].model) == ("claude", "opus")
    assert running[0].elapsed_seconds >= 0

    rundir.append_event({
        "kind": "attempt",
        "task": "PLAN",
        "stage": "plan",
        "attempt": 1,
        "outcome": "done",
        "turns": 7,
        "binding": {"cli": "claude", "model": "opus", "auth": "subscription"},
        "duration_seconds": 12.5,
        "metrics": {"new_input_tokens": 100, "output_tokens": 25},
    })

    completed = agent_activities(rundir)
    assert completed[0].status == "done"
    assert completed[0].elapsed_seconds == 12.5
    assert completed[0].turns == 7
    assert completed[0].fresh_tokens == 125
    assert completed[0].cached_tokens == 0

    rundir.append_intent({
        "task": "PLAN",
        "stage": "plan",
        "attempt": 2,
        "binding": {"cli": "claude", "model": "fable", "auth": "subscription"},
    })
    rundir.append_event({
        "kind": "attempt",
        "task": "PLAN",
        "stage": "plan",
        "attempt": 2,
        "outcome": "quota",
        "quota": {"synthetic": True},
    })

    projected = agent_activities(rundir)
    assert projected[0].status == "skipped"


@pytest.mark.asyncio
async def test_control_room_renders_run_and_approves(regie_home, fixture_repo):
    rundir = _seed(regie_home, "r1", fixture_repo, stage="approve")
    (rundir.path / "brief.md").write_text("# Build a control room\n")
    rundir.append_intent({
        "task": "T1", "stage": "test", "attempt": 1,
        "binding": {"cli": "codex", "model": "gpt-5.6-terra"},
    })
    rundir.append_event({
        "kind": "attempt", "task": "T1", "stage": "test", "attempt": 1,
        "outcome": "done", "turns": 3,
        "metrics": {"new_input_tokens": 20, "output_tokens": 10},
    })
    app = ControlRoom(regie_home, "r1", refresh_interval=60)

    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.pause()
        assert len(app.query("#runs")) == 0
        assert app.query_one("#tasks", DataTable).row_count == 1
        assert app.query_one("#agents", DataTable).row_count == 1
        assert app.query_one("#operations").parent.id == "overview"
        assert len(app.query_one("#bottom").children) == 2
        rendered_summary = str(app.query_one("#run-summary", Static).render())
        assert "STAGE APPROVE" in rendered_summary
        assert "TOKENS" not in rendered_summary
        assert "COST" not in rendered_summary
        assert "Build the control room" in str(
            app.query_one("#task-detail", Static).render())

        await pilot.press("o")
        await pilot.pause()
        assert isinstance(app.screen, ArtifactScreen)
        assert app.screen.artifacts[0][0] == "Product brief"
        assert "Build a control room" in app.screen.artifacts[0][1].read_text()
        await pilot.press("escape")
        await pilot.pause()

        await pilot.press("a")
        await pilot.pause()

    assert rundir.read_state().stage == "tasks"
    assert any(event.get("kind") == "operator_action" for event in read_events(rundir))


@pytest.mark.asyncio
async def test_agent_refresh_preserves_cursor_and_horizontal_scroll(
        regie_home, fixture_repo):
    rundir = _seed(regie_home, "stable-agent-view", fixture_repo)
    for number in range(1, 6):
        rundir.append_intent({
            "task": f"LONG-AGENT-NAME-{number}",
            "stage": "plan-review:architecture-design-reviewer",
            "attempt": 1,
            "binding": {"cli": "codex", "model": "gpt-5.6-sol"},
        })
        rundir.append_event({
            "kind": "attempt",
            "task": f"LONG-AGENT-NAME-{number}",
            "stage": "plan-review:architecture-design-reviewer",
            "attempt": 1,
            "outcome": "done",
            "duration_seconds": number,
            "metrics": {"new_input_tokens": number * 100},
        })
    app = ControlRoom(regie_home, "stable-agent-view", refresh_interval=60)

    async with app.run_test(size=(120, 42)) as pilot:
        await pilot.pause()
        table = app.query_one("#agents", DataTable)
        table.focus()
        table.move_cursor(row=3)
        table.scroll_to(x=40, animate=False, immediate=True, force=True)
        await pilot.pause()
        before_cursor = table.cursor_row
        before_scroll_x = table.scroll_offset.x
        assert before_scroll_x > 0

        app._render_agents(force=True)
        await pilot.pause()

        assert table.cursor_row == before_cursor
        assert table.scroll_offset.x == before_scroll_x


@pytest.mark.asyncio
async def test_empty_control_room_opens_new_run_composer(regie_home, fixture_repo):
    app = ControlRoom(regie_home, refresh_interval=60, default_repo=fixture_repo)
    spawned = []
    app._spawn_engine = lambda args, cwd, label: spawned.append((args, cwd, label))

    async with app.run_test(size=(150, 50)) as pilot:
        await pilot.pause()
        assert app.run_id is None
        assert isinstance(app.screen, SetupScreen)
        assert app.screen.detection.language == "unknown"
        app.screen.query_one("#setup-test", Input).value = "pytest -q"
        app.screen.query_one("#setup-provider-codex", Checkbox).value = False
        app.screen.query_one("#save-setup", Button).press()
        await pilot.pause()
        assert isinstance(app.screen, NewRunScreen)
        brief_editor = app.screen.query_one("#brief-body", TextArea)
        assert brief_editor.has_focus
        brief_editor.text = "# Build the requested feature\n\nKeep it focused."
        app.screen.query_one("#launch-new-run", Button).press()
        await pilot.pause()

    assert spawned
    args, cwd, _ = spawned[0]
    assert args[0] == "run" and "--workflow" in args
    assert cwd == fixture_repo.resolve()
    brief_path = Path(args[1])
    assert brief_path.name == "build-the-requested-feature.md"
    assert brief_path.read_text() == "# Build the requested feature\n\nKeep it focused.\n"
    config = fixture_repo / "regie.toml"
    assert config.is_file()
    assert '[commands]' in config.read_text()
    assert 'test = "pytest -q"' in config.read_text()
    assert 'enabled = ["claude"]' in config.read_text()


@pytest.mark.asyncio
async def test_bare_control_room_does_not_show_runs_from_other_repos(
        regie_home, fixture_repo, tmp_path):
    _seed(regie_home, "regie-itself", fixture_repo)
    new_workspace = tmp_path / "blank-project"
    new_workspace.mkdir()
    app = ControlRoom(
        regie_home,
        refresh_interval=60,
        default_repo=new_workspace,
        auto_compose=False,
        setup_if_missing=False,
    )

    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.pause()
        assert app.run_id is None
        assert len(app.query("#runs")) == 0
        summary = str(app.query_one("#run-summary", Static).render())
        assert str(new_workspace.resolve()) in summary


@pytest.mark.asyncio
async def test_changed_repo_setup_cancel_preserves_brief(regie_home, fixture_repo, tmp_path):
    (fixture_repo / "regie.toml").write_text(
        'test_globs = ["tests/**"]\n[commands]\ntest = "true"\nlint = "true"\n'
    )
    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    app = ControlRoom(regie_home, refresh_interval=60, default_repo=fixture_repo)

    async with app.run_test(size=(150, 50)) as pilot:
        await pilot.pause()
        composer = app.screen
        assert isinstance(composer, NewRunScreen)
        composer.query_one("#brief-body", TextArea).text = "A valuable draft"
        composer.query_one("#repo-path", Input).value = str(other_repo)
        composer.query_one("#launch-new-run", Button).press()
        await pilot.pause()
        assert isinstance(app.screen, SetupScreen)

        app.screen.query_one("#cancel-setup", Button).press()
        await pilot.pause()
        assert isinstance(app.screen, NewRunScreen)
        assert app.screen.query_one("#brief-body", TextArea).text == "A valuable draft"
        assert app.screen.query_one("#repo-path", Input).value == str(other_repo)


@pytest.mark.asyncio
async def test_empty_task_table_highlight_event_is_ignored(
        regie_home, fixture_repo):
    app = ControlRoom(
        regie_home,
        refresh_interval=60,
        default_repo=fixture_repo,
        auto_compose=False,
        setup_if_missing=False,
    )

    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        tasks = app.query_one("#tasks", DataTable)
        app.on_data_table_row_highlighted(
            SimpleNamespace(data_table=tasks, row_key=None)
        )


@pytest.mark.asyncio
async def test_provider_settings_are_editable_from_control_room(regie_home, fixture_repo):
    (fixture_repo / "regie.toml").write_text(
        'test_globs = ["tests/**"]\n'
        '[commands]\ntest = "true"\nlint = "true"\n'
        '[providers]\nenabled = ["claude", "codex"]\n'
    )
    _seed(regie_home, "configured-run", fixture_repo)
    app = ControlRoom(regie_home, "configured-run", refresh_interval=60)

    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        assert isinstance(app.screen, ProviderScreen)
        app.screen.query_one("#provider-claude", Checkbox).value = False
        app.screen.query_one("#save-providers", Button).press()
        await pilot.pause()

    assert 'enabled = ["codex"]' in (fixture_repo / "regie.toml").read_text()


@pytest.mark.asyncio
async def test_run_switch_requires_intentional_selection(regie_home, fixture_repo):
    _seed(regie_home, "older", fixture_repo)
    _seed(regie_home, "newer", fixture_repo)
    app = ControlRoom(regie_home, "older", refresh_interval=60)

    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        assert app.run_id == "older"
        await pilot.press("l")
        await pilot.pause()
        assert isinstance(app.screen, RunScreen)
        runs = app.screen.query_one("#run-picker", DataTable)
        runs.focus()
        runs.move_cursor(row=runs.get_row_index("newer"))
        await pilot.press("enter")
        await pilot.pause()
        assert app.run_id == "newer"


@pytest.mark.asyncio
async def test_resume_action_routes_through_existing_cli(regie_home, fixture_repo):
    _seed(regie_home, "halted-run", fixture_repo, stage="halted")
    app = ControlRoom(regie_home, "halted-run", refresh_interval=60)
    spawned = []
    app._spawn_engine = lambda args, cwd, label: spawned.append((args, cwd, label))

    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()

    assert spawned[0][0] == ["resume", "halted-run", "--repo", str(fixture_repo)]
    assert spawned[0][1] == fixture_repo


@pytest.mark.asyncio
async def test_clarification_is_answered_and_resumed_inside_control_room(
        regie_home, fixture_repo):
    rundir = _seed(regie_home, "clarify-run", fixture_repo, stage="halted")
    state = rundir.read_state()
    state.halt_reason = "blocked: clarify: Should selection use Shift-click?"
    state.tasks["T1"].status = "blocked"
    rundir.write_state(state)
    app = ControlRoom(regie_home, "clarify-run", refresh_interval=60)
    spawned = []
    app._spawn_engine = lambda args, cwd, label: spawned.append((args, cwd, label))

    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, ClarificationScreen)
        app.screen.query_one("#clarification-answer", TextArea).text = (
            "Yes, Shift-click extends the selection.")
        app.screen.query_one("#save-clarification", Button).press()
        await pilot.pause()

    assert "Shift-click extends" in (rundir.path / "decisions.md").read_text()
    assert spawned[0][0] == [
        "resume", "clarify-run", "--repo", str(fixture_repo)]
