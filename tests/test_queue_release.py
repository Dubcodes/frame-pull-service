from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select, text

from frame_pull_service.config import Settings
from frame_pull_service.db import create_db_engine, init_db, make_session_factory
from frame_pull_service.main import create_app
from frame_pull_service.models import Job, JobStatus, Recording, RecordingStatus
from frame_pull_service.services.operations import seed_settings
from frame_pull_service.services.queue import claim_next_job, queue_recording, set_jobs_released


class QueueReleaseTests(unittest.TestCase):
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
        )
        self.engine = create_db_engine(self.settings)
        init_db(self.engine)
        self.sessions = make_session_factory(self.engine)
        with self.sessions() as session:
            seed_settings(session, self.settings)

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    def add_job(self, name: str, status: JobStatus = JobStatus.QUEUED) -> int:
        with self.sessions() as session:
            recording = Recording(filename=name, source_path=name, fingerprint=name, size_bytes=10,
                                  mtime_epoch=1, status=RecordingStatus.READY)
            session.add(recording)
            session.flush()
            job = queue_recording(session, recording)
            job.status = status
            session.commit()
            return job.id

    def test_held_is_default_and_claim_requires_release_and_resume(self):
        held_id = self.add_job("held.ts")
        released_id = self.add_job("released.ts")
        with self.sessions() as session:
            self.assertFalse(session.get(Job, held_id).released_for_processing)
            self.assertIsNone(claim_next_job(session, paused=False))
            first = set_jobs_released(session, [released_id], True)
            again = set_jobs_released(session, [released_id], True)
            self.assertEqual(first["changed"], [released_id])
            self.assertEqual(again["unchanged"], [released_id])
            self.assertIsNone(claim_next_job(session, paused=True))
            claimed = claim_next_job(session, paused=False)
            self.assertEqual(claimed.id, released_id)
            self.assertEqual(session.get(Job, held_id).status, JobStatus.QUEUED)

    def test_hold_removes_eligibility_and_only_explicit_ids_change(self):
        first = self.add_job("first.ts")
        second = self.add_job("second.ts")
        with self.sessions() as session:
            set_jobs_released(session, [first, second], True)
            result = set_jobs_released(session, [first], False)
            self.assertEqual(result["changed"], [first])
            self.assertFalse(session.get(Job, first).released_for_processing)
            self.assertTrue(session.get(Job, second).released_for_processing)
            self.assertEqual(claim_next_job(session, paused=False).id, second)

    def test_claim_matrix_order_and_held_jobs_do_not_fill_worker_slots(self):
        held = [self.add_job("held-one.ts"), self.add_job("held-two.ts")]
        released = [self.add_job("released-one.ts"), self.add_job("released-two.ts")]
        with self.sessions() as session:
            set_jobs_released(session, released, True)
            self.assertIsNone(claim_next_job(session, max_concurrent=4, paused=True))
            first = claim_next_job(session, max_concurrent=4, paused=False)
            second = claim_next_job(session, max_concurrent=4, paused=False)
            self.assertEqual([first.id, second.id], released)
            self.assertIsNone(claim_next_job(session, max_concurrent=4, paused=False))
            self.assertTrue(all(session.get(Job, job_id).status == JobStatus.QUEUED for job_id in held))

    def test_retry_and_reprocess_create_fresh_held_jobs(self):
        failed = self.add_job("failed-retry.ts", JobStatus.FAILED)
        with self.sessions() as session:
            old = session.get(Job, failed)
            old.released_for_processing = True
            recording = session.get(Recording, old.recording_id)
            recording.status = RecordingStatus.FAILED
            retry = queue_recording(session, recording)
            session.commit()
            self.assertNotEqual(retry.id, old.id)
            self.assertFalse(retry.released_for_processing)

        complete = self.add_job("complete-reprocess.ts", JobStatus.COMPLETE)
        app = create_app(self.settings)
        with self.sessions() as session:
            old = session.get(Job, complete)
            recording_id = old.recording_id
            session.get(Recording, recording_id).status = RecordingStatus.COMPLETE
            old.released_for_processing = True
            session.commit()
        with TestClient(app) as client:
            response = client.post(f"/api/recordings/{recording_id}/reprocess")
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["released_for_processing"])

    def test_invalid_status_rejects_entire_bulk_change(self):
        queued = self.add_job("queued.ts")
        complete = self.add_job("complete.ts", JobStatus.COMPLETE)
        with self.sessions() as session:
            with self.assertRaisesRegex(ValueError, "only queued"):
                set_jobs_released(session, [queued, complete], True)
            session.rollback()
            self.assertFalse(session.get(Job, queued).released_for_processing)
            with self.assertRaises(LookupError):
                set_jobs_released(session, [999999], True)

    def test_two_workers_never_claim_same_released_job(self):
        ids = [self.add_job("one.ts"), self.add_job("two.ts")]
        with self.sessions() as session:
            set_jobs_released(session, ids, True)
        barrier = threading.Barrier(2)
        claimed: list[int] = []
        errors: list[Exception] = []

        def run_claim() -> None:
            try:
                with self.sessions() as session:
                    barrier.wait()
                    job = claim_next_job(session, max_concurrent=2, paused=False)
                    if job:
                        claimed.append(job.id)
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        workers = [threading.Thread(target=run_claim), threading.Thread(target=run_claim)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertFalse(errors)
        self.assertEqual(sorted(claimed), sorted(ids))

    def test_api_counts_explicit_release_hold_and_resume_wording(self):
        first = self.add_job("api-one.ts")
        second = self.add_job("api-two.ts")
        app = create_app(self.settings)
        with TestClient(app) as client:
            health = client.get("/api/health").json()
            self.assertEqual(health["held_jobs"], 2)
            self.assertEqual(health["released_jobs"], 0)
            self.assertEqual(client.post(f"/api/jobs/{first}/release").status_code, 200)
            self.assertEqual(client.post("/api/jobs/bulk/release", json={"job_ids": [second]}).status_code, 200)
            self.assertEqual(client.post(f"/api/jobs/{first}/hold").status_code, 200)
            resumed = client.post("/api/operations/resume").json()
            self.assertIn("only explicitly released", resumed["message"])
            page = client.get("/").text
            self.assertIn("Resume released jobs", page)
            self.assertIn("Release selected jobs", client.get("/static/dashboard.js").text)
            self.assertEqual(client.post("/api/jobs/bulk/release", json={"job_ids": [999999]}).status_code, 404)
            self.assertEqual(client.post("/api/jobs/bulk/release", json={"job_ids": []}).status_code, 409)


class QueueMigrationTests(unittest.TestCase):
    def test_additive_migration_preserves_jobs_and_holds_historical_queue(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = root / "legacy.sqlite"
            connection = sqlite3.connect(database)
            connection.executescript("""
                CREATE TABLE recordings (
                    id INTEGER PRIMARY KEY, filename VARCHAR(255), source_path TEXT, fingerprint VARCHAR(128),
                    size_bytes INTEGER, mtime_epoch FLOAT, first_discovered_at DATETIME, stable_at DATETIME,
                    status VARCHAR(32), historical BOOLEAN
                );
                CREATE TABLE jobs (
                    id INTEGER PRIMARY KEY, recording_id INTEGER, status VARCHAR(32), queued_at DATETIME,
                    started_at DATETIME, finished_at DATETIME, heartbeat_at DATETIME, attempt_count INTEGER,
                    progress_stage VARCHAR(120), progress_percent FLOAT, error_summary TEXT, log_path TEXT,
                    processing_run_id INTEGER
                );
                INSERT INTO recordings VALUES (7, 'old.ts', 'old.ts', 'old', 10, 1, CURRENT_TIMESTAMP, NULL, 'QUEUED', 1);
                INSERT INTO jobs VALUES (11, 7, 'QUEUED', CURRENT_TIMESTAMP, NULL, NULL, NULL, 0, 'queued', 0, NULL, NULL, NULL);
            """)
            connection.commit()
            before = connection.execute("SELECT id, recording_id, status FROM jobs").fetchall()
            connection.close()

            settings = Settings(source_dir=root / "source", data_dir=root / "data",
                                database_url=f"sqlite:///{database.as_posix()}")
            engine = create_db_engine(settings)
            init_db(engine)
            with engine.connect() as migrated:
                columns = {row[1] for row in migrated.execute(text("PRAGMA table_info(jobs)"))}
                after = migrated.execute(text("SELECT id, recording_id, status, released_for_processing FROM jobs")).fetchall()
            engine.dispose()
            self.assertIn("released_for_processing", columns)
            self.assertEqual(before, [(row[0], row[1], row[2]) for row in after])
            self.assertEqual(after[0][3], 0)


if __name__ == "__main__":
    unittest.main()
