from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Recording, RecordingStatus
from .operations import get_value
from .race_days import attach_recording_to_race_day


def recording_fingerprint(path: Path, stat) -> str:
    material = f"{path.name}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:32]


def discover_recordings(session: Session, settings: Settings, now: datetime | None = None) -> dict:
    # Keep naive DB timestamps in the same local-time convention used by
    # datetime.fromtimestamp/file mtimes on the Windows deployment.
    now = now or datetime.now()
    source_dir = settings.source_dir.resolve()
    baseline_exists = session.execute(select(Recording.id).limit(1)).first() is not None
    created = updated = queued = 0
    for path in sorted(source_dir.glob("*.ts")):
        try:
            stat = path.stat()
            readable = path.open("rb")
            readable.close()
        except OSError:
            continue
        fingerprint = recording_fingerprint(path, stat)
        record = session.execute(select(Recording).where(Recording.filename == path.name)).scalar_one_or_none()
        stability_seconds = int(get_value(session, "file_stability_seconds", settings.stable_for_seconds))
        stable = stat.st_size >= settings.min_input_bytes and (now.timestamp() - stat.st_mtime) >= stability_seconds
        if record is None:
            historical = not baseline_exists and not settings.queue_existing_on_first_run
            status = RecordingStatus.HISTORICAL if historical else (RecordingStatus.READY if stable else RecordingStatus.WAITING)
            record = Recording(filename=path.name, source_path=str(path.resolve()), fingerprint=fingerprint,
                               size_bytes=stat.st_size, mtime_epoch=stat.st_mtime, historical=historical,
                               stable_at=now if stable else None, is_closed=stable, status=status)
            session.add(record)
            session.flush(); attach_recording_to_race_day(session, record)
            created += 1
            if status == RecordingStatus.READY and bool(get_value(session, "autoqueue_closed_recordings", settings.auto_queue)):
                from .queue import queue_recording
                queue_recording(session, record)
                queued += 1
            continue
        if record.size_bytes != stat.st_size or record.mtime_epoch != stat.st_mtime:
            record.size_bytes, record.mtime_epoch, record.fingerprint = stat.st_size, stat.st_mtime, fingerprint
            if record.status not in (RecordingStatus.PROCESSING, RecordingStatus.COMPLETE):
                record.status = RecordingStatus.WAITING
                record.stable_at = None
            updated += 1
        elif stable and record.status in (RecordingStatus.DISCOVERED, RecordingStatus.WAITING):
            record.stable_at = now
            record.is_closed = True
            record.status = RecordingStatus.READY
            updated += 1
            if bool(get_value(session, "autoqueue_closed_recordings", settings.auto_queue)) and not record.historical:
                from .queue import queue_recording
                queue_recording(session, record)
                queued += 1
    session.commit()
    return {"created": created, "updated": updated, "queued": queued, "baseline_established": not baseline_exists}


def initialize_baseline(session: Session, settings: Settings) -> dict:
    original = settings.queue_existing_on_first_run
    try:
        settings.queue_existing_on_first_run = False
        return discover_recordings(session, settings)
    finally:
        settings.queue_existing_on_first_run = original
