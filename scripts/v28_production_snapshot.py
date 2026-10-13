from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path


DATABASE = Path(r"J:\projects\frame-pull-service\data\frame_pull.db")


def snapshot() -> dict:
    connection = sqlite3.connect(f"file:{DATABASE.as_posix()}?mode=ro", uri=True)
    try:
        queued = connection.execute(
            "SELECT id, recording_id, status FROM jobs WHERE status = 'QUEUED' ORDER BY id"
        ).fetchall()
        payload = json.dumps(queued, separators=(",", ":"))
        settings = {
            key: json.loads(value)
            for key, value in connection.execute(
                "SELECT key, value FROM operational_settings "
                "WHERE key IN ('processing_paused', 'autoqueue_closed_recordings', "
                "'automatic_race_day_mode', 'source_deletion_enabled')"
            )
        }
        columns = [row[1] for row in connection.execute("PRAGMA table_info(jobs)")]
        result = {
            "recordings": connection.execute("SELECT count(*) FROM recordings").fetchone()[0],
            "jobs": dict(connection.execute("SELECT status, count(*) FROM jobs GROUP BY status ORDER BY status")),
            "interviews": connection.execute("SELECT count(*) FROM interviews").fetchone()[0],
            "groups": connection.execute("SELECT count(*) FROM appearance_groups").fetchone()[0],
            "settings": settings,
            "queued_count": len(queued),
            "queued_min": queued[0] if queued else None,
            "queued_max": queued[-1] if queued else None,
            "queued_sha256": hashlib.sha256(payload.encode()).hexdigest(),
            "job_columns": columns,
        }
        if "released_for_processing" in columns:
            result["eligibility"] = dict(connection.execute(
                "SELECT CASE WHEN released_for_processing THEN 'released' ELSE 'held' END, count(*) "
                "FROM jobs WHERE status = 'QUEUED' GROUP BY released_for_processing"
            ))
        return result
    finally:
        connection.close()


if __name__ == "__main__":
    print(json.dumps(snapshot(), indent=2))
