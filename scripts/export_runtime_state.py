from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


ARTIFACT_DIRECTORIES = ("approved", "interviews", "jobs", "logs", "evidence")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def semantic_snapshot(connection: sqlite3.Connection) -> dict:
    queue_identity = connection.execute(
        "SELECT id, recording_id, status FROM jobs WHERE status='QUEUED' ORDER BY id"
    ).fetchall()
    payload = json.dumps(queue_identity, separators=(",", ":"))
    return {
        "recordings": connection.execute("SELECT count(*) FROM recordings").fetchone()[0],
        "jobs_by_status": dict(connection.execute("SELECT status, count(*) FROM jobs GROUP BY status ORDER BY status")),
        "queued_held": connection.execute("SELECT count(*) FROM jobs WHERE status='QUEUED' AND released_for_processing=0").fetchone()[0],
        "queued_released": connection.execute("SELECT count(*) FROM jobs WHERE status='QUEUED' AND released_for_processing=1").fetchone()[0],
        "active": connection.execute("SELECT count(*) FROM jobs WHERE status='PROCESSING'").fetchone()[0],
        "interviews": connection.execute("SELECT count(*) FROM interviews").fetchone()[0],
        "candidates": connection.execute("SELECT count(*) FROM candidates").fetchone()[0],
        "groups": connection.execute("SELECT count(*) FROM appearance_groups").fetchone()[0],
        "queue_sha256": hashlib.sha256(payload.encode()).hexdigest(),
    }


def main() -> int:
    cli = argparse.ArgumentParser(description="Create a consistent SQLite-native Frame Pull runtime export.")
    cli.add_argument("--data-root", required=True, type=Path)
    cli.add_argument("--database", type=Path)
    cli.add_argument("--output", type=Path)
    args = cli.parse_args()
    data_root = args.data_root.resolve()
    database = (args.database or data_root / "frame_pull.db").resolve()
    if not database.is_file() or database.parent != data_root:
        raise SystemExit("database must be an existing direct child of data-root")
    output = (args.output or data_root / "backups" / f"v210-runtime-{datetime.now():%Y%m%d-%H%M%S}").resolve()
    if output.exists():
        raise SystemExit(f"backup target already exists: {output}")
    output.mkdir(parents=True)
    backup_database = output / "frame_pull.db"
    source = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    destination = sqlite3.connect(backup_database)
    try:
        source.backup(destination)
        destination.commit()
        snapshot = semantic_snapshot(destination)
    finally:
        destination.close()
        source.close()

    artifacts = output / "artifacts"
    copied_roots: list[str] = []
    for name in ARTIFACT_DIRECTORIES:
        candidate = data_root / name
        if candidate.is_dir():
            shutil.copytree(candidate, artifacts / name, copy_function=shutil.copy2)
            copied_roots.append(name)
    files = []
    for path in sorted(item for item in output.rglob("*") if item.is_file() and item.name != "export_manifest.json"):
        files.append({"path": path.relative_to(output).as_posix(), "bytes": path.stat().st_size, "sha256": sha256(path)})
    manifest = {
        "schema": "frame-pull-runtime-export-v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "source_database": database.name,
        "database_backup_method": "sqlite3.Connection.backup",
        "copied_artifact_roots": copied_roots,
        "excluded": ["source recordings", "WAL/SHM sidecars", "prior backups", "isolated qualification roots"],
        "semantic_snapshot": snapshot,
        "file_count": len(files),
        "total_bytes": sum(item["bytes"] for item in files),
        "files": files,
    }
    (output / "export_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), **manifest}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
