from __future__ import annotations

import atexit
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from regie.config import RegieConfig, load_config
from regie.gitops import (
    GitError,
    create_run_worktree,
    delete_branch,
    fetch_base_sha,
    git,
    head_sha,
    remove_run_worktree,
)
from regie.models import ChildRun, RunState, TaskSpec, TaskState
from regie.notify import notify
from regie.pipeline import (
    apply_direct_brief,
    finalize_stage,
    plan_stage,
    pr_stage,
    reconcile,
    reflect_stage,
    run_tasks_stage,
)
from regie.provider_health import ProviderHealthStore
from regie.rundir import RunDir, RunLocked
from regie.workflow import route_brief

app = typer.Typer(add_completion=False, invoke_without_command=True, no_args_is_help=False)

_DEFAULT_PROFILES = Path(__file__).parent.parent.parent / "profiles"
_DEFAULT_REPO = Path.cwd()


def _home() -> Path:
    return Path(os.environ.get("REGIE_HOME", Path.home() / ".regie"))


@app.callback()
def main(ctx: typer.Context) -> None:
    """Régie control room; subcommands remain available for automation."""
    if ctx.invoked_subcommand is None:
        from regie.control_room import ControlRoom

        ControlRoom(_home(), default_repo=Path.cwd()).run()


def _repo_marker_path(home: Path, repo: Path) -> Path:
    digest = hashlib.sha256(str(repo).encode()).hexdigest()[:16]
    return home / "active" / digest


def _live_run_blocking(home: Path, other_run_id: str) -> bool:
    """True iff other_run_id's run.lock is currently held by a live process."""
    try:
        rd = RunDir.open(home, other_run_id)
    except FileNotFoundError:
        return False
    try:
        rd.acquire_lock()
    except RunLocked:
        return True
    rd.release_lock()
    return False


def _check_live_guard(home: Path, repo: Path, run_id: str) -> None:
    """Guard against two live runs against the same target repo. When called
    for a run_id that already owns the marker, this process must be holding
    that run's OWN lock at this point -- that isn't "another" live run."""
    marker = _repo_marker_path(home, repo)
    if marker.exists():
        other_run_id = marker.read_text().strip()
        if other_run_id != run_id and _live_run_blocking(home, other_run_id):
            typer.echo("another run is live against this repo", err=True)
            raise typer.Exit(2)


def _mark_live(home: Path, repo: Path, run_id: str) -> None:
    marker = _repo_marker_path(home, repo)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(run_id)
    atexit.register(lambda: marker.unlink(missing_ok=True))


def _guard_and_mark(home: Path, repo: Path, run_id: str) -> None:
    """Guard against two live runs against the same target repo. Must be called
    only after this process holds its OWN run's lock."""
    _check_live_guard(home, repo, run_id)
    _mark_live(home, repo, run_id)


def _resolve_base_sha(repo: Path, base_branch: str) -> str:
    try:
        return fetch_base_sha(repo, base_branch)
    except GitError:
        pass
    try:
        return head_sha(repo, base_branch)
    except GitError:
        return head_sha(repo, "HEAD")


def _ensure_worktree(repo: Path, branch: str, base_sha: str, path: Path) -> Path:
    if path.exists():
        return path
    try:
        return create_run_worktree(repo, branch, base_sha, path)
    except GitError:
        git(repo, "worktree", "add", str(path), branch)
        return path


def _open_rundir(home: Path, run_id: str) -> RunDir:
    try:
        return RunDir.open(home, run_id)
    except FileNotFoundError:
        typer.echo(f"run {run_id} not found", err=True)
        raise typer.Exit(2) from None


def _print_approve_hint(rundir: RunDir, state: RunState) -> None:
    artifact = (rundir.path / "checkpoint.md" if state.stage == "checkpoint"
                else rundir.path / "spec" / "spec.md")
    typer.echo(f"{'checkpoint' if state.stage == 'checkpoint' else 'spec'} ready: {artifact}")
    typer.echo(f"run `regie approve {state.id}` to continue")


