"""Dedicated conservative interview-to-race context resolver."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CalendarMeeting, CalendarRace, Interview, InterviewContext, InterviewRevision, Recording
from .operations import get_value
from .time_model import DEFAULT_BROADCAST_TIMEZONE, in_broadcast_timezone, interview_wall_clock, iso_broadcast

RESOLVER_VERSION = "v2.3"


@dataclass(frozen=True)
class ResolvedInterviewContext:
    interview_at: datetime | None
    meeting: CalendarMeeting | None
    race: CalendarRace | None
    confidence: str
    reason: str
    nearby: list[tuple[CalendarMeeting, CalendarRace, float]]


class InterviewContextResolver:
    def __init__(self, timezone: str = DEFAULT_BROADCAST_TIMEZONE): self.timezone = timezone

    def resolve(self, session: Session, interview: Interview) -> ResolvedInterviewContext:
        recording = session.get(Recording, interview.recording_id)
        interview_at = interview_wall_clock(recording.recording_started_at if recording else None, interview.source_start, self.timezone)
        if not recording or not recording.race_day_id or not interview_at:
            return ResolvedInterviewContext(interview_at, None, None, "unknown", "Recording wall-clock timing is unavailable.", [])
        rows = session.execute(select(CalendarMeeting, CalendarRace).join(CalendarRace).where(
            CalendarMeeting.race_day_id == recording.race_day_id, CalendarRace.scheduled_time.is_not(None))).all()
        if not rows:
            return ResolvedInterviewContext(interview_at, None, None, "unknown", "No scheduled calendar races are cached.", [])
        pre = int(get_value(session, "context_pre_race_minutes", 20)) * 60
        post = int(get_value(session, "context_post_race_minutes", 30)) * 60
        nearby_limit = max(pre, post, 45 * 60)
        nearby = sorted([(meeting, race, (interview_at - in_broadcast_timezone(race.scheduled_time, self.timezone)).total_seconds()) for meeting, race in rows], key=lambda item: abs(item[2]))
        visible = [item for item in nearby if abs(item[2]) <= nearby_limit]
        plausible = [item for item in nearby if -pre <= item[2] <= post]
        if not plausible:
            return ResolvedInterviewContext(interview_at, None, None, "unknown", "No scheduled race is within the configured timing window.", visible[:4])
        revision = session.get(InterviewRevision, interview.active_revision_id) if interview.active_revision_id else None
        track_evidence = interview.final_track or (revision.track_value if revision else None)
        if track_evidence:
            matching = [item for item in plausible if item[0].track.casefold() == track_evidence.casefold()]
            if matching:
                plausible = matching
            elif plausible:
                return ResolvedInterviewContext(interview_at, None, None, "conflict", "Existing track evidence conflicts with nearby calendar meetings.", visible[:4])
        candidate_tracks = {item[0].track.casefold() for item in plausible}
        if len(candidate_tracks) > 1:
            return ResolvedInterviewContext(interview_at, None, None, "conflict", "Multiple meetings are similarly plausible in the configured timing window.", visible[:4])
        closest = min(plausible, key=lambda item: abs(item[2]))
        tied = [item for item in plausible if abs(abs(item[2]) - abs(closest[2])) <= 10 * 60]
        if len(tied) > 1:
            return ResolvedInterviewContext(interview_at, None, None, "conflict", "Adjacent races are equally plausible for this interview time.", visible[:4])
        confidence = "high" if track_evidence or abs(closest[2]) <= 10 * 60 else "medium"
        return ResolvedInterviewContext(interview_at, closest[0], closest[1], confidence, "Matched by broadcast wall-clock time and configured race window.", visible[:4])


def context_payload(result: ResolvedInterviewContext, timezone: str = DEFAULT_BROADCAST_TIMEZONE) -> dict:
    race = result.race
    return {"resolver_version": RESOLVER_VERSION, "timezone": timezone, "interview_time": iso_broadcast(result.interview_at, timezone),
            "resolved_track": result.meeting.track if result.meeting else None, "meeting_id": result.meeting.id if result.meeting else None,
            "race_id": race.id if race else None, "race_number": race.race_number if race else None, "race_name": race.name if race else None,
            "scheduled_time": iso_broadcast(race.scheduled_time, timezone) if race else None, "confidence": result.confidence,
            "reason": result.reason, "runners": race.context.get("runners", []) if race else [],
            "nearby_races": [{"meeting_id": meeting.id, "track": meeting.track, "race_id": item.id, "race_number": item.race_number,
                              "race_name": item.name, "scheduled_time": iso_broadcast(item.scheduled_time, timezone), "offset_seconds": round(offset, 1)}
                             for meeting, item, offset in result.nearby]}


def refresh_interview_context(session: Session, interview: Interview, timezone: str = DEFAULT_BROADCAST_TIMEZONE) -> dict:
    payload = context_payload(InterviewContextResolver(timezone).resolve(session, interview), timezone)
    row = session.execute(select(InterviewContext).where(InterviewContext.interview_id == interview.id)).scalar_one_or_none()
    if row is None:
        row = InterviewContext(interview_id=interview.id); session.add(row)
    row.resolver_version, row.timezone, row.interview_at = RESOLVER_VERSION, timezone, payload["interview_time"]
    row.meeting_id, row.race_id, row.resolved_track, row.confidence, row.evidence = payload["meeting_id"], payload["race_id"], payload["resolved_track"], payload["confidence"], payload
    return payload


def resolve_interview_context(session: Session, interview: Interview, timezone: str = DEFAULT_BROADCAST_TIMEZONE) -> dict:
    """Read-through resolver for API callers; refresh route persists this evidence."""
    stored = session.execute(select(InterviewContext).where(InterviewContext.interview_id == interview.id)).scalar_one_or_none()
    if stored and stored.resolver_version == RESOLVER_VERSION:
        return stored.evidence
    return context_payload(InterviewContextResolver(timezone).resolve(session, interview), timezone)


def refresh_race_day_contexts(session: Session, race_day_id: int, timezone: str = DEFAULT_BROADCAST_TIMEZONE) -> int:
    rows = session.execute(select(Interview).join(Recording).where(Recording.race_day_id == race_day_id)).scalars().all()
    for interview in rows: refresh_interview_context(session, interview, timezone)
    session.commit()
    return len(rows)
