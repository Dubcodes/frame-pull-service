from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import CalendarMeeting, Interview, Job, JobStatus, RaceDay, Recording, RecordingStatus, ReviewStatus


FILENAME_TIME = re.compile(r"^trackside_(\d{8})-(\d{4})(?:_\d+)?\.ts$", re.I)


def parse_recording_filename(filename: str) -> datetime | None:
    match = FILENAME_TIME.match(filename)
    if not match:
        return None
    try:
        return datetime.strptime("".join(match.groups()), "%Y%m%d%H%M")
    except ValueError:
        return None


def ensure_race_day(session: Session, started: datetime) -> RaceDay:
    item = session.execute(select(RaceDay).where(RaceDay.race_date == started.date())).scalar_one_or_none()
    if item is None:
        item = RaceDay(race_date=started.date(), label=f"{started.strftime('%A')} {started.day} {started.strftime('%B %Y')}")
        session.add(item); session.flush()
    return item


def attach_recording_to_race_day(session: Session, recording: Recording) -> None:
    started = recording.recording_started_at or parse_recording_filename(recording.filename)
    if not started:
        return
    recording.recording_started_at = started
    if recording.duration_seconds and not recording.recording_stopped_at:
        recording.recording_stopped_at = started + timedelta(seconds=recording.duration_seconds)
    recording.race_day_id = ensure_race_day(session, started).id


def backfill_race_days(session: Session) -> int:
    changed = 0
    for recording in session.execute(select(Recording).where(Recording.race_day_id.is_(None))).scalars():
        before = recording.race_day_id
        attach_recording_to_race_day(session, recording)
        changed += bool(recording.race_day_id != before)
    session.commit()
    return changed


def race_day_summary(session: Session, race_day: RaceDay) -> dict:
    records = session.execute(select(Recording).where(Recording.race_day_id == race_day.id).order_by(Recording.recording_started_at)).scalars().all()
    job_counts = defaultdict(int)
    interviews = pending = approved = 0
    for record in records:
        for job in record.jobs:
            job_counts[job.status.value] += 1
        count = session.scalar(select(func.count()).select_from(Interview).where(Interview.recording_id == record.id)) or 0
        interviews += count
        pending += session.scalar(select(func.count()).select_from(Interview).where(Interview.recording_id == record.id, Interview.review_status == ReviewStatus.PENDING)) or 0
        approved += session.scalar(select(func.count()).select_from(Interview).where(Interview.recording_id == record.id, Interview.review_status == ReviewStatus.APPROVED)) or 0
    meetings = [row.track for row in session.execute(select(CalendarMeeting).where(CalendarMeeting.race_day_id == race_day.id)).scalars()]
    return {"id": race_day.id, "date": race_day.race_date.isoformat(), "label": race_day.label or race_day.race_date.isoformat(),
            "meetings": meetings, "files": len(records), "interviews": interviews, "pending": pending, "approved": approved,
            "jobs": dict(job_counts), "recordings": [{"id": r.id, "filename": r.filename, "status": r.status.value,
              "started_at": r.recording_started_at.isoformat() if r.recording_started_at else None,
              "stopped_at": r.recording_stopped_at.isoformat() if r.recording_stopped_at else None,
              "duration_seconds": r.duration_seconds} for r in records]}


def list_race_days(session: Session) -> list[dict]:
    return [race_day_summary(session, item) for item in session.execute(select(RaceDay).order_by(RaceDay.race_date.desc())).scalars()]