def _advance(rundir: RunDir, state: RunState, cfg: RegieConfig, worktree: Path) -> None:
    """From stage "tasks" onward: run tasks, then finalize, then the PR stage
    (squash, scribe, push, CI watch with gated debugger rounds)."""
    if state.stage == "plan":
        plan_stage(rundir, state, cfg, worktree)
        if _after_plan_stage(rundir, state):
            return
    if state.stage == "tasks":
        run_tasks_stage(rundir, state, cfg, worktree)
    # A direct owner may discover concrete evidence that planning is needed.
    if state.stage == "plan":
        plan_stage(rundir, state, cfg, worktree)
        if _after_plan_stage(rundir, state):
            return
    if state.stage == "halted":
        _finish(state)
        return
    if state.stage == "finalize":
        finalize_stage(rundir, state, cfg, worktree)
    if state.stage == "halted":
        _finish(state)
        return
    if state.stage == "pr":
        pr_stage(rundir, state, cfg, worktree)
    if state.stage == "halted":
        _finish(state)
        return
    if state.stage == "reflect":
        reflect_stage(rundir, state, cfg)
    if state.stage == "done":
        typer.echo(f"PR ready: {state.pr_url}")
        notify("regie PR ready", f"{state.id}: {state.pr_url}")
        return
    _finish(state)


def _after_plan_stage(rundir: RunDir, state: RunState) -> bool:
    """Handles the outcomes of plan_stage that stop this invocation here.
    Returns True if the caller should return now."""
    if state.stage == "approve":
        _print_approve_hint(rundir, state)
        return True
    if state.stage == "halted":
        _finish(state)
        return True
    return False


@app.command()
def run(brief: Path, repo: Annotated[Path, typer.Option()],
        profiles: Annotated[Path, typer.Option()] = _DEFAULT_PROFILES,
        autonomous: Annotated[bool, typer.Option("--autonomous")] = False,
        tasks_file: Annotated[Path | None, typer.Option("--tasks-file")] = None,
        workflow: Annotated[str, typer.Option("--workflow")] = "auto",
        parent: Annotated[str | None, typer.Option("--parent")] = None):
    if not brief.exists():
        typer.echo(f"brief not found: {brief}", err=True)
        raise typer.Exit(2)
    cfg = load_config(repo, profiles)
    home = _home()
    run_id = f"{datetime.now(tz=UTC).date().isoformat()}-{brief.stem}"
    _check_live_guard(home, repo, run_id)
    try:
        rundir = RunDir.create(home, run_id)
    except FileExistsError:
        typer.echo(f"run {run_id} already exists — pick a different brief name", err=True)
        raise typer.Exit(2)
    rundir.acquire_lock()
    _mark_live(home, repo, run_id)

    brief_text = brief.read_text()
    (rundir.path / "brief.md").write_text(brief_text)

    base = _resolve_base_sha(repo, cfg.base_branch)
    wt = create_run_worktree(repo, f"regie/{run_id}", base, home / "worktrees" / run_id)

    state = RunState(id=run_id, target_repo=str(repo), branch=f"regie/{run_id}",
                     base_sha=base, base_branch=cfg.base_branch, worktree_path=str(wt),
                     autonomous=autonomous, stage="intake", workflow=workflow,
                     parent_id=parent)
    if parent:
        parent_dir = _open_rundir(home, parent)
        parent_state = parent_dir.read_state()
        parent_state.children.append(ChildRun(
            run_id=run_id, repo=str(repo), status="running"))
        parent_dir.write_state(parent_state)

    if tasks_file is not None:
        specs = [TaskSpec(**t) for t in json.loads(tasks_file.read_text())]
        state.tasks = {s.id: TaskState(spec=s) for s in specs}
        state.stage = "tasks"
        rundir.write_state(state)
    else:
        route, reason = route_brief(brief_text, workflow, cfg)
        state.execution_route = route
        state.route_reason = reason
        if route == "direct":
            apply_direct_brief(rundir, state, brief_text)
        else:
            state.stage = "plan"
            rundir.append_event({
                "kind": "workflow_routed",
                "task": "PLAN",
                "stage": "intake",
                "route": "planned",
                "reason": reason,
            })
            rundir.write_state(state)

    _advance(rundir, state, cfg, Path(state.worktree_path))


