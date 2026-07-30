"""Cross-run binding telemetry: the evidence base for reordering each
profile's bindings. Reads only recorded RunState attempts (never config), so
it stays valid across profile-schema changes. Deterministic aggregation —
suggestions are flagged observations for the human, not automatic rerouting.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

_STAGES = ("test", "build", "review")


@dataclass
class BindingStats:
    attempts: int = 0
    done: int = 0
    failed: int = 0
    blocked: int = 0
    quota: int = 0
    turns: int = 0
    first_attempts: int = 0
    first_done: int = 0
    escalation_done: int = 0  # succeeded as a non-first binding in a ladder

    def record(self, outcome: str | None, turns: int, position: int) -> None:
        self.attempts += 1
        self.turns += turns or 0
        if outcome in ("done", "failed", "blocked", "quota"):
            setattr(self, outcome, getattr(self, outcome) + 1)
        if position == 0:
            self.first_attempts += 1
            if outcome == "done":
                self.first_done += 1
        elif outcome == "done":
            self.escalation_done += 1


@dataclass
class RunsStats:
    # (stage, "cli:model") -> BindingStats
    by_binding: dict[tuple[str, str], BindingStats] = field(default_factory=dict)
    runs: int = 0

    def bucket(self, stage: str, key: str) -> BindingStats:
        return self.by_binding.setdefault((stage, key), BindingStats())


def collect(home: Path) -> RunsStats:
    stats = RunsStats()
    runs_dir = home / "runs"
    if not runs_dir.is_dir():
        return stats
    for run_dir in sorted(runs_dir.iterdir()):
        state_file = run_dir / "state.json"
        if not state_file.is_file():
            continue
        try:
            state = json.loads(state_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        stats.runs += 1
        for task in (state.get("tasks") or {}).values():
            attempts = task.get("attempts") or {}
            for stage in _STAGES:
                for pos, a in enumerate(attempts.get(stage) or []):
                    b = a.get("binding") or {}
                    key = f"{b.get('cli', '?')}:{b.get('model', '?')}"
                    stats.bucket(stage, key).record(
                        a.get("outcome"), a.get("turns") or 0, pos)
        for pos, a in enumerate(state.get("planner_attempts") or []):
            b = a.get("binding") or {}
            key = f"{b.get('cli', '?')}:{b.get('model', '?')}"
            stats.bucket("plan", key).record(a.get("outcome"), a.get("turns") or 0, pos)
    return stats


def suggestions(stats: RunsStats, min_attempts: int = 5) -> list[str]:
    """Flagged observations, never prescriptions. Rules are deliberately
    coarse: fine-grained routing judgment is exactly what telemetry must
    EARN, per the design's evidence-over-prediction principle."""
    out: list[str] = []
    by_stage: dict[str, list[tuple[str, BindingStats]]] = defaultdict(list)
    for (stage, key), b in stats.by_binding.items():
        by_stage[stage].append((key, b))
    for stage, entries in sorted(by_stage.items()):
        for key, b in sorted(entries):
            if b.first_attempts >= min_attempts:
                rate = b.first_done / b.first_attempts
                if rate < 0.5:
                    out.append(
                        f"{stage}: {key} first-attempt success is "
                        f"{rate:.0%} over {b.first_attempts} attempts — "
                        f"consider a stronger primary for this stage")
            if b.escalation_done >= 2:
                out.append(
                    f"{stage}: {key} succeeded {b.escalation_done}x as an "
                    f"escalation target — consider promoting it in the list")
            if b.quota >= 2:
                out.append(
                    f"{stage}: {key} hit quota {b.quota}x — consider an "
                    f"alternative-provider fallback after it")
    return out
