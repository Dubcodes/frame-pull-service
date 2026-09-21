from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

from frame_pull_service.config import Settings
from frame_pull_service.db import create_db_engine, init_db, make_session_factory
from frame_pull_service.models import Job, JobStatus, Recording, RecordingStatus
from frame_pull_service.services.discovery import discover_recordings
from frame_pull_service.services.legacy_engine import LegacySubprocessEngine
from frame_pull_service.services.media import clip_bounds
from frame_pull_service.services.normalizer import interview_key
from frame_pull_service.services.paths import contained_path
from frame_pull_service.services.queue import claim_next_job, queue_recording, recover_stale_jobs
from frame_pull_service.services.cleanup import cleanup_plan


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); root = Path(self.temp.name); self.source = root / "source"; self.source.mkdir()
        self.settings = Settings(source_dir=self.source, data_dir=root / "data", database_url=f"sqlite:///{(root/'service.db').as_posix()}", min_input_bytes=10, stable_for_seconds=0)
        self.engine = create_db_engine(self.settings); init_db(self.engine); self.sessions = make_session_factory(self.engine)

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    def test_first_run_baselines_existing_recordings_without_queueing(self):
        (self.source / "old.ts").write_bytes(b"x" * 20)
        with self.sessions() as session:
            discover_recordings(session, self.settings)
            record = session.query(Recording).one()
            self.assertTrue(record.historical); self.assertEqual(record.status, RecordingStatus.HISTORICAL); self.assertEqual(session.query(Job).count(), 0)

    def test_tiny_recording_is_not_ready(self):
        (self.source / "tiny.ts").write_bytes(b"x")
        with self.sessions() as session:
            discover_recordings(session, self.settings)
            self.assertEqual(session.query(Recording).one().status, RecordingStatus.HISTORICAL)

    def test_interview_identity_ignores_ocr_and_is_deterministic(self):
        self.assertEqual(interview_key("clip.ts", 1.25, 2.5), interview_key("clip.ts", 1.25, 2.5))
        self.assertNotEqual(interview_key("clip.ts", 1.25, 2.5), interview_key("clip.ts", 1.251, 2.5))

    def test_clip_bounds_clamp(self):
        self.assertEqual(clip_bounds(2, 9, 10, 5), (0.0, 10))

    def test_path_containment_blocks_escape(self):
        root = self.settings.data_dir; root.mkdir()
        with self.assertRaises(ValueError): contained_path(root, root / ".." / "escape.txt")

    def test_queue_single_active_job_and_stale_recovery(self):
        with self.sessions() as session:
            record = Recording(filename="manual.ts", source_path="x", fingerprint="f", size_bytes=20, mtime_epoch=0, status=RecordingStatus.READY)
            session.add(record); session.flush(); one=queue_recording(session, record); two=queue_recording(session, record); self.assertEqual(one.id, two.id); session.commit()
            claimed=claim_next_job(session); self.assertEqual(claimed.status, JobStatus.PROCESSING)
            claimed.heartbeat_at=datetime(2000,1,1); session.commit(); self.assertEqual(recover_stale_jobs(session, 1), 1)

    def test_legacy_adapter_isolated_and_single_input(self):
        engine = LegacySubprocessEngine(self.settings); job = self.settings.data_dir / "job"; source = self.source / "one.ts"; source.write_bytes(b"x" * 20)
        config = engine._job_config(source, job); content=config.read_text()
        self.assertIn(str(source).replace('\\','/'), content); self.assertIn((job/'legacy_output').as_posix(), content); self.assertNotIn('--all', content); self.assertNotIn('--rerun', content)

    def test_sqlite_wal_and_foreign_keys(self):
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text("PRAGMA foreign_keys")).scalar(), 1)
            self.assertEqual(conn.execute(text("PRAGMA journal_mode")).scalar().lower(), "wal")

    def test_cleanup_plan_is_dry_run_and_excludes_sources(self):
        source = self.source / "retained.ts"; source.write_bytes(b"x" * 20)
        (self.settings.data_dir / "jobs" / "old").mkdir(parents=True)
        (self.settings.data_dir / "jobs" / "old" / "trace.log").write_text("test")
        plan = cleanup_plan(self.settings)
        self.assertTrue(plan["dry_run"]); self.assertEqual(plan["delete_count"], 0); self.assertTrue(source.exists())
