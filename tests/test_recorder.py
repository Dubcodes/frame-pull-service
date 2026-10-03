from __future__ import annotations

import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from frame_pull_service.config import Settings
from frame_pull_service.db import create_db_engine, init_db, make_session_factory
from frame_pull_service.main import create_app
from frame_pull_service.models import JobStatus, Recording, RecordingSession, RecordingStatus
from frame_pull_service.models import CalendarMeeting, CalendarRace, RaceDay
from frame_pull_service.services.discovery import discover_recordings
from frame_pull_service.services.operations import seed_settings, update_settings
from frame_pull_service.services.orchestrator import RaceDayOrchestrator, planned_recording_window
from frame_pull_service.services.queue import claim_next_job
from frame_pull_service.services.recorder import FFmpegRecorder, RecorderAdapter, RecorderState, redact_input


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output = self.root / "recordings"
        self.output.mkdir()
        self.input = self.root / "synthetic.mp4"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=440", "-t", "2", "-c:v", "mpeg4", "-c:a", "aac", str(self.input),
        ], check=True)
        self.settings = Settings(
            source_dir=self.output, recorder_output_dir=self.output, data_dir=self.root / "data",
            database_url=f"sqlite:///{(self.root / 'service.sqlite').as_posix()}", min_input_bytes=1,
            stable_for_seconds=0, recorder_enabled=True, recorder_input=str(self.input),
            recorder_chunk_minutes=0.05, recorder_min_free_space_gb=0,
            recorder_profile="generic", recorder_origin="", recorder_referer="", recorder_program=None,
        )
        self.engine = create_db_engine(self.settings)
        init_db(self.engine)
        self.sessions = make_session_factory(self.engine)
        with self.sessions() as session:
            seed_settings(session, self.settings)
            # Avoid first-run historical handling in the lifecycle assertion.
            session.add(Recording(filename="prior.ts", source_path="prior", fingerprint="prior", size_bytes=1, mtime_epoch=0, status=RecordingStatus.HISTORICAL))
            session.commit()
        self.recorder = FFmpegRecorder(self.settings)

    def tearDown(self):
        try:
            with self.sessions() as session:
                self.recorder.stop(session, hard=True)
                for _ in range(20):
                    state = self.recorder.refresh(session)
                    if not state.running:
                        break
                    time.sleep(0.1)
        finally:
            self.engine.dispose()
            self.temp.cleanup()

    def _wait_for(self, predicate, timeout: float = 15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.sessions() as session:
                state = self.recorder.refresh(session)
                if predicate(state):
                    return state
            time.sleep(0.2)
        self.fail("recorder did not reach the expected state")

    def test_disabled_or_unconfigured_recorder_cannot_start_and_redacts_input(self):
        self.assertEqual(redact_input("https://user:secret@example.test/live?token=private"), "Configured")
        with self.sessions() as session:
            update_settings(session, {"recorder_enabled": False})
            with self.assertRaisesRegex(RuntimeError, "disabled"):
                self.recorder.start(session)
            update_settings(session, {"recorder_enabled": True})
            self.settings.recorder_input = ""
            with self.assertRaisesRegex(RuntimeError, "input"):
                self.recorder.start(session)

    def test_trackside_profile_places_structured_input_options_before_input(self):
        source = "https://user:secret@stream.example.test/live/master.m3u8?token=private"
        self.settings.recorder_input = source
        self.settings.recorder_profile = "trackside_hls"
        self.settings.recorder_origin = "https://origin.example.test"
        self.settings.recorder_referer = "https://origin.example.test/"
        self.settings.recorder_program = 1
        command = self.recorder._command(1)
        input_index = command.index("-i")
        self.assertLess(command.index("-readrate"), input_index)
        self.assertLess(command.index("-fflags"), input_index)
        self.assertLess(command.index("-rw_timeout"), input_index)
        self.assertLess(command.index("-reconnect"), input_index)
        self.assertLess(command.index("-headers"), input_index)
        self.assertEqual(command[command.index("-headers") + 1], "Origin: https://origin.example.test\r\nReferer: https://origin.example.test/\r\n")
        self.assertGreater(command.index("0:p:1:v:0"), input_index)
        self.assertGreater(command.index("0:p:1:a:0"), input_index)
        self.assertNotIn("0", command[input_index + 2:])
        self.assertNotIn("-nostdin", command)
        logged = self.recorder._log_command(command)
        self.assertNotIn("secret", logged)
        self.assertNotIn("private", logged)
        self.assertNotIn("origin.example.test", logged)

    def test_trackside_profile_requires_headers_and_explicit_program(self):
        self.settings.recorder_profile = "trackside_hls"
        with self.sessions() as session:
            with self.assertRaisesRegex(RuntimeError, "Origin and Referer"):
                self.recorder.start(session)
        self.settings.recorder_origin = "https://origin.example.test"
        self.settings.recorder_referer = "https://origin.example.test/"
        with self.sessions() as session:
            with self.assertRaisesRegex(RuntimeError, "program"):
                self.recorder.start(session)

    def test_synthetic_stream_copy_rolls_and_normal_discovery_never_sees_active_chunk(self):
        with self.sessions() as session:
            first_state = self.recorder.start(session)
            self.assertTrue(first_state.running)
        first = self._wait_for(lambda state: state.active_path is not None)
        self.assertIsNotNone(first.active_path)
        self.assertTrue(first.active_path.name.startswith("trackside_"))
        with self.sessions() as session:
            result = discover_recordings(session, self.settings, datetime.now() + timedelta(seconds=10))
            self.assertEqual(result["created"], 0)
            self.assertIsNone(session.query(Recording).filter_by(filename=first.active_path.name).one_or_none())
        second = self._wait_for(lambda state: state.active_chunk_sequence and first.active_chunk_sequence and state.active_chunk_sequence > first.active_chunk_sequence)
        with self.sessions() as session:
            discover_recordings(session, self.settings, datetime.now() + timedelta(seconds=10))
            closed = session.query(Recording).filter_by(filename=first.active_path.name).one()
            self.assertEqual(closed.status, RecordingStatus.READY)
            self.assertIsNone(session.query(Recording).filter_by(filename=second.active_path.name).one_or_none())
        with self.sessions() as session:
            self.recorder.stop(session)
            discover_recordings(session, self.settings, datetime.now() + timedelta(seconds=10))
            self.assertIsNone(session.query(Recording).filter_by(filename=second.active_path.name).one_or_none())
        self._wait_for(lambda state: not state.running)
        chunks = sorted(self.output.glob("trackside_*.ts"))
        self.assertGreaterEqual(len(chunks), 2)
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1", str(chunks[0])], capture_output=True, text=True, check=True)
        self.assertIn("duration=", probe.stdout)

    def test_autoqueue_is_independent_from_processing_pause(self):
        chunk = self.output / "trackside_20261002-1200_001.ts"
        chunk.write_bytes(b"x" * 100)
        with self.sessions() as session:
            update_settings(session, {"autoqueue_closed_recordings": True, "processing_paused": True})
            discover_recordings(session, self.settings, datetime.now() + timedelta(seconds=10))
            item = session.query(Recording).filter_by(filename=chunk.name).one()
            self.assertEqual(item.status, RecordingStatus.QUEUED)
            self.assertIsNone(claim_next_job(session, max_concurrent=1, paused=True))

    def test_dead_persisted_pid_is_failed_without_killing_any_process(self):
        with self.sessions() as session:
            row = RecordingSession(status="recording", adapter_name="ffmpeg", ffmpeg_pid=99999999, started_at=datetime.now())
            session.add(row)
            session.commit()
            state = self.recorder.recover(session)
            self.assertEqual(state.state, "failed")
            self.assertIn("no longer running", state.error)

    def test_duplicate_start_is_rejected_and_stop_is_idempotent(self):
        with self.sessions() as session:
            self.recorder.start(session)
            with self.assertRaisesRegex(RuntimeError, "already active"):
                self.recorder.start(session)
            self.assertTrue(self.recorder.stop(session).running)
            self.assertTrue(self.recorder.stop(session).running)
        self._wait_for(lambda state: not state.running)

    def test_automatic_orchestration_requires_valid_plan_and_never_duplicates_a_session(self):
        class EligibleRecorder(RecorderAdapter):
            def __init__(self): self.started = False; self.calls = 0
            def status(self, _session=None): return RecorderState(state="recording" if self.started else "idle", health="healthy")
            def start(self, _session, _race_day=None, _planned_end=None, *, manual=True):
                self.calls += 1; self.started = True; return self.status()

        recorder = EligibleRecorder()
        with self.sessions() as session:
            day = RaceDay(race_date=datetime.now().date()); session.add(day); session.flush()
            update_settings(session, {"automatic_race_day_mode": True})
            orchestrator = RaceDayOrchestrator(recorder)
            self.assertEqual(orchestrator.tick(session, day)["state"], "schedule_incomplete")
            meeting = CalendarMeeting(race_day_id=day.id, track="Fixture", provider_status="healthy"); session.add(meeting); session.flush()
            session.add(CalendarRace(meeting_id=meeting.id, race_number=1, scheduled_time=datetime.now() + timedelta(minutes=50)))
            session.commit()
            plan = planned_recording_window(session, day)
            self.assertEqual(orchestrator.tick(session, day, now=plan.start)["state"], "started")
            self.assertEqual(orchestrator.tick(session, day, now=plan.start)["state"], "observed")
            self.assertEqual(recorder.calls, 1)

    def test_disk_threshold_blocks_start(self):
        with self.sessions() as session:
            update_settings(session, {"recorder_min_free_space_gb": 999999})
            with self.assertRaisesRegex(RuntimeError, "disk space"):
                self.recorder.start(session)

    def test_status_api_redacts_configured_input_and_rejects_disabled_start(self):
        secret = "https://user:secret@example.test/live?token=private"
        settings = Settings(
            source_dir=self.root / "api-source", data_dir=self.root / "api-data",
            database_url=f"sqlite:///{(self.root / 'api.sqlite').as_posix()}",
            recorder_enabled=False, recorder_input=secret,
        )
        with TestClient(create_app(settings)) as client:
            status = client.get("/api/recorder/status")
            self.assertEqual(status.status_code, 200)
            self.assertNotIn("secret", status.text)
            self.assertNotIn("private", status.text)
            self.assertEqual(client.post("/api/recorder/start", json={}).status_code, 409)