@app.command()
def resume(run_id: str, repo: Annotated[Path, typer.Option()],
           profiles: Annotated[Path, typer.Option()] = _DEFAULT_PROFILES):
    cfg = load_config(repo, profiles)
    home = _home()
    rundir = _open_rundir(home, run_id)
    try:
        rundir.acquire_lock()
    except RunLocked:
        typer.echo("run is live in another process; refusing", err=True)
        raise typer.Exit(2)
    _guard_and_mark(home, repo, run_id)

    state = rundir.read_state()
    worktree = Path(state.worktree_path) if state.worktree_path else home / "worktrees" / run_id
    if not worktree.exists():
        worktree = _ensure_worktree(repo, state.branch, state.base_sha, worktree)
        state.worktree_path = str(worktree)
        rundir.write_state(state)

    fixed = reconcile(rundir, state, worktree)
    if fixed:
        typer.echo(f"reconciled {fixed} orphaned attempt(s)")
    if state.stage == "halted":
        state.halt_reason = None
        if state.pushed:
            # The halt happened at or after the first push (e.g. mid CI-watch,
            # or during a debugger round) -- tasks are done and history is
            # already squashed and pushed. Re-entering "tasks" would re-run
            # finalize's squash/push, which correctly refuses a second plain
            # push. pr_stage's re-entrancy picks this back up at the CI loop.
            state.stage = "pr"
        elif state.tasks:
            state.stage = "tasks"
            for _tid, t in state.tasks.items():
                if t.status in ("failed", "blocked", "running"):
                    t.status = "pending"
                    # A human-mediated resume deserves a fresh ladder: recorded
                    # attempts would otherwise re-trigger _should_halt instantly.
                    # The audit trail survives in events.jsonl and task_dir
                    # transcripts, so nothing is lost by clearing them here.
                    t.attempts = {"test": [], "build": [], "review": []}
                    rundir.append_intent({"task": _tid, "reset": True})
                    # ...and a fresh bad-test escape: a spent escape from a
                    # previous cycle otherwise dooms refactor tasks whose
                    # builder must route existing-test adaptations to the
                    # test-writer (first dogfood run finding).
                    t.escaped = False
        else:
            # Halted before any tasks existed: the halt happened during
            # planning. A fresh ladder means clearing planner_attempts too.
            state.stage = "plan"
            state.planner_attempts = []
            state.product_owner_attempts = []
            state.product_owner_decision = None
            rundir.append_intent({"task": "PLAN", "reset": True})
            rundir.append_intent({"task": "PRODUCT-OWNER", "reset": True})

    if state.stage == "plan":
        plan_stage(rundir, state, cfg, worktree)
        if _after_plan_stage(rundir, state):
            return

    if state.stage in {"approve", "checkpoint"}:
        _print_approve_hint(rundir, state)
        return

    _advance(rundir, state, cfg, worktree)


@app.command()
def approve(run_id: str):
    from regie.operations import approve_waiting_state

    rundir = _open_rundir(_home(), run_id)
    state = rundir.read_state()
    try:
        approve_waiting_state(rundir, state)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2)

    typer.echo(f"approved — run `regie resume {run_id} --repo <path>`")


@app.command()
def answer(run_id: str, response: str):
    """Answer a material clarification requested by the current owner."""
    from regie.operations import record_clarification

    rundir = _open_rundir(_home(), run_id)
    state = rundir.read_state()
    try:
        question = record_clarification(rundir, state, response)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2)
    typer.echo(f"answer recorded for: {question}")
    typer.echo(f"run `regie resume {run_id} --repo {state.target_repo}`")


@app.command()
def status(run_id: str):
    state = _open_rundir(_home(), run_id).read_state()
    typer.echo(f"run {state.id}  stage={state.stage}"
               + (f"  halt={state.halt_reason}" if state.halt_reason else ""))
    for tid in state.ordered_task_ids():
        t = state.tasks[tid]
        counts = "/".join(str(len(t.attempts[s])) for s in ("test", "build", "review"))
        typer.echo(f"  {tid}  {t.status:8s} stage={t.stage:6s} attempts(t/b/r)={counts}")
    for child in state.children:
        typer.echo(f"  child {child.run_id}  {child.status:8s} repo={child.repo}")


