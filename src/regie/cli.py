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
from regie.models import RunState, TaskSpec, TaskState
from regie.notify import notify
from regie.pipeline import (
    finalize_stage,
    plan_stage,
    pr_stage,
    reconcile,
    run_tasks_stage,
)
from regie.rundir import RunDir, RunLocked

app = typer.Typer(add_completion=False)

_DEFAULT_PROFILES = Path(__file__).parent.parent.parent / "profiles"


def _home() -> Path:
    return Path(os.environ.get("REGIE_HOME", Path.home() / ".regie"))


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
    typer.echo(f"spec ready: {rundir.path / 'spec' / 'spec.md'}")
    typer.echo(f"run `regie approve {state.id}` to continue")


def _advance(rundir: RunDir, state: RunState, cfg: RegieConfig, worktree: Path) -> None:
    """From stage "tasks" onward: run tasks, then finalize, then the PR stage
    (squash, scribe, push, CI watch with gated debugger rounds)."""
    if state.stage == "tasks":
        run_tasks_stage(rundir, state, cfg, worktree)
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
        tasks_file: Annotated[Path | None, typer.Option("--tasks-file")] = None):
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

    (rundir.path / "brief.md").write_text(brief.read_text())

    base = _resolve_base_sha(repo, cfg.base_branch)
    wt = create_run_worktree(repo, f"regie/{run_id}", base, home / "worktrees" / run_id)

    state = RunState(id=run_id, target_repo=str(repo), branch=f"regie/{run_id}",
                     base_sha=base, base_branch=cfg.base_branch, worktree_path=str(wt),
                     autonomous=autonomous, stage="plan")

    if tasks_file is not None:
        specs = [TaskSpec(**t) for t in json.loads(tasks_file.read_text())]
        state.tasks = {s.id: TaskState(spec=s) for s in specs}
        state.stage = "tasks"
        rundir.write_state(state)
    else:
        rundir.write_state(state)
        plan_stage(rundir, state, cfg, Path(state.worktree_path))
        if _after_plan_stage(rundir, state):
            return

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
            for t in state.tasks.values():
                if t.status in ("failed", "blocked", "running"):
                    t.status = "pending"
                    # A human-mediated resume deserves a fresh ladder: recorded
                    # attempts would otherwise re-trigger _should_halt instantly.
                    # The audit trail survives in events.jsonl and task_dir
                    # transcripts, so nothing is lost by clearing them here.
                    t.attempts = {"test": [], "build": [], "review": []}
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

    if state.stage == "plan":
        plan_stage(rundir, state, cfg, worktree)
        if _after_plan_stage(rundir, state):
            return

    if state.stage == "approve":
        _print_approve_hint(rundir, state)
        return

    _advance(rundir, state, cfg, worktree)


@app.command()
def approve(run_id: str):
    rundir = _open_rundir(_home(), run_id)
    state = rundir.read_state()
    if state.stage != "approve":
        typer.echo(f"run {run_id} is not awaiting approval (stage={state.stage})", err=True)
        raise typer.Exit(2)
    state.stage = "tasks"
    rundir.write_state(state)
    typer.echo(f"approved — run `regie resume {run_id} --repo <path>`")


@app.command()
def status(run_id: str):
    state = _open_rundir(_home(), run_id).read_state()
    typer.echo(f"run {state.id}  stage={state.stage}"
               + (f"  halt={state.halt_reason}" if state.halt_reason else ""))
    for tid in state.ordered_task_ids():
        t = state.tasks[tid]
        counts = "/".join(str(len(t.attempts[s])) for s in ("test", "build", "review"))
        typer.echo(f"  {tid}  {t.status:8s} stage={t.stage:6s} attempts(t/b/r)={counts}")


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
