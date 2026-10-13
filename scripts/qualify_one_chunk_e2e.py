"""Run exactly one real recording through the normal Frame Pull job path.

This is an explicit qualification utility. It creates a new isolated source,
SQLite database, and service data tree; it never starts the worker loop and it
never deletes either the original or copied source.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PIL import Image

from frame_pull_service.config import Settings
from frame_pull_service.db import create_db_engine, init_db, make_session_factory
from frame_pull_service.models import Candidate, Interview, InterviewRevision, Job, JobStatus, Recording, RecordingStatus
from frame_pull_service.services.context import resolve_interview_context
from frame_pull_service.services.discovery import discover_recordings
from frame_pull_service.services.legacy_engine import LegacySubprocessEngine
from frame_pull_service.services.operations import seed_settings, update_settings
from frame_pull_service.services.queue import claim_next_job, queue_recording, release_job
from frame_pull_service.services.worker import Worker


EXPECTED_LEGACY_CONFIG_SHA256 = "84BAE97469F307BA446A6DAF285509C35AF0F263440A3432CE222B1004B5BE56"


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def validate_request(source: Path, base: Settings, qualification_root: Path) -> tuple[Path, Path]:
    source = source.resolve()
    qualification_root = qualification_root.resolve()
    safe_root = (ROOT / "data" / "v26-one-chunk").resolve()
    if not source.is_file() or source.suffix.lower() != ".ts":
        raise ValueError("The explicit qualification input must be one existing .ts file.")
    if _inside(source, base.source_dir):
        raise ValueError("Production source recordings are forbidden for this qualification.")
    if not _inside(qualification_root, safe_root) or qualification_root == safe_root:
        raise ValueError("Qualification root must be a unique child of ignored data/v26-one-chunk.")
    production_db = Path(base.resolved_database_url.removeprefix("sqlite:///")).resolve()
    isolated_db = (qualification_root / "frame_pull_v26.sqlite").resolve()
    if isolated_db == production_db:
        raise ValueError("Production database is forbidden for this qualification.")
    return source, qualification_root


def assert_single_input(source_dir: Path, expected_name: str) -> Path:
    inputs = sorted(source_dir.glob("*.ts"))
    if len(inputs) != 1 or inputs[0].name != expected_name:
        raise RuntimeError("Isolated source must contain exactly the one explicit input.")
    return inputs[0]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _tree_snapshot(root: Path) -> dict:
    files = [path for path in root.rglob("*") if path.is_file()] if root.exists() else []
    latest = max(files, key=lambda item: item.stat().st_mtime_ns, default=None)
    return {
        "files": len(files),
        "directories": sum(1 for path in root.rglob("*") if path.is_dir()) if root.exists() else 0,
        "bytes": sum(path.stat().st_size for path in files),
        "latest_write_ns": latest.stat().st_mtime_ns if latest else None,
        "latest_relative": latest.relative_to(root).as_posix() if latest else None,
    }


def _production_snapshot(settings: Settings) -> dict:
    database = Path(settings.resolved_database_url.removeprefix("sqlite:///")).resolve()
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        decode = lambda value: json.loads(value) if isinstance(value, str) else value
        keys = ("processing_paused", "autoqueue_closed_recordings", "automatic_race_day_mode", "source_deletion_enabled")
        return {
            "recordings": connection.execute("select count(*) from recordings").fetchone()[0],
            "jobs": dict(connection.execute("select lower(status), count(*) from jobs group by lower(status)").fetchall()),
            "interviews": connection.execute("select count(*) from interviews").fetchone()[0],
            "groups": connection.execute("select count(*) from appearance_groups").fetchone()[0],
            "settings": {key: decode(connection.execute("select value from operational_settings where key=?", (key,)).fetchone()[0]) for key in keys},
        }
    finally:
        connection.close()


def _probe(path: Path) -> dict:
    result = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries",
        "format=format_name,duration,size:stream=index,codec_type,codec_name,width,height,r_frame_rate,channels,sample_rate",
        "-of", "json", str(path),
    ], capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise RuntimeError("Selected input failed bounded FFprobe validation.")
    return json.loads(result.stdout)


def _relative(root: Path, value: str | Path | None) -> str | None:
    if value is None:
        return None
    path = Path(value).resolve()
    if not _inside(path, root):
        raise RuntimeError("Qualification artifact escaped the isolated root.")
    return path.relative_to(root).as_posix()


def _clip_probe(path: Path) -> dict:
    result = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,width,height",
        "-of", "json", str(path),
    ], capture_output=True, text=True, timeout=30, check=False)
    return {"returncode": result.returncode, "media": json.loads(result.stdout) if result.returncode == 0 else {}}


def _artifact_report(session, qualification_root: Path) -> tuple[list[dict], int]:
    details: list[dict] = []
    candidate_total = 0
    for interview in session.query(Interview).order_by(Interview.id):
        revision = session.get(InterviewRevision, interview.active_revision_id) if interview.active_revision_id else None
        candidates = session.query(Candidate).filter_by(revision_id=revision.id).order_by(Candidate.rank).all() if revision else []
        candidate_total += len(candidates)
        candidate_details = []
        for candidate in candidates:
            image_path = Path(candidate.image_path)
            readable = False
            try:
                with Image.open(image_path) as image:
                    image.verify()
                readable = True
            except OSError:
                pass
            candidate_details.append({
                "id": candidate.id,
                "rank": candidate.rank,
                "source_timestamp": candidate.source_timestamp,
                "selected": candidate.selected,
                "path": _relative(qualification_root, image_path),
                "readable": readable,
            })
        details.append({
            "id": interview.id,
            "interview_key": interview.interview_key,
            "source_start": interview.source_start,
            "source_end": interview.source_end,
            "review_status": interview.review_status.value,
            "final_name": interview.final_name,
            "identity_source": interview.identity_source,
            "context": resolve_interview_context(session, interview),
            "revision": None if revision is None else {
                "id": revision.id,
                "trusted_name": revision.trusted_name,
                "ocr_name_candidate": revision.ocr_name_candidate,
                "name_trust_reason": revision.name_trust_reason,
                "clip_start": revision.clip_start,
                "clip_end": revision.clip_end,
                "clip": _relative(qualification_root, revision.clip_path),
                "clip_probe": _clip_probe(Path(revision.clip_path)),
                "poster": _relative(qualification_root, revision.poster_path),
                "manifest": _relative(qualification_root, revision.manifest_path),
            },
            "candidates": candidate_details,
        })
    return details, candidate_total


def run(source: Path) -> tuple[int, Path, dict]:
    base = Settings()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    qualification_root = (ROOT / "data" / "v26-one-chunk" / f"run_{stamp}").resolve()
    source, qualification_root = validate_request(source, base, qualification_root)
    report_path = qualification_root / "qualification_report.json"
    report: dict = {
        "kind": "v26_one_chunk_e2e",
        "started_at": datetime.now().astimezone().isoformat(),
        "source_filename": source.name,
        "worker_loop_started": False,
        "source_deletion_performed": False,
    }
    engine = None
    try:
        legacy_config = base.legacy_engine_root / "config.yaml"
        report["legacy_before"] = {
            "config_sha256": _sha256(legacy_config),
            "output": _tree_snapshot(base.legacy_engine_root / "output"),
            "work": _tree_snapshot(base.legacy_engine_root / "work"),
        }
        if report["legacy_before"]["config_sha256"] != EXPECTED_LEGACY_CONFIG_SHA256:
            raise RuntimeError("Frozen legacy config checksum does not match the approved baseline.")
        report["production_before"] = _production_snapshot(base)
        report["source_probe"] = _probe(source)

        isolated_source = qualification_root / "source"
        isolated_data = qualification_root / "service-data"
        isolated_source.mkdir(parents=True, exist_ok=False)
        copied = isolated_source / source.name
        shutil.copy2(source, copied)
        copied = assert_single_input(isolated_source, source.name)
        settings = base.model_copy(update={
            "source_dir": isolated_source,
            "data_dir": isolated_data,
            "database_url": f"sqlite:///{(qualification_root / 'frame_pull_v26.sqlite').as_posix()}",
            "queue_existing_on_first_run": True,
            "auto_queue": False,
            "stable_for_seconds": 0,
            "min_input_bytes": 1,
        })
        settings.ensure_data_dirs()
        engine = create_db_engine(settings)
        init_db(engine)
        sessions = make_session_factory(engine)
        with sessions() as session:
            seed_settings(session, settings)
            update_settings(session, {
                "processing_paused": True,
                "autoqueue_closed_recordings": False,
                "automatic_race_day_mode": False,
                "source_deletion_enabled": False,
                "max_concurrent_recordings": 1,
            })
            discovery = discover_recordings(session, settings, datetime.now() + timedelta(seconds=2))
            records = session.query(Recording).all()
            if len(records) != 1 or records[0].filename != source.name or records[0].status != RecordingStatus.READY:
                raise RuntimeError("Isolated discovery did not produce exactly one READY recording.")
            recording = records[0]
            job = queue_recording(session, recording)
            session.commit()
            if session.query(Job).count() != 1:
                raise RuntimeError("Isolated qualification created more than one job.")
            paused_claim = claim_next_job(session, max_concurrent=1)
            if paused_claim is not None:
                raise RuntimeError("Paused isolated processing unexpectedly claimed a job.")
            report["discovery"] = discovery
            report["recording"] = {"id": recording.id, "filename": recording.filename, "status_before_queue": "ready", "size_bytes": recording.size_bytes}
            report["job"] = {"id": job.id, "recording_id": recording.id, "status_before_run": job.status.value, "paused_claim_is_none": True}
            update_settings(session, {"processing_paused": False})
            release_job(session, job.id)
            claimed = claim_next_job(session, max_concurrent=1)
            if claimed is None or claimed.id != job.id:
                raise RuntimeError("Failed to claim the one explicit isolated job.")
            job_id = claimed.id

        expected_job_dir = settings.data_dir / "jobs" / f"job_{job_id:06d}"
        expected_config = expected_job_dir / "legacy_job_config.yaml"
        command = LegacySubprocessEngine(settings)._command(expected_config, copied)
        report["legacy_invocation"] = {
            "shape": [Path(command[0]).name, "-u", "profile_extractor.py", "--config", "<isolated-job-config>", "--input", f"<isolated-source>/{copied.name}"],
            "input_flag_count": command.count("--input"),
            "contains_all": "--all" in command,
            "contains_rerun": "--rerun" in command,
        }
        started = datetime.now().astimezone()
        started_perf = time.monotonic()
        Worker(sessions, settings)._process(job_id)
        elapsed = time.monotonic() - started_perf
        finished = datetime.now().astimezone()

        with sessions() as session:
            job = session.get(Job, job_id)
            recording = session.get(Recording, job.recording_id)
            interviews, candidate_total = _artifact_report(session, qualification_root)
            update_settings(session, {"processing_paused": True})
            report["processing"] = {
                "started_at": started.isoformat(),
                "finished_at": finished.isoformat(),
                "elapsed_seconds": round(elapsed, 3),
                "detector_exit_code": 0 if job.status == JobStatus.COMPLETE else None,
                "job_status": job.status.value,
                "recording_status": recording.status.value,
                "job_error": job.error_summary,
                "interview_count": len(interviews),
                "candidate_count": candidate_total,
            }
            report["interviews"] = interviews
            report["copied_source_relative"] = _relative(qualification_root, copied)
            report["copied_source_preserved"] = copied.exists()
            report["job_dir_relative"] = _relative(qualification_root, expected_job_dir)
            report["isolated_counts"] = {
                "recordings": session.query(Recording).count(),
                "jobs": session.query(Job).count(),
                "interviews": session.query(Interview).count(),
                "candidates": session.query(Candidate).count(),
            }

        report["legacy_after"] = {
            "config_sha256": _sha256(legacy_config),
            "output": _tree_snapshot(base.legacy_engine_root / "output"),
            "work": _tree_snapshot(base.legacy_engine_root / "work"),
        }
        report["production_after"] = _production_snapshot(base)
        report["original_source_preserved"] = source.exists()
        legacy_unchanged = report["legacy_before"] == report["legacy_after"]
        production_unchanged = report["production_before"] == report["production_after"]
        report["safety"] = {"legacy_unchanged": legacy_unchanged, "production_unchanged": production_unchanged}
        if not legacy_unchanged or not production_unchanged:
            raise RuntimeError("Post-run isolation audit failed.")
        if report["processing"]["job_status"] != "complete":
            raise RuntimeError("The one isolated processing job failed.")
        report["result"] = "passed"
        return 0, report_path, report
    except Exception as exc:
        report["result"] = "failed"
        report["error"] = str(exc)
        return 1, report_path, report
    finally:
        report["finished_at"] = datetime.now().astimezone().isoformat()
        qualification_root.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
        if engine is not None:
            engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one explicit real chunk through an isolated Frame Pull job.")
    parser.add_argument("--confirm-detector-run", action="store_true", help="Required acknowledgement before invoking the frozen detector once.")
    parser.add_argument("--input", type=Path, required=True, help="One explicit non-production MPEG-TS input.")
    args = parser.parse_args()
    if not args.confirm_detector_run:
        parser.error("--confirm-detector-run is required.")
    code, report_path, report = run(args.input)
    print(json.dumps({
        "result": report.get("result"),
        "qualification_root": str(report_path.parent),
        "report": str(report_path),
        "job_status": report.get("processing", {}).get("job_status"),
        "interview_count": report.get("processing", {}).get("interview_count"),
        "candidate_count": report.get("processing", {}).get("candidate_count"),
        "error": report.get("error"),
    }, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
