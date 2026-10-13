from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from frame_pull_service.config import Settings
from frame_pull_service.db import create_db_engine, init_db, make_session_factory
from frame_pull_service.models import Job, JobStatus, Recording, RecordingStatus
from frame_pull_service.services.legacy_engine import LegacyResult, LegacySubprocessEngine
from frame_pull_service.services.operations import seed_settings, update_settings
from frame_pull_service.services.queue import claim_next_job, queue_recording, release_job
from frame_pull_service.services.worker import Worker
from scripts.qualify_one_chunk_e2e import ROOT, assert_single_input, validate_request


class OneChunkQualificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.production_source = self.root / "production-source"
        self.production_source.mkdir()
        self.legacy = self.root / "legacy"
        self.legacy.mkdir()
        (self.legacy / "config.yaml").write_text("input_file: old\noutput_root: old\nwork_root: old\n", encoding="utf-8")
        self.settings = Settings(
            source_dir=self.production_source,
            data_dir=self.root / "production-data",
            database_url=f"sqlite:///{(self.root / 'production.sqlite').as_posix()}",
            legacy_engine_root=self.legacy,
            min_input_bytes=1,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_validation_rejects_production_source_and_database_root(self):
        production_input = self.production_source / "one.ts"
        production_input.write_bytes(b"x")
        safe_run = ROOT / "data" / "v26-one-chunk" / "test-run"
        with self.assertRaisesRegex(ValueError, "Production source"):
            validate_request(production_input, self.settings, safe_run)
        external = self.root / "one.ts"
        external.write_bytes(b"x")
        with self.assertRaisesRegex(ValueError, "Qualification root"):
            validate_request(external, self.settings, self.settings.data_dir)

    def test_single_input_guard_rejects_a_second_recording(self):
        source = self.root / "isolated-source"
        source.mkdir()
        (source / "one.ts").write_bytes(b"1")
        self.assertEqual(assert_single_input(source, "one.ts").name, "one.ts")
        (source / "two.ts").write_bytes(b"2")
        with self.assertRaisesRegex(RuntimeError, "exactly"):
            assert_single_input(source, "one.ts")

    def test_legacy_command_has_one_explicit_input_and_no_batch_flags(self):
        source = self.root / "one.ts"
        config = self.root / "job" / "legacy_job_config.yaml"
        command = LegacySubprocessEngine(self.settings)._command(config, source)
        self.assertEqual(command.count("--input"), 1)
        self.assertEqual(command[command.index("--input") + 1], str(source))
        self.assertNotIn("--all", command)
        self.assertNotIn("--rerun", command)

    def _isolated_worker_fixture(self):
        source_dir = self.root / "isolated-source"
        source_dir.mkdir(exist_ok=True)
        source = source_dir / "one.ts"
        source.write_bytes(b"source-preserved")
        settings = self.settings.model_copy(update={
            "source_dir": source_dir,
            "data_dir": self.root / "isolated-data",
            "database_url": f"sqlite:///{(self.root / 'isolated.sqlite').as_posix()}",
        })
        settings.ensure_data_dirs()
        engine = create_db_engine(settings)
        init_db(engine)
        sessions = make_session_factory(engine)
        with sessions() as session:
            seed_settings(session, settings)
            update_settings(session, {"processing_paused": True, "source_deletion_enabled": False})
            recording = Recording(filename=source.name, source_path=str(source), fingerprint="one", size_bytes=source.stat().st_size,
                                  mtime_epoch=source.stat().st_mtime, status=RecordingStatus.READY, is_closed=True)
            session.add(recording)
            session.flush()
            job = queue_recording(session, recording)
            session.commit()
            self.assertIsNone(claim_next_job(session))
            update_settings(session, {"processing_paused": False})
            release_job(session, job.id)
            claimed = claim_next_job(session)
            job_id = claimed.id
        return settings, engine, sessions, source, job_id

    def test_one_job_zero_interviews_completes_and_preserves_source(self):
        settings, engine, sessions, source, job_id = self._isolated_worker_fixture()
        result = LegacyResult(settings.data_dir / "jobs" / "job_000001", self.root / "out", self.root / "work", self.root / "work" / "one", {}, {"segments": []})
        try:
            with patch("frame_pull_service.services.worker.LegacySubprocessEngine.run", return_value=result), patch("frame_pull_service.services.worker.normalize_run", return_value=[]):
                Worker(sessions, settings)._process(job_id)
            with sessions() as session:
                self.assertEqual(session.get(Job, job_id).status, JobStatus.COMPLETE)
                self.assertEqual(session.query(Job).count(), 1)
            self.assertTrue(source.exists())
        finally:
            engine.dispose()

    def test_detector_failure_marks_only_job_failed_and_preserves_source(self):
        settings, engine, sessions, source, job_id = self._isolated_worker_fixture()
        try:
            with patch("frame_pull_service.services.worker.LegacySubprocessEngine.run", side_effect=RuntimeError("fixture detector failure")):
                Worker(sessions, settings)._process(job_id)
            with sessions() as session:
                job = session.get(Job, job_id)
                self.assertEqual(job.status, JobStatus.FAILED)
                self.assertIn("fixture detector failure", job.error_summary)
                self.assertEqual(session.query(Job).count(), 1)
            self.assertTrue(source.exists())
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
