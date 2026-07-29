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
