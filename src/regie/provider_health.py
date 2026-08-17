from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from regie.agents.base import AgentResult
from regie.models import Binding
from regie.quota import fallback_reset_at

_PROBE_LEASE = timedelta(minutes=10)
_FAILED_PROBE_DELAY = timedelta(minutes=5)


def provider_key(binding: Binding) -> str:
    return f"{binding.cli}:{binding.auth}"


def binding_key(binding: Binding) -> str:
    return f"{provider_key(binding)}:{binding.model}"


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


@dataclass(frozen=True)
class ProviderDecision:
    allowed: bool
    probe: bool = False
    key: str = ""
    reason: str = ""
    reset_at: str | None = None
    kind: str | None = None


class ProviderHealthStore:
    """Concurrency-safe quota circuit breaker shared by every Régie run."""

    def __init__(self, home: Path):
        self.home = home
        self.path = home / "provider-health.json"
        self.lock_path = home / "provider-health.lock"

    @contextmanager
    def _locked(self) -> Iterator[dict]:
        self.home.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                try:
                    data = json.loads(self.path.read_text()) if self.path.exists() else {}
                except (json.JSONDecodeError, OSError):
                    data = {}
                if not isinstance(data, dict):
                    data = {}
                data.setdefault("version", 1)
                data.setdefault("providers", {})
                yield data
                tmp = self.path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
                os.replace(tmp, self.path)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def _entry_key(binding: Binding, scope: str) -> str:
        return binding_key(binding) if scope == "model" else provider_key(binding)

    def reserve(self, binding: Binding, *, now: datetime | None = None) -> ProviderDecision:
        # The fake adapter represents arbitrary providers in tests; giving it a
        # global circuit would conflate independent scripted fixtures.
        if binding.cli == "fake":
            return ProviderDecision(allowed=True)
        observed = now or datetime.now(UTC)
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=UTC)
        with self._locked() as data:
            providers = data["providers"]
            candidates = [provider_key(binding), binding_key(binding)]
            key = next((candidate for candidate in candidates if candidate in providers), None)
            if key is None:
                return ProviderDecision(allowed=True)
            entry = providers[key]
            unavailable_until = _parse_time(entry.get("unavailable_until"))
            if unavailable_until is not None and observed < unavailable_until:
                return ProviderDecision(
                    allowed=False, key=key, reason=entry.get("reason", "quota exhausted"),
                    reset_at=entry.get("unavailable_until"), kind=entry.get("kind"),
                )
            probe_until = _parse_time(entry.get("probe_until"))
            if probe_until is not None and observed < probe_until:
                return ProviderDecision(
                    allowed=False, key=key,
                    reason=f"provider recovery probe already running for {key}",
                    reset_at=entry.get("probe_until"), kind=entry.get("kind"),
                )
            entry["probe_until"] = (observed + _PROBE_LEASE).isoformat()
            return ProviderDecision(allowed=True, probe=True, key=key,
                                    reset_at=entry.get("unavailable_until"),
                                    kind=entry.get("kind"))

    def record_quota(self, binding: Binding, result: AgentResult,
                     *, now: datetime | None = None) -> dict:
        observed = now or datetime.now(UTC)
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=UTC)
        kind = result.quota_kind or "unknown"
        scope = result.quota_scope or "provider"
        explicit_reset = result.quota_reset_at is not None
        reset_at = result.quota_reset_at or fallback_reset_at(kind, binding.auth, now=observed)
        key = self._entry_key(binding, scope)
        entry = {
            "cli": binding.cli,
            "auth": binding.auth,
            "model": binding.model if scope == "model" else None,
            "scope": scope,
            "kind": kind,
            "observed_at": observed.astimezone(UTC).isoformat(),
            "unavailable_until": reset_at,
            "reset_source": "provider" if explicit_reset else "fallback",
            "probe_until": None,
            "reason": result.quota_reason or result.text or "quota exhausted",
        }
        with self._locked() as data:
            previous = data["providers"].get(key)
            # Do not let a concurrent call that omitted reset metadata replace
            # a timestamp the provider supplied explicitly.
            if (previous and previous.get("reset_source") == "provider"
                    and not explicit_reset):
                entry["unavailable_until"] = previous["unavailable_until"]
                entry["reset_source"] = "provider"
                if previous.get("kind") == "weekly":
                    entry["kind"] = "weekly"
            data["providers"][key] = entry
        return entry

    def finish_probe(self, binding: Binding, decision: ProviderDecision,
                     result: AgentResult, *, now: datetime | None = None) -> None:
        if not decision.probe:
            return
        observed = now or datetime.now(UTC)
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=UTC)
        with self._locked() as data:
            entry = data["providers"].get(decision.key)
            if entry is None:
                return
            if result.outcome in {"done", "blocked"}:
                data["providers"].pop(decision.key, None)
            elif result.outcome != "quota":
                entry["unavailable_until"] = (observed + _FAILED_PROBE_DELAY).isoformat()
                entry["probe_until"] = None
                entry["reason"] = "provider recovery probe failed: " + result.text[-500:]

    def entries(self) -> dict[str, dict]:
        with self._locked() as data:
            return json.loads(json.dumps(data["providers"]))

    def clear(self, cli: str | None = None, auth: str | None = None) -> int:
        with self._locked() as data:
            providers = data["providers"]
            keys = [key for key, entry in providers.items()
                    if (cli is None or entry.get("cli") == cli)
                    and (auth is None or entry.get("auth") == auth)]
            for key in keys:
                providers.pop(key, None)
            return len(keys)
