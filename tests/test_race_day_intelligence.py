from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from frame_pull_service.config import Settings
from frame_pull_service.db import create_db_engine, init_db, make_session_factory
from frame_pull_service.main import serialize_interview
from frame_pull_service.models import CalendarMeeting, CalendarRace, Interview, InterviewContext, RaceDay, Recording, RecordingSession, RecordingStatus
from frame_pull_service.services.context import refresh_interview_context, resolve_interview_context
from frame_pull_service.services.discovery import discover_recordings
from frame_pull_service.services.groups import list_groups
from frame_pull_service.services.operations import seed_settings, update_settings
from frame_pull_service.services.race_days import parse_recording_filename
from frame_pull_service.services.time_model import interview_wall_clock, recording_end


class RaceDayIntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.settings = Settings(
            source_dir=self.source,
            data_dir=self.root / "data",
            database_url=f"sqlite:///{(self.root / 'service.sqlite').as_posix()}",
            min_input_bytes=1,
            stable_for_seconds=0,
        )
        self.engine = create_db_engine(self.settings)
        init_db(self.engine)
        self.sessions = make_session_factory(self.engine)
        with self.sessions() as session:
            seed_settings(session, self.settings)
            day = RaceDay(race_date=date(2026, 9, 21))
            session.add(day)
            session.commit()
            self.day_id = day.id

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    def _meeting(self, session, track: str, races: list[tuple[int, datetime, dict | None]]):
        meeting = CalendarMeeting(race_day_id=self.day_id, track=track, source="fixture", provider_status="healthy")
        session.add(meeting)
        session.flush()
        for number, scheduled, context in races:
            session.add(CalendarRace(meeting_id=meeting.id, race_number=number, name=f"Race {number}", scheduled_time=scheduled, context=context or {}))
        session.flush()
        return meeting

    def _interview(self, session, key: str, started: datetime, source_start: float, *, name: str | None = None, track: str | None = None):
        recording = Recording(
            filename=f"trackside_20260921-1200_{key}.ts",
            source_path=str(self.source / f"{key}.ts"),
            fingerprint=f"recording-{key}",
            size_bytes=1,
            mtime_epoch=0,
            status=RecordingStatus.COMPLETE,
            race_day_id=self.day_id,
            recording_started_at=started,
        )
        session.add(recording)
        session.flush()
        interview = Interview(
            interview_key=f"interview-{key}", recording_id=recording.id, source_start=source_start,
            source_end=source_start + 10, bounds_basis="fixture", final_name=name, final_track=track,
        )
        session.add(interview)
        session.flush()
        return interview

    def test_filename_and_wall_clock_are_dst_safe(self):
        zone = ZoneInfo("Pacific/Auckland")
        spring = parse_recording_filename("trackside_20260921-1350_021.ts")
        autumn = parse_recording_filename("trackside_20260421-1350_021.ts")
        self.assertEqual(spring, datetime(2026, 9, 21, 13, 50, tzinfo=zone))
        self.assertEqual(parse_recording_filename("trackside_20260921-1350_17.ts"), datetime(2026, 9, 21, 13, 50, 17, tzinfo=zone))
        self.assertEqual(spring.utcoffset(), timedelta(hours=12))
        self.assertEqual(autumn.utcoffset(), timedelta(hours=12))
        summer = parse_recording_filename("trackside_20261221-1350_021.ts")
        self.assertEqual(summer.utcoffset(), timedelta(hours=13))
        self.assertEqual(interview_wall_clock(spring, 23 * 60 + 17.5), datetime(2026, 9, 21, 14, 13, 17, 500000, tzinfo=zone))
        self.assertEqual(recording_end(spring, 90.0), datetime(2026, 9, 21, 13, 51, 30, tzinfo=zone))
        self.assertIsNone(parse_recording_filename("not-a-trackside-file.ts"))

    def test_resolver_preserves_runner_evidence_without_identity_inference(self):
        with self.sessions() as session:
            runners = {"runners": [{"number": 7, "horse": "Clear Winner", "jockey": "A Rider", "trainer": "T Trainer", "scratched": False}]}
            self._meeting(session, "Riccarton", [(5, datetime(2026, 9, 21, 12, 10), runners)])
            interview = self._interview(session, "runner", datetime(2026, 9, 21, 12, 0), 15 * 60)
            payload = refresh_interview_context(session, interview)
            session.commit()
            self.assertEqual(payload["confidence"], "high")
            self.assertEqual(payload["resolved_track"], "Riccarton")
            self.assertEqual(payload["race_number"], 5)
            self.assertEqual(payload["runners"][0]["horse"], "Clear Winner")
            self.assertNotIn("identity", payload)
            persisted = session.query(InterviewContext).one()
            self.assertEqual(persisted.race_id, payload["race_id"])

    def test_resolver_handles_before_next_conflict_and_unknown_conservatively(self):
        with self.sessions() as session:
            self._meeting(session, "Riccarton", [(4, datetime(2026, 9, 21, 12, 0), None), (5, datetime(2026, 9, 21, 12, 25), None)])
            update_settings(session, {"context_post_race_minutes": 5})
            before_next = self._interview(session, "before", datetime(2026, 9, 21, 12, 0), 15 * 60)
            self.assertEqual(resolve_interview_context(session, before_next)["race_number"], 5)
            self._meeting(session, "Cambridge", [(1, datetime(2026, 9, 21, 12, 15), None)])
            conflict = self._interview(session, "conflict", datetime(2026, 9, 21, 12, 0), 15 * 60)
            self.assertEqual(resolve_interview_context(session, conflict)["confidence"], "conflict")
            distant = self._interview(session, "distant", datetime(2026, 9, 21, 15, 0), 0)
            self.assertEqual(resolve_interview_context(session, distant)["confidence"], "unknown")

    def test_group_location_conflict_and_unknown_people_remain_separate(self):
        with self.sessions() as session:
            first = self._interview(session, "group-one", datetime(2026, 9, 21, 12, 0), 0, name="Alex Smith", track="Riccarton")
            second = self._interview(session, "group-two", datetime(2026, 9, 21, 13, 0), 0, name="Alex Smith", track="Cambridge")
            self._interview(session, "unknown-one", datetime(2026, 9, 21, 14, 0), 0)
            self._interview(session, "unknown-two", datetime(2026, 9, 21, 15, 0), 0)
            session.commit()
            groups = list_groups(session)
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0]["interview_ids"], [first.id, second.id])
            self.assertEqual(groups[0]["location_state"], "conflict")
            self.assertIsNone(groups[0]["track"])

    def test_active_recorder_output_is_absent_until_closed_and_stable(self):
        active = self.source / "trackside_20260921-1200_001.ts"
        active.write_bytes(b"x" * 100)
        with self.sessions() as session:
            # Establishing one prior recording makes a subsequently closed file eligible for READY.
            session.add(Recording(filename="prior.ts", source_path="prior", fingerprint="prior", size_bytes=1, mtime_epoch=0, status=RecordingStatus.HISTORICAL))
            session.add(RecordingSession(status="recording", active_path=str(active)))
            session.commit()
            result = discover_recordings(session, self.settings, datetime.now() + timedelta(seconds=30))
            self.assertEqual(result["created"], 0)
            self.assertIsNone(session.query(Recording).filter_by(filename=active.name).one_or_none())
            session.query(RecordingSession).update({RecordingSession.status: "stopped"})
            session.commit()
            discover_recordings(session, self.settings, datetime.now() + timedelta(seconds=30))
            closed = session.query(Recording).filter_by(filename=active.name).one()
            self.assertEqual(closed.status, RecordingStatus.READY)
            self.assertFalse(closed.jobs)

    def test_bridge_context_has_no_local_source_path(self):
        with self.sessions() as session:
            self._meeting(session, "Riccarton", [(2, datetime(2026, 9, 21, 12, 10), None)])
            interview = self._interview(session, "bridge", datetime(2026, 9, 21, 12, 0), 10 * 60, name="Alex Smith")
            session.commit()
            payload = serialize_interview(session, interview)
            self.assertEqual(payload["calendar_context"]["resolved_track"], "Riccarton")
            self.assertNotIn("source_path", payload)
            self.assertNotIn(str(self.source), str(payload))

