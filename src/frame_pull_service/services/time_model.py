"""Explicit broadcast wall-clock handling for recordings and calendar events."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

DEFAULT_BROADCAST_TIMEZONE = "Pacific/Auckland"


def broadcast_zone(name: str = DEFAULT_BROADCAST_TIMEZONE) -> ZoneInfo:
    return ZoneInfo(name)


def in_broadcast_timezone(value: datetime | None, timezone: str = DEFAULT_BROADCAST_TIMEZONE) -> datetime | None:
    if value is None:
        return None
    zone = broadcast_zone(timezone)
    return value.replace(tzinfo=zone) if value.tzinfo is None else value.astimezone(zone)


def iso_broadcast(value: datetime | None, timezone: str = DEFAULT_BROADCAST_TIMEZONE) -> str | None:
    normalized = in_broadcast_timezone(value, timezone)
    return normalized.isoformat() if normalized else None


def recording_end(start: datetime | None, duration_seconds: float | None, timezone: str = DEFAULT_BROADCAST_TIMEZONE) -> datetime | None:
    normalized = in_broadcast_timezone(start, timezone)
    return normalized + timedelta(seconds=duration_seconds) if normalized is not None and duration_seconds is not None else None


def interview_wall_clock(recording_start: datetime | None, source_timestamp: float, timezone: str = DEFAULT_BROADCAST_TIMEZONE) -> datetime | None:
    start = in_broadcast_timezone(recording_start, timezone)
    return start + timedelta(seconds=source_timestamp) if start is not None else None
