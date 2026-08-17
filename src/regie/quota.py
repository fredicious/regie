from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Literal

QuotaKind = Literal["session", "weekly", "rate", "unknown"]
QuotaScope = Literal["provider", "model"]


@dataclass(frozen=True)
class QuotaMetadata:
    kind: QuotaKind
    scope: QuotaScope
    reset_at: str | None
    reason: str


_RESET_KEYS = {
    "reset", "resetat", "resetsat", "resettime", "resettimestamp",
    "retryat", "retryafter", "retryafterseconds", "availableat",
}


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _walk_reset_values(value: Any) -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = _normalized_key(str(key))
            if normalized in _RESET_KEYS:
                found.append((normalized, child))
            found.extend(_walk_reset_values(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_reset_values(child))
    return found


def _as_datetime(value: Any, now: datetime, *, relative: bool = False) -> datetime | None:
    if isinstance(value, (int, float)):
        if relative or value < 10_000_000:
            return now + timedelta(seconds=max(0, float(value)))
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.isdigit():
        return _as_datetime(int(raw), now, relative=relative)
    try:
        parsed = datetime.fromisoformat(raw)
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(raw)
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    except (TypeError, ValueError):
        return None


def _reset_from_text(text: str, now: datetime) -> datetime | None:
    iso = re.search(
        r"\b(20\d{2}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)\b",
        text,
    )
    if iso:
        parsed = _as_datetime(iso.group(1), now)
        if parsed is not None:
            return parsed

    relative = re.search(
        r"resets?\s+in\s+(?:(\d+)\s*(?:d|day)s?)?\s*"
        r"(?:(\d+)\s*(?:h|hour)s?)?\s*(?:(\d+)\s*(?:m|min|minute)s?)?",
        text,
        re.IGNORECASE,
    )
    if relative and any(relative.groups()):
        days, hours, minutes = (int(part or 0) for part in relative.groups())
        return now + timedelta(days=days, hours=hours, minutes=minutes)

    clock = re.search(
        r"resets?(?:\s+(?:at|on))?\s+(?:[^\n,]*?\s+)?"
        r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b",
        text,
        re.IGNORECASE,
    )
    if clock:
        hour = int(clock.group(1)) % 12
        if clock.group(3).lower() == "pm":
            hour += 12
        minute = int(clock.group(2) or 0)
        local_now = now.astimezone()
        candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local_now:
            candidate += timedelta(days=1)
        return candidate
    return None


def quota_metadata(text: str, payload: Any = None, *, now: datetime | None = None
                   ) -> QuotaMetadata:
    observed = now or datetime.now(UTC)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    low = text.lower()
    if "weekly" in low or "week limit" in low:
        kind: QuotaKind = "weekly"
    elif re.search(r"(?:5|five)[ -]?hour|session(?:-based)?\s+(?:usage\s+)?limit", low):
        kind = "session"
    elif "rate limit" in low or "rate_limit" in low:
        kind = "rate"
    elif "usage limit" in low or "usage_limit" in low or "quota" in low:
        kind = "session"
    else:
        kind = "unknown"

    scope: QuotaScope = (
        "model" if re.search(r"model[- ]specific|limit\s+for\s+(?:the\s+)?model", low)
        else "provider"
    )
    reset: datetime | None = None
    for key, value in _walk_reset_values(payload):
        reset = _as_datetime(value, observed, relative="retryafter" in key)
        if reset is not None:
            break
    if reset is None:
        reset = _reset_from_text(text, observed)
    reset_at = reset.astimezone(UTC).isoformat() if reset is not None else None
    return QuotaMetadata(kind=kind, scope=scope, reset_at=reset_at,
                         reason=text[-2000:])


def fallback_reset_at(kind: QuotaKind, auth: str, *, now: datetime | None = None) -> str:
    observed = now or datetime.now(UTC)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    auth_key = auth.lower().replace("_", "-")
    paid_api = auth_key in {"api", "api-key", "apikey", "payg", "usage-based"}
    if kind == "weekly":
        delay = timedelta(hours=24)  # probe daily when a weekly timestamp is absent
    elif kind == "session" or (kind == "rate" and not paid_api):
        delay = timedelta(hours=5)
    elif kind == "rate":
        delay = timedelta(minutes=5)
    else:
        delay = timedelta(minutes=15)
    return (observed + delay).astimezone(UTC).isoformat()
