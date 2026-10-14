from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from frame_pull_service.config import Settings
from frame_pull_service.db import create_db_engine, init_db, pending_schema_additions
from frame_pull_service.main import create_app


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def create_legacy_database(path: Path) -> list[tuple[int, int, str]]:
    connection = sqlite3.connect(path)
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
    rows = connection.execute("SELECT id, recording_id, status FROM jobs ORDER BY id").fetchall()
    connection.close()
    return rows


def job_columns(path: Path) -> list[str]:
    connection = sqlite3.connect(path)
    try:
        return [row[1] for row in connection.execute("PRAGMA table_info(jobs)")]
    finally:
        connection.close()


class StartupMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "legacy.sqlite"
        self.before_rows = create_legacy_database(self.database)
        self.source = self.root / "source"
        self.source.mkdir()
        self.runtime_data = self.root / "runtime-data"
        self.settings = Settings(
            source_dir=self.source,
            data_dir=self.runtime_data,
            database_url=f"sqlite:///{self.database.as_posix()}",
            min_input_bytes=1,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_fresh_module_import_does_not_migrate_or_create_runtime_dirs(self):
        environment = os.environ.copy()
        environment.update({
            "FRAME_PULL_SOURCE_DIR": str(self.source),
            "FRAME_PULL_DATA_DIR": str(self.runtime_data),
            "FRAME_PULL_DATABASE_URL": f"sqlite:///{self.database.as_posix()}",
        })
        result = subprocess.run(
            [sys.executable, "-c", "import frame_pull_service.main"],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("released_for_processing", job_columns(self.database))
        self.assertFalse(self.runtime_data.exists())

    def test_create_app_without_lifespan_leaves_migration_pending(self):
        app = create_app(self.settings)
        self.assertEqual(
            pending_schema_additions(app.state.service.engine)["jobs"],
            ["released_for_processing"],
        )
        self.assertNotIn("released_for_processing", job_columns(self.database))
        self.assertFalse(self.runtime_data.exists())
        app.state.service.engine.dispose()

    def test_read_only_plan_then_explicit_initialization(self):
        engine = create_db_engine(self.settings)
        before_columns = job_columns(self.database)
        plan = pending_schema_additions(engine)
        self.assertEqual(plan["jobs"], ["released_for_processing"])
        self.assertEqual(job_columns(self.database), before_columns)
        init_db(engine)
        self.assertEqual(pending_schema_additions(engine), {})
        self.assertIn("released_for_processing", job_columns(self.database))
        engine.dispose()

    def test_read_only_plan_does_not_create_an_absent_sqlite_database(self):
        database = self.root / "not-created.sqlite"
        settings = self.settings.model_copy(update={"database_url": f"sqlite:///{database.as_posix()}"})
        engine = create_db_engine(settings)
        self.assertEqual(pending_schema_additions(engine), {})
        self.assertFalse(database.exists())
        engine.dispose()

    def test_lifespan_applies_migration_before_health_queries_and_preserves_rows(self):
        app = create_app(self.settings)
        self.assertIn("jobs", pending_schema_additions(app.state.service.engine))
        with TestClient(app) as client:
            health = client.get("/api/health")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()["held_jobs"], 1)
            self.assertEqual(health.json()["released_jobs"], 0)
            self.assertEqual(pending_schema_additions(app.state.service.engine), {})

        connection = sqlite3.connect(self.database)
        try:
            rows = connection.execute(
                "SELECT id, recording_id, status, released_for_processing FROM jobs ORDER BY id"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(self.before_rows, [(row[0], row[1], row[2]) for row in rows])
        self.assertEqual(rows[0][3], 0)


if __name__ == "__main__":
    unittest.main()