@app.command()
def watch(
    run_id: Annotated[str | None, typer.Argument()] = None,
    refresh: Annotated[float, typer.Option(min=0.2, max=60.0)] = 1.0,
):
    """Open the live terminal control room for a run (latest by default)."""
    from regie.control_room import ControlRoom, resolve_run_id

    home = _home()
    try:
        selected = resolve_run_id(home, run_id)
    except FileNotFoundError:
        typer.echo(f"run {run_id} not found" if run_id else "no Régie runs found", err=True)
        raise typer.Exit(2) from None
    ControlRoom(home, selected, refresh_interval=refresh).run()


@app.command(name="init")
def init_(repo: Annotated[Path, typer.Option()] = _DEFAULT_REPO,
          force: Annotated[bool, typer.Option("--force")] = False):
    """Detect project tooling and create a verified starter regie.toml."""
    from regie.onboarding import initialize
    target = repo / "regie.toml"
    if target.exists() and not force:
        typer.echo(f"{target} already exists; use --force to replace it", err=True)
        raise typer.Exit(2)
    detection = initialize(repo)
    typer.echo(f"created {target}")
    typer.echo(f"detected language={detection.language} test={detection.test}")


@app.command()
def providers(profiles: Annotated[Path, typer.Option()] = _DEFAULT_PROFILES):
    """Report adapter readiness for every configured binding."""
    from regie.config import _load_profiles
    from regie.providers import health
    errors: list[str] = []
    loaded = _load_profiles(profiles, errors)
    if errors:
        typer.echo("; ".join(errors), err=True)
        raise typer.Exit(2)
    seen = set()
    for profile in loaded.values():
        for binding in profile.bindings:
            key = (binding.cli, binding.model)
            if key in seen:
                continue
            seen.add(key)
            result = health(binding)
            typer.echo(f"{result.status:11s} {binding.cli}:{binding.model} — {result.detail}")


@app.command("knowledge-approve")
def knowledge_approve(run_id: str):
    """Promote a run's reviewed learning candidates into project knowledge."""
    from regie.knowledge import approve_candidates
    rundir = _open_rundir(_home(), run_id)
    state = rundir.read_state()
    count = approve_candidates(rundir, Path(state.target_repo))
    typer.echo(f"approved {count} new knowledge entr{'y' if count == 1 else 'ies'}")


@app.command()
def handoff(run_id: str):
    """Render a human-readable continuation packet from authoritative state."""
    rundir = _open_rundir(_home(), run_id)
    state = rundir.read_state()
    lines = [f"# Handoff: {state.id}", "", "## Objective",
             (rundir.path / "brief.md").read_text().strip(), "",
             "## Current status", f"- Stage: {state.stage}",
             f"- Halt: {state.halt_reason or 'none'}", f"- PR: {state.pr_url or 'none'}",
             "", "## Tasks"]
    for task_id in state.ordered_task_ids():
        task = state.tasks[task_id]
        lines.append(f"- {task_id}: {task.status} / {task.stage} — {task.spec.title}")
    lines += ["", "## How to verify"]
    try:
        cfg = load_config(Path(state.target_repo), _DEFAULT_PROFILES)
        lines.extend(f"- `{name}`: `{command}`" for name, command in cfg.commands.items())
    except Exception:  # noqa: BLE001 - handoff remains useful with stale config
        lines.append("- Read the target repository's regie.toml")
    lines += ["", "## Next action",
              (f"Resolve: {state.halt_reason}" if state.stage == "halted"
               else f"Resume stage `{state.stage}` with `regie resume {run_id} --repo <path>`")]
    path = rundir.path / "handoff.md"
    path.write_text("\n".join(lines) + "\n")
    typer.echo(path)


@app.command("provider-status")
def provider_status():
    """Show provider accounts currently held open by the quota circuit breaker."""
    entries = ProviderHealthStore(_home()).entries()
    if not entries:
        typer.echo("all providers available")
        return
    now = datetime.now(UTC)
    for key, entry in sorted(entries.items()):
        raw_reset = entry.get("unavailable_until")
        try:
            reset = datetime.fromisoformat(str(raw_reset))
        except ValueError:
            reset = now
        if reset.tzinfo is None:
            reset = reset.replace(tzinfo=UTC)
        state = "unavailable" if reset > now else "probe-ready"
        if entry.get("probe_until"):
            try:
                probe = datetime.fromisoformat(str(entry["probe_until"]))
                if probe.tzinfo is None:
                    probe = probe.replace(tzinfo=UTC)
                if probe > now:
                    state = "probing"
            except ValueError:
                pass
        typer.echo(f"{key:32s} {state:11s} {entry.get('kind', 'unknown'):8s} "
                   f"until {raw_reset or '?'}")


