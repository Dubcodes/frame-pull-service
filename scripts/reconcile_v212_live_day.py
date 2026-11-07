"""One-time bounded reconciliation for the 7-Nov-2026 V2.12 live recorder session."""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from frame_pull_service.config import get_settings
from frame_pull_service.db import create_db_engine, make_session_factory
from frame_pull_service.models import Job, JobStatus, Recording, RecordingSession, RecordingStatus
from frame_pull_service.services.context import refresh_race_day_contexts
from frame_pull_service.services.time_model import broadcast_zone


def media_duration(path: Path) -> float:
    output = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(path),
    ], text=True)
    return float(output.strip())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--session-id", type=int, default=3)
    parser.add_argument("--date", default="20261107")
    args = parser.parse_args()
    settings = get_settings()
    engine = create_db_engine(settings)
    factory = make_session_factory(engine)
    zone = broadcast_zone(settings.broadcast_timezone)
    with factory() as db:
        recorder = db.get(RecordingSession, args.session_id)
        if not recorder or recorder.status != "closed":
            raise SystemExit("target recorder session is not closed")
        active = db.scalar(select(Job.id).where(Job.status == JobStatus.PROCESSING).limit(1))
        if active:
            raise SystemExit("an active job prevents reconciliation")
        records = db.execute(select(Recording).where(Recording.filename.like(f"trackside_{args.date}-%")).order_by(Recording.id)).scalars().all()
        if len(records) != 6:
            raise SystemExit(f"expected exactly 6 bounded recordings, found {len(records)}")
        changes = []
        for record in records:
            path = settings.source_dir / record.filename
            duration = record.duration_seconds or media_duration(path)
            stopped = datetime.fromtimestamp(record.mtime_epoch, UTC).astimezone(zone)
            started = stopped - timedelta(seconds=duration)
            changes.append({
                "id": record.id, "filename": record.filename,
                "old_started_at": str(record.recording_started_at), "new_started_at": started.isoformat(),
                "old_stopped_at": str(record.recording_stopped_at), "new_stopped_at": stopped.isoformat(),
                "duration": duration,
                "old_status": record.status.value,
                "new_status": "ignored" if record.size_bytes < settings.min_input_bytes else record.status.value,
            })
            if args.apply:
                record.recorder_session_id = recorder.id
                record.duration_seconds = duration
                record.metadata_probed_at = datetime.utcnow()
                record.recording_started_at = started.replace(tzinfo=None)
                record.recording_stopped_at = stopped.replace(tzinfo=None)
                if record.size_bytes < settings.min_input_bytes and not record.jobs:
                    record.status, record.is_closed, record.stable_at = RecordingStatus.IGNORED, True, datetime.utcnow()
        if args.apply:
            recorder.started_at = recorder.started_at.replace(tzinfo=UTC).astimezone(zone).replace(tzinfo=None)
            recorder.stopped_at = recorder.stopped_at.replace(tzinfo=UTC).astimezone(zone).replace(tzinfo=None)
            db.commit()
            refreshed = refresh_race_day_contexts(db, records[0].race_day_id, settings.broadcast_timezone)
        else:
            refreshed = 0
        print(json.dumps({"apply": args.apply, "records": changes, "contexts_refreshed": refreshed}, indent=2))


if __name__ == "__main__":
    main()
