from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import CalendarMeeting, CalendarRace, Interview, Job, JobStatus, RaceDay, Recording, RecordingSession, RecordingStatus, ReviewStatus
from .time_model import DEFAULT_BROADCAST_TIMEZONE, broadcast_zone, in_broadcast_timezone, iso_broadcast, recording_end


FILENAME_TIME = re.compile(r"^trackside_(\d{8})-(\d{4})(?:_(\d+))?\.ts$", re.I)


def parse_recording_filename(filename: str, timezone: str = DEFAULT_BROADCAST_TIMEZONE) -> datetime | None:
    match = FILENAME_TIME.match(filename)
    if not match:
        return None
    try:
        stamp = datetime.strptime("".join(match.group(1, 2)), "%Y%m%d%H%M")
        # Historical chunks use a three-digit ordinal. Recorder V2.4 uses a
        # two-digit seconds suffix so each rolled file retains its true start.
        suffix = match.group(3)
        if suffix and len(suffix) == 2 and 0 <= int(suffix) <= 59:
            stamp = stamp.replace(second=int(suffix))
        return stamp.replace(tzinfo=broadcast_zone(timezone))
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
    recording.recording_started_at = in_broadcast_timezone(started).replace(tzinfo=None)
    if recording.duration_seconds and not recording.recording_stopped_at:
        recording.recording_stopped_at = recording_end(recording.recording_started_at, recording.duration_seconds).replace(tzinfo=None)
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
    meeting_rows = session.execute(select(CalendarMeeting).where(CalendarMeeting.race_day_id == race_day.id)).scalars().all()
    meetings = [row.track for row in meeting_rows]
    races = [race for meeting in meeting_rows for race in session.execute(select(CalendarRace).where(CalendarRace.meeting_id == meeting.id)).scalars()]
    race_times = [in_broadcast_timezone(race.scheduled_time) for race in races if race.scheduled_time]
    calendar_status = "not_refreshed" if not meeting_rows else ("stale" if any(row.provider_status == "error" for row in meeting_rows) else "healthy")
    recorder_session = session.execute(select(RecordingSession).where(RecordingSession.race_day_id == race_day.id).order_by(RecordingSession.id.desc())).scalars().first()
    recorder = None if recorder_session is None else {"state": recorder_session.status, "session_id": recorder_session.id,
        "active_chunk": Path(recorder_session.active_path).name if recorder_session.active_path else None,
        "chunk_sequence": recorder_session.active_chunk_sequence, "started_at": iso_broadcast(recorder_session.started_at),
        "planned_end_at": iso_broadcast(recorder_session.planned_end_at), "error": recorder_session.error_summary}
    return {"id": race_day.id, "date": race_day.race_date.isoformat(), "label": race_day.label or race_day.race_date.isoformat(),
            "meetings": meetings, "calendar_status": calendar_status, "race_count": len(races),
            "first_race": iso_broadcast(min(race_times)) if race_times else None, "last_race": iso_broadcast(max(race_times)) if race_times else None,
            "last_calendar_refresh": iso_broadcast(max((row.refreshed_at for row in meeting_rows), default=None)),
            "files": len(records), "interviews": interviews, "pending": pending, "approved": approved,
            "jobs": dict(job_counts), "recordings": [{"id": r.id, "filename": r.filename, "status": r.status.value,
              "started_at": iso_broadcast(r.recording_started_at), "stopped_at": iso_broadcast(r.recording_stopped_at),
              "duration_seconds": r.duration_seconds} for r in records], "recorder": recorder}


def list_race_days(session: Session) -> list[dict]:
    return [race_day_summary(session, item) for item in session.execute(select(RaceDay).order_by(RaceDay.race_date.desc())).scalars()]