@app.command("provider-reset")
def provider_reset(cli: str,
                   auth: Annotated[str | None, typer.Option()] = None):
    """Manually clear a provider circuit after verifying that access recovered."""
    count = ProviderHealthStore(_home()).clear(cli=cli, auth=auth)
    typer.echo(f"cleared {count} provider circuit(s)")


@app.command()
def preflight(repo: Annotated[Path, typer.Option()],
              profiles: Annotated[Path, typer.Option()] = _DEFAULT_PROFILES):
    """Run the repo's own gate commands (lint/typecheck/test) and report
    pass/fail by exit code. Exit 0 iff every command passed — so it composes
    in CI or a pre-dispatch check."""
    from regie.preflight import all_passed
    from regie.preflight import preflight as run_preflight
    cfg = load_config(repo, profiles)
    results = run_preflight(cfg.commands, repo)
    if not results:
        typer.echo("no preflight commands configured (lint/typecheck/test)")
        raise typer.Exit(0)
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        typer.echo(f"  {mark}  {r.name:10s} (exit {r.exit_code})  {r.command}")
        if not r.passed:
            typer.echo("        " + r.tail.strip().replace("\n", "\n        ")[-500:])
    ok = all_passed(results)
    typer.echo("\npreflight: " + ("all green" if ok else "FAILED"))
    raise typer.Exit(0 if ok else 1)


@app.command()
def stats(tokens: Annotated[bool, typer.Option("--tokens")]=False):
    """Cross-run binding telemetry: outcomes per stage×binding + suggestions."""
    from regie.stats import collect, suggestions
    data = collect(_home())
    typer.echo(f"{data.runs} run(s) analyzed\n")
    typer.echo(f"{'stage':26s} {'binding':22s} {'att':>4s} {'done':>5s} "
               f"{'fail':>5s} {'quota':>5s} {'1st-ok':>7s} {'esc-ok':>7s} {'turns/att':>9s}")
    for (stage, key), b in sorted(data.by_binding.items()):
        first = f"{b.first_done}/{b.first_attempts}" if b.first_attempts else "-"
        avg = f"{b.turns / b.attempts:.0f}" if b.attempts else "-"
        typer.echo(f"{stage:26s} {key:22s} {b.attempts:4d} {b.done:5d} "
                   f"{b.failed:5d} {b.quota:5d} {first:>7s} {b.escalation_done:7d} {avg:>9s}")
    if tokens:
        typer.echo("\ntoken usage (provider-normalized):")
        typer.echo(f"{'stage':26s} {'binding':22s} {'new-in':>10s} {'cached':>10s} "
                   f"{'cache-w':>10s} {'output':>10s} {'reason':>10s} "
                   f"{'tool MB':>9s} {'done/MTok':>10s} {'cost $':>9s}")
        for (stage, key), b in sorted(data.by_binding.items()):
            typer.echo(
                f"{stage:26s} {key:22s} {b.new_input_tokens:10d} "
                f"{b.cached_input_tokens:10d} {b.cache_write_input_tokens:10d} "
                f"{b.output_tokens:10d} {b.reasoning_output_tokens:10d} "
                f"{b.tool_output_bytes / 1_000_000:9.2f} "
                f"{b.done_per_million_tokens:10.2f} {b.cost_usd:9.2f}")
    sugg = suggestions(data)
    typer.echo("\nsuggestions:")
    if sugg:
        for s in sugg:
            typer.echo(f"  - {s}")
    else:
        typer.echo("  (none — not enough evidence yet)")


@app.command()
def spec(run_id: str):
    """Print the run's spec (the planner's output you approve)."""
    rundir = _open_rundir(_home(), run_id)
    path = rundir.path / "spec" / "spec.md"
    if not path.exists():
        typer.echo(f"run {run_id} has no spec yet (stage may be pre-plan)", err=True)
        raise typer.Exit(2)
    typer.echo(path.read_text())


