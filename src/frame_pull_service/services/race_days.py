from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import CalendarMeeting, CalendarRace, Interview, InterviewRevision, Job, JobStatus, PlaybackProxy, RaceDay, Recording, RecordingSession, RecordingStatus, ReviewStatus
from .media_audit import latest_media_audit, media_audit_view
from .paths import resolve_recording_source
from .playback import playback_proxy_view
from .detector_candidates import detector_candidates_for_race_day, source_review_view
from .source_review_queue import source_review_progress
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
            if job.status == JobStatus.QUEUED:
                job_counts["released" if job.released_for_processing else "held"] += 1
            else:
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
    def latest_job(recording: Recording) -> dict | None:
        job = max(recording.jobs, key=lambda item: item.id, default=None)
        if not job:
            return None
        return {"id": job.id, "status": job.status.value,
                "released_for_processing": job.released_for_processing,
                "queued_at": iso_broadcast(job.queued_at), "started_at": iso_broadcast(job.started_at),
                "finished_at": iso_broadcast(job.finished_at)}

    return {"id": race_day.id, "date": race_day.race_date.isoformat(), "label": race_day.label or race_day.race_date.isoformat(),
            "meetings": meetings, "meeting_details": [{"track": row.track, "racing_code": row.racing_code,
                "racing_code_source": row.racing_code_source} for row in meeting_rows],
            "calendar_status": calendar_status, "race_count": len(races),
            "first_race": iso_broadcast(min(race_times)) if race_times else None, "last_race": iso_broadcast(max(race_times)) if race_times else None,
            "last_calendar_refresh": iso_broadcast(max((row.refreshed_at for row in meeting_rows), default=None)),
            "files": len(records), "interviews": interviews, "pending": pending, "approved": approved,
            "jobs": dict(job_counts), "source_review": source_review_view(race_day),
            "recordings": [{"id": r.id, "filename": r.filename, "status": r.status.value,
              "started_at": iso_broadcast(r.recording_started_at), "stopped_at": iso_broadcast(r.recording_stopped_at),
              "duration_seconds": r.duration_seconds,
              "latest_job": latest_job(r)} for r in records], "recorder": recorder}


def list_race_days(session: Session) -> list[dict]:
    return [race_day_summary(session, item) for item in session.execute(select(RaceDay).order_by(RaceDay.race_date.desc())).scalars()]


def race_day_archive(session: Session, settings, race_day: RaceDay) -> dict:
    summary = race_day_summary(session, race_day)
    records = session.execute(
        select(Recording).where(Recording.race_day_id == race_day.id).order_by(Recording.recording_started_at, Recording.id)
    ).scalars().all()
    health_counts = defaultdict(int)
    record_rows = []
    interview_rows = []
    for record in records:
        audit = latest_media_audit(session, record.id)
        audit_view = media_audit_view(audit)
        health_counts[audit_view["classification"]] += 1
        try:
            resolve_recording_source(settings, record, verify_size=True)
            source_available = True
        except (ValueError, FileNotFoundError, OSError):
            source_available = False
        rows = session.execute(select(Interview).where(Interview.recording_id == record.id).order_by(Interview.source_start)).scalars().all()
        record_rows.append({
            "id": record.id, "filename": record.filename, "started_at": iso_broadcast(record.recording_started_at),
            "stopped_at": iso_broadcast(record.recording_stopped_at), "duration_seconds": record.duration_seconds,
            "size_bytes": record.size_bytes, "status": record.status.value, "source_available": source_available,
            "source_protected": record.source_protected, "interview_count": len(rows), "media_audit": audit_view,
            "playback_proxy": playback_proxy_view(session, settings, record.id),
        })
        for interview in rows:
            revision = session.get(InterviewRevision, interview.active_revision_id) if interview.active_revision_id else None
            interview_rows.append({
                "id": interview.id, "recording_id": record.id, "source_start": interview.source_start,
                "source_end": interview.source_end, "local_time": iso_broadcast(
                    record.recording_started_at + timedelta(seconds=interview.source_start) if record.recording_started_at else None
                ),
                "name": interview.final_name or (revision.trusted_name if revision else None) or (revision.ocr_name_candidate if revision else None),
                "role": interview.final_role or (revision.role_clean if revision else None),
                "track": interview.final_track or (revision.track_value if revision else None),
                "racing_code": interview.racing_code,
                "review_status": interview.review_status.value, "export_state": interview.export_state,
                "has_portrait": bool(interview.selected_candidate_id), "manual": interview.bounds_basis == "manual_operator",
                "url": f"/review/{interview.id}",
            })
    rejected = sum(row["review_status"] == "rejected" for row in interview_rows)
    exported = sum(row["export_state"] == "exported" for row in interview_rows)
    pending_exports = sum(row["review_status"] == "approved" and row["export_state"] != "exported" for row in interview_rows)
    manual = sum(row["manual"] for row in interview_rows)
    detector_candidates = detector_candidates_for_race_day(session, settings, race_day.id)
    summary.update({
        "total_source_bytes": sum(row["size_bytes"] for row in record_rows if row["source_available"]),
        "total_source_duration_seconds": sum((row["duration_seconds"] or 0) for row in record_rows if row["source_available"]),
        "source_available_count": sum(row["source_available"] for row in record_rows),
        "health": dict(health_counts), "rejected": rejected, "exported": exported, "pending_exports": pending_exports,
        "automated_interviews": len(interview_rows) - manual, "manually_recovered_interviews": manual,
        "detector_candidate_count": len(detector_candidates),
        "unresolved_detector_candidates": sum(not row["recovered_interview_id"] for row in detector_candidates),
        "source_review_progress": source_review_progress(session, settings, race_day.id),
        "recordings": record_rows, "interview_rows": interview_rows,
    })
    return summary
