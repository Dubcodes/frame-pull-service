"""Safety-first race-day coordination. It never enables itself."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CalendarMeeting, CalendarRace, RaceDay
from .operations import get_value
from .recorder import RecorderAdapter
from .time_model import in_broadcast_timezone


@dataclass(frozen=True)
class RecordingPlan:
    state: str
    start: datetime | None = None
    end: datetime | None = None
    reason: str = ""


def planned_recording_window(session: Session, race_day: RaceDay) -> RecordingPlan:
    times = [
        in_broadcast_timezone(value)
        for value in session.execute(
            select(CalendarRace.scheduled_time)
            .join(CalendarMeeting)
            .where(CalendarMeeting.race_day_id == race_day.id, CalendarRace.scheduled_time.is_not(None))
        ).scalars().all()
    ]
    if not times: return RecordingPlan("schedule_incomplete", reason="Race times are unavailable; no fallback window is configured.")
    lead = int(get_value(session, "recording_lead_minutes", 50)); tail = int(get_value(session, "recording_tail_minutes", 30))
    return RecordingPlan("planned", min(times) - timedelta(minutes=lead), max(times) + timedelta(minutes=tail), f"First race {min(times):%H:%M}; last race {max(times):%H:%M}; lead {lead}m; tail {tail}m.")


class RaceDayOrchestrator:
    def __init__(self, recorder: RecorderAdapter): self.recorder = recorder

    def tick(self, session: Session, race_day: RaceDay, now: datetime | None = None) -> dict:
        if not bool(get_value(session, "automatic_race_day_mode", False)):
            return {"state": "disabled", "reason": "Automatic Race Day Mode is off."}
        plan = planned_recording_window(session, race_day)
        if plan.state != "planned": return {"state": plan.state, "reason": plan.reason}
        status = self.recorder.refresh(session) if hasattr(self.recorder, "refresh") else self.recorder.status(session)
        if status.health == "unconfigured": return {"state": "recorder_unconfigured", "plan": plan}
        now = in_broadcast_timezone(now or datetime.now())
        if status.running and plan.end and now >= plan.end:
            status = self.recorder.stop_after_current_chunk(session)
            return {"state": "stopping", "plan": plan, "recorder_running": status.running}
        if not status.running and plan.start <= now <= plan.end:
            result = self.recorder.start(session, race_day, plan.end, manual=False)
            return {"state": "started", "plan": plan, "recorder_running": result.running}
        return {"state": "observed", "plan": plan, "recorder_running": status.running}


def closed_chunk_eligible(path: Path, active_path: Path | None, minimum_bytes: int, stable_seconds: int, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    try: stat = path.stat(); path.open("rb").close()
    except OSError: return False
    if active_path and path.resolve() == active_path.resolve(): return False
    return path.suffix.lower() == ".ts" and stat.st_size >= minimum_bytes and now.timestamp() - stat.st_mtime >= stable_seconds
