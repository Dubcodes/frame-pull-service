from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from frame_pull_service.config import Settings
from frame_pull_service.db import create_db_engine, init_db, make_session_factory
from frame_pull_service.models import CalendarMeeting, CalendarRace, Interview, RaceDay, Recording, RecordingStatus
from frame_pull_service.services.calendar import (CalendarMeetingPayload, CalendarProviderError,
    CalendarRacePayload, CalendarRunner, LoveRacingCalendarProvider, RaceCalendarProvider,
    discover_meeting_urls, parse_meeting_html, refresh_race_day)
from frame_pull_service.services.operations import seed_settings, update_settings
from frame_pull_service.services.orchestrator import RaceDayOrchestrator, closed_chunk_eligible, planned_recording_window
from frame_pull_service.services.recorder import RecorderAdapter
from frame_pull_service.services.context import resolve_interview_context


class CalendarAndOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.settings = Settings(data_dir=self.root / "data", source_dir=self.root / "source", database_url=f"sqlite:///{(self.root / 'db.sqlite').as_posix()}")
        self.engine = create_db_engine(self.settings); init_db(self.engine); self.sessions = make_session_factory(self.engine)
        with self.sessions() as session: seed_settings(session, self.settings); self.day = RaceDay(race_date=date(2026, 9, 21)); session.add(self.day); session.commit(); self.day_id = self.day.id

    def tearDown(self): self.engine.dispose(); self.temp.cleanup()

    def test_provider_normalizes_multiple_meetings_and_race_context(self):
        calendar = '<a href="/RaceInfo/11/Meeting-Overview.aspx">A</a><a href="/RaceInfo/12/Meeting-Overview.aspx">B</a>'
        fixture = '<h1>Race Meeting for Canterbury Racing at Riccarton on 21 September 2026</h1><div>Race 1 Spring Handicap 12:20 pm</div><div>Race 2 Cup Trial 1:05 pm</div>'
        def fetch(url): return calendar if url.endswith("RaceInfo.aspx") else fixture
        provider = LoveRacingCalendarProvider(fetch_html=fetch)
        self.assertEqual(len(discover_meeting_urls(calendar)), 2)
        with self.sessions() as session:
            result = refresh_race_day(session, session.get(RaceDay, self.day_id), provider)
            self.assertTrue(result["ok"]); meeting = session.query(CalendarMeeting).one(); races = session.query(CalendarRace).all()
            self.assertEqual(meeting.track, "Riccarton"); self.assertEqual(len(races), 2); self.assertEqual(races[0].scheduled_time, datetime(2026, 9, 21, 12, 20))

    def test_provider_failure_preserves_cached_context(self):
        payload = CalendarMeetingPayload("Riccarton", "https://example.test", date(2026, 9, 21), [CalendarRacePayload(1, "Race", datetime(2026, 9, 21, 12, 20), runners=[CalendarRunner(1, "Example", "J Rider", "T Trainer", True)])])
        with self.sessions() as session:
            from frame_pull_service.services.calendar import upsert_meeting_context
            upsert_meeting_context(session, self.day_id, payload); session.commit()
            class Failing(RaceCalendarProvider):
                name = "loveracing"
                def meetings_for_day(self, _): raise CalendarProviderError("offline")
            result = refresh_race_day(session, session.get(RaceDay, self.day_id), Failing())
            self.assertFalse(result["ok"]); self.assertTrue(result["preserved_cache"]); self.assertEqual(session.query(CalendarMeeting).one().provider_status, "error")

    def test_parse_meeting_rejects_untrusted_track_and_normalizes_date(self):
        parsed = parse_meeting_html('<h1>Race Meeting for Example Club at Cambridge on 21 September 2026</h1><div>Race 3 Feature 2:30pm</div>', 'https://loveracing.nz/RaceInfo/1/Meeting-Overview.aspx')
        self.assertEqual(parsed.track, "Cambridge"); self.assertEqual(parsed.race_date, date(2026, 9, 21)); self.assertEqual(parsed.races[0].scheduled_time, datetime(2026, 9, 21, 14, 30))

    def test_plan_and_orchestrator_remain_disabled_or_unconfigured(self):
        with self.sessions() as session:
            day = session.get(RaceDay, self.day_id); self.assertEqual(planned_recording_window(session, day).state, "schedule_incomplete")
            self.assertEqual(RaceDayOrchestrator(RecorderAdapter()).tick(session, day)["state"], "disabled")
            session.add(CalendarMeeting(race_day_id=day.id, track="Riccarton")); session.flush(); meeting = session.query(CalendarMeeting).one(); session.add(CalendarRace(meeting_id=meeting.id, race_number=1, scheduled_time=datetime(2026, 9, 21, 12, 20))); session.commit()
            plan = planned_recording_window(session, day); self.assertEqual(plan.start, datetime(2026, 9, 21, 11, 30)); self.assertEqual(plan.end, datetime(2026, 9, 21, 12, 50))
            update_settings(session, {"automatic_race_day_mode": True}); self.assertEqual(RaceDayOrchestrator(RecorderAdapter()).tick(session, day)["state"], "recorder_unconfigured")

    def test_active_chunk_never_qualifies_and_closed_chunk_requires_stability(self):
        path = self.root / "hour.ts"; path.write_bytes(b"x" * 100)
        now = datetime.now(); self.assertFalse(closed_chunk_eligible(path, path, 10, 1, now + timedelta(seconds=10)))
        self.assertFalse(closed_chunk_eligible(path, None, 200, 1, now + timedelta(seconds=10)))
        self.assertTrue(closed_chunk_eligible(path, None, 10, 0, now + timedelta(seconds=10)))

    def test_context_is_explicitly_uncertain_when_two_meetings_are_equally_plausible(self):
        with self.sessions() as session:
            day = session.get(RaceDay, self.day_id)
            first, second = CalendarMeeting(race_day_id=day.id, track="Riccarton"), CalendarMeeting(race_day_id=day.id, track="Cambridge")
            session.add_all([first, second]); session.flush()
            when = datetime(2026, 9, 21, 12, 20)
            session.add_all([CalendarRace(meeting_id=first.id, race_number=1, scheduled_time=when), CalendarRace(meeting_id=second.id, race_number=2, scheduled_time=when)])
            recording = Recording(filename="trackside_20260921-1200_001.ts", source_path="x", fingerprint="context", size_bytes=1, mtime_epoch=0, status=RecordingStatus.COMPLETE, race_day_id=day.id, recording_started_at=datetime(2026, 9, 21, 12, 0))
            session.add(recording); session.flush(); interview = Interview(interview_key="context", recording_id=recording.id, source_start=20 * 60, source_end=21 * 60, bounds_basis="test"); session.add(interview); session.commit()
            context = resolve_interview_context(session, interview)
            self.assertEqual(context["confidence"], "conflict"); self.assertNotIn("identity", context)