@app.command(name="open")
def open_(run_id: str):
    """Print the run's artifact paths (spec, state, transcripts, notes)."""
    rundir = _open_rundir(_home(), run_id)
    typer.echo(f"run dir:   {rundir.path}")
    for label, rel in (("spec", "spec/spec.md"), ("state", "state.json"),
                       ("events", "events.jsonl"), ("decisions", "decisions.md"),
                       ("pr body", "pr-body.md")):
        p = rundir.path / rel
        if p.exists():
            typer.echo(f"{label + ':':10s} {p}")
    tasks_dir = rundir.path / "tasks"
    if tasks_dir.is_dir():
        for td in sorted(tasks_dir.iterdir()):
            arts = sorted(x.name for x in td.iterdir())
            typer.echo(f"{td.name + ':':10s} {td}  ({', '.join(arts)})")


@app.command()
def doctor(run_id: str):
    """Diagnose a run: halt reason, failing gates, evidence, suggested action."""
    rundir = _open_rundir(_home(), run_id)
    state = rundir.read_state()
    typer.echo(f"run {state.id}  stage={state.stage}")
    if state.stage == "done":
        typer.echo(f"healthy — PR: {state.pr_url or '(none recorded)'}")
        return
    if state.halt_reason:
        typer.echo(f"halt reason: {state.halt_reason}")
    for tid in state.ordered_task_ids():
        t = state.tasks[tid]
        if t.status not in ("failed", "blocked"):
            continue
        typer.echo(f"\n{tid} ({t.spec.title}) — {t.status} at stage {t.stage}")
        attempts = t.attempts.get(t.stage, [])
        if attempts:
            last = attempts[-1]
            typer.echo(f"  last attempt: outcome={last.outcome} "
                       f"binding={last.binding.cli}:{last.binding.model} turns={last.turns}")
            for g in last.gate_results:
                if not g.passed:
                    typer.echo(f"  failing gate: {g.name} — {g.detail[:200]}")
            if last.blocked_question:
                typer.echo(f"  blocked question: {last.blocked_question[:300]}")
        tdir = rundir.path / "tasks" / tid
        if tdir.is_dir():
            for note in sorted(tdir.glob("note-*.md")):
                typer.echo(f"  evidence: {note}")
            outs = sorted(tdir.glob("attempt-*.out"))
            if outs:
                typer.echo(f"  last transcript: {outs[-1]}")
    typer.echo("\nsuggested action:")
    reason = state.halt_reason or ""
    if "quota" in reason:
        typer.echo(f"  wait for the provider window to reset, then: "
                   f"regie resume {run_id} --repo <path>")
    elif "rebase conflict" in reason:
        typer.echo("  the halt reason names the conflicting files and how far "
                   "the base moved. In the worktree: rebase onto the base, "
                   "resolve, `git rebase --continue`, then: "
                   f"regie resume {run_id} --repo <path> (finalize is now "
                   "idempotent — it won't re-rebase an already-resolved tree)")
    elif "blocked" in reason:
        typer.echo("  answer the question (edit the spec or decisions.md), then: "
                   f"regie resume {run_id} --repo <path>")
    else:
        typer.echo("  read the evidence above, fix what it names (spec, tests, or "
                   f"budgets), then: regie resume {run_id} --repo <path> (fresh ladders)")


@app.command()
def clean(run_id: str, repo: Annotated[Path, typer.Option()]):
    rundir = _open_rundir(_home(), run_id)
    state = rundir.read_state()
    removed = []
    if state.worktree_path:
        try:
            remove_run_worktree(repo, Path(state.worktree_path))
            removed.append(f"worktree {state.worktree_path}")
        except GitError:
            pass
    if state.branch:
        try:
            delete_branch(repo, state.branch)
            removed.append(f"branch {state.branch}")
        except GitError:
            pass
    typer.echo("removed: " + ", ".join(removed) if removed else "nothing to remove")


def _finish(state: RunState) -> None:
    if state.stage == "halted":
        notify("regie halted", f"{state.id}: {state.halt_reason}")
        typer.echo(f"HALTED: {state.halt_reason}", err=True)
        raise typer.Exit(1)
    notify("regie run complete", f"{state.id} → {state.stage}")
    typer.echo(f"run {state.id} → {state.stage}")


if __name__ == "__main__":
    app()
