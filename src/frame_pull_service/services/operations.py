from __future__ import annotations

from sqlalchemy.orm import Session

from ..config import Settings
from ..models import OperationalSetting


DEFAULTS = {
    "automatic_race_day_mode": False,
    "processing_paused": False,
    "max_concurrent_recordings": 1,
    "autoqueue_closed_recordings": False,
    "file_stability_seconds": 120,
    "calendar_auto_refresh": False,
    "calendar_refresh_minutes": 60,
    "source_deletion_enabled": False,
    "source_retention_hours": 168,
    "require_review_complete_before_delete": True,
    "require_export_before_delete": True,
    "default_review_view": "people",
}


def seed_settings(session: Session, settings: Settings) -> None:
    defaults = {**DEFAULTS, "autoqueue_closed_recordings": settings.auto_queue,
                "file_stability_seconds": settings.stable_for_seconds,
                "max_concurrent_recordings": settings.max_concurrent_jobs}
    for key, value in defaults.items():
        if session.get(OperationalSetting, key) is None:
            session.add(OperationalSetting(key=key, value=value))
    session.commit()


def get_value(session: Session, key: str, default=None):
    item = session.get(OperationalSetting, key)
    if item is not None:
        return item.value
    return DEFAULTS.get(key) if default is None else default


def settings_view(session: Session) -> dict:
    return {key: get_value(session, key, value) for key, value in DEFAULTS.items()}


def update_settings(session: Session, values: dict) -> dict:
    allowed = set(DEFAULTS)
    for key, value in values.items():
        if key not in allowed:
            continue
        if key == "max_concurrent_recordings":
            value = min(4, max(1, int(value)))
        if key == "default_review_view" and value not in {"people", "interviews"}:
            raise ValueError("default review view must be people or interviews")
        item = session.get(OperationalSetting, key)
        if item is None:
            item = OperationalSetting(key=key, value=value); session.add(item)
        else:
            item.value = value
    session.commit()
    return settings_view(session)
