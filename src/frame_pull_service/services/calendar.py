from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CalendarMeeting, CalendarRace


class RaceCalendarProvider:
    """Provider boundary. Calendar context informs review but never identity."""
    name = "unconfigured"

    def meetings_for_day(self, _race_date):
        return []


def upsert_meeting_context(session: Session, race_day_id: int, payload: dict) -> CalendarMeeting:
    track = str(payload["track"]).strip()
    meeting = session.execute(select(CalendarMeeting).where(CalendarMeeting.race_day_id == race_day_id, CalendarMeeting.track == track)).scalar_one_or_none()
    if meeting is None:
        meeting = CalendarMeeting(race_day_id=race_day_id, track=track); session.add(meeting); session.flush()
    meeting.source = payload.get("source", "manual")
    meeting.confidence = payload.get("confidence")
    meeting.refreshed_at = datetime.utcnow()
    for race in payload.get("races", []):
        number = int(race["race_number"])
        item = session.execute(select(CalendarRace).where(CalendarRace.meeting_id == meeting.id, CalendarRace.race_number == number)).scalar_one_or_none()
        if item is None:
            item = CalendarRace(meeting_id=meeting.id, race_number=number); session.add(item)
        item.name, item.context = race.get("name"), race.get("context", {})
    session.commit()
    return meeting
