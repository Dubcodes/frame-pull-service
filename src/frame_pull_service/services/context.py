"""Conservative calendar context resolution for reviewed interview evidence."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CalendarMeeting, CalendarRace, Interview, Recording


def resolve_interview_context(session: Session, interview: Interview) -> dict:
    recording = session.get(Recording, interview.recording_id)
    if not recording or not recording.race_day_id or not recording.recording_started_at:
        return {"confidence": "unknown", "reason": "Recording timing or race day is unavailable."}
    observed = recording.recording_started_at + timedelta(seconds=interview.source_start)
    rows = session.execute(select(CalendarMeeting, CalendarRace).join(CalendarRace).where(CalendarMeeting.race_day_id == recording.race_day_id, CalendarRace.scheduled_time.is_not(None))).all()
    if not rows: return {"confidence": "unknown", "reason": "No scheduled calendar races are cached."}
    distances = sorted(((abs((race.scheduled_time - observed).total_seconds()), meeting, race) for meeting, race in rows), key=lambda item: item[0])
    closest, meeting, race = distances[0]
    if closest > 45 * 60: return {"confidence": "low", "reason": "No race is close to the interview timestamp."}
    tied = [item for item in distances if abs(item[0] - closest) <= 10 * 60]
    confidence = "high" if len(tied) == 1 and closest <= 20 * 60 else "conflict" if len(tied) > 1 else "medium"
    return {"confidence": confidence, "track": meeting.track, "race_number": race.race_number, "race_name": race.name,
            "scheduled_time": race.scheduled_time.isoformat() if race.scheduled_time else None,
            "evidence": "recording_start_plus_interview_offset", "runner_context": race.context.get("runners", [])}
