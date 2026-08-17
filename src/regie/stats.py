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
    new_input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    tool_output_bytes: int = 0
    cost_usd: float = 0.0

    def record(self, outcome: str | None, turns: int, position: int,
               metrics: dict | None = None) -> None:
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
        metrics = metrics or {}
        for name in ("new_input_tokens", "cached_input_tokens",
                     "cache_write_input_tokens", "output_tokens",
                     "reasoning_output_tokens", "tool_output_bytes"):
            setattr(self, name, getattr(self, name) + int(metrics.get(name) or 0))
        self.cost_usd += float(metrics.get("cost_usd") or 0)

    @property
    def total_tokens(self) -> int:
        return (self.new_input_tokens + self.cached_input_tokens
                + self.cache_write_input_tokens + self.output_tokens)

    @property
    def done_per_million_tokens(self) -> float:
        return self.done * 1_000_000 / self.total_tokens if self.total_tokens else 0.0


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
                        a.get("outcome"), a.get("turns") or 0, pos,
                        _metrics(a, b.get("cli", "")))
            for specialist, specialist_attempts in (task.get("specialist_attempts") or {}).items():
                for pos, a in enumerate(specialist_attempts):
                    b = a.get("binding") or {}
                    key = f"{b.get('cli', '?')}:{b.get('model', '?')}"
                    stats.bucket(f"review:{specialist}", key).record(
                        a.get("outcome"), a.get("turns") or 0, pos,
                        _metrics(a, b.get("cli", "")))
        for pos, a in enumerate(state.get("planner_attempts") or []):
            b = a.get("binding") or {}
            key = f"{b.get('cli', '?')}:{b.get('model', '?')}"
            stats.bucket("plan", key).record(
                a.get("outcome"), a.get("turns") or 0, pos,
                _metrics(a, b.get("cli", "")))
    return stats


def _metrics(attempt: dict, cli: str) -> dict:
    """Read new normalized telemetry and backfill historic run states."""
    normalized = attempt.get("metrics") or {}
    if any(normalized.get(name) for name in (
            "new_input_tokens", "cached_input_tokens", "cache_write_input_tokens",
            "output_tokens", "reasoning_output_tokens", "tool_output_bytes", "cost_usd")):
        return normalized
    usage = attempt.get("usage") or {}
    cached = int(usage.get("cached_input_tokens")
                 or usage.get("cache_read_input_tokens") or 0)
    raw_input = int(usage.get("input_tokens") or 0)
    new_input = max(0, raw_input - cached) if cli == "codex" else raw_input
    return {
        "new_input_tokens": new_input,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": int(
            usage.get("cache_write_input_tokens")
            or usage.get("cache_creation_input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "reasoning_output_tokens": int(usage.get("reasoning_output_tokens") or 0),
        "cost_usd": float(usage.get("total_cost_usd") or 0),
    }


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
