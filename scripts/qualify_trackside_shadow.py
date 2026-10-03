"""Explicit, isolated qualification for a configured Trackside HLS recorder.

This utility never starts a worker or the legacy detector. It requires an
operator flag because it connects to the locally configured live input.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from frame_pull_service.config import Settings
from frame_pull_service.db import create_db_engine, init_db, make_session_factory
from frame_pull_service.models import Recording, RecordingStatus
from frame_pull_service.services.discovery import discover_recordings
from frame_pull_service.services.operations import seed_settings, update_settings
from frame_pull_service.services.queue import claim_next_job
from frame_pull_service.services.recorder import FFmpegRecorder


def _redact_text(value: str, settings: Settings) -> str:
    for secret in (settings.recorder_input, settings.recorder_origin, settings.recorder_referer):
        if secret:
            value = value.replace(secret, "<redacted>")
    return value


def _write_report(path: Path, report: dict) -> None:
    path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _probe_command(settings: Settings) -> list[str]:
    recorder = FFmpegRecorder(settings)
    return [
        "ffprobe", "-v", "error", "-headers", recorder._trackside_headers(), "-show_programs",
        "-show_streams", "-of", "json", settings.recorder_input,
    ]


def _probe_live(settings: Settings) -> dict:
    try:
        result = subprocess.run(_probe_command(settings), capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("FFprobe could not complete the bounded Trackside input probe.") from exc
    if result.returncode:
        raise RuntimeError("FFprobe rejected the configured Trackside input or program layout.")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("FFprobe did not return a readable stream description.") from exc
    program = next((item for item in payload.get("programs", []) if str(item.get("program_id")) == str(settings.recorder_program)), None)
    if program is None:
        raise RuntimeError("Configured Trackside program is not present; no alternative program was selected.")
    streams = program.get("streams", [])
    video = [item for item in streams if item.get("codec_type") == "video"]
    audio = [item for item in streams if item.get("codec_type") == "audio"]
    if not video or not audio:
        raise RuntimeError("Configured Trackside program does not contain both first video and first audio streams.")
    def summary(item: dict) -> dict:
        return {key: item.get(key) for key in ("index", "codec_type", "codec_name", "width", "height", "r_frame_rate", "channels", "sample_rate") if item.get(key) is not None}
    return {"program_id": program.get("program_id"), "video": summary(video[0]), "audio": summary(audio[0])}


def _probe_chunk(path: Path) -> dict:
    result = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate,channels,sample_rate",
        "-of", "json", str(path),
    ], capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        return {"filename": path.name, "size_bytes": path.stat().st_size, "probe": "failed"}
    payload = json.loads(result.stdout)
    streams = payload.get("streams", [])
    return {
        "filename": path.name,
        "size_bytes": path.stat().st_size,
        "duration_seconds": float(payload.get("format", {}).get("duration", 0) or 0),
        "video": next(({key: item.get(key) for key in ("codec_name", "width", "height", "r_frame_rate")} for item in streams if item.get("codec_type") == "video"), None),
        "audio": next(({key: item.get(key) for key in ("codec_name", "channels", "sample_rate")} for item in streams if item.get("codec_type") == "audio"), None),
    }


def _wait_for(recorder: FFmpegRecorder, sessions, predicate, timeout: float) -> object:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with sessions() as session:
            state = recorder.refresh(session)
        if state.state == "failed":
            raise RuntimeError(state.error or "FFmpeg exited before the required recorder state was reached.")
        if predicate(state):
            return state
        time.sleep(2)
    raise RuntimeError("Recorder did not reach the expected controlled-shadow state in time.")


def _baseline_marker(session) -> None:
    session.add(Recording(
        filename="shadow_baseline.ts", source_path="shadow_baseline", fingerprint="shadow-baseline",
        size_bytes=1, mtime_epoch=0, historical=True, is_closed=True,
        status=RecordingStatus.HISTORICAL,
    ))
    session.commit()


def _wall_clock(filename: str, duration_seconds: float, timezone: str) -> dict | None:
    match = re.fullmatch(r"trackside_(\d{8})-(\d{4})_(\d{2})\.ts", filename)
    if not match:
        return None
    started = datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S").replace(tzinfo=ZoneInfo(timezone))
    offset = max(1, min(15, int(duration_seconds // 2)))
    return {"filename_start": started.isoformat(), "example_source_offset_seconds": offset,
            "example_broadcast_time": (started + timedelta(seconds=offset)).isoformat()}


def run(args: argparse.Namespace) -> int:
    base = Settings()
    if not base.recorder_input:
        raise RuntimeError("No local recorder input is configured.")
    if base.recorder_profile.strip().lower() != "trackside_hls":
        raise RuntimeError("This controlled utility requires FRAME_PULL_RECORDER_PROFILE=trackside_hls.")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = (ROOT / "data" / "shadow-recording" / f"run_{stamp}").resolve()
    chunks = root / "chunks"
    data_dir = root / "service-data"
    chunks.mkdir(parents=True, exist_ok=False)
    settings = base.model_copy(update={
        "source_dir": chunks,
        "recorder_output_dir": chunks,
        "data_dir": data_dir,
        "database_url": f"sqlite:///{(root / 'shadow.sqlite').as_posix()}",
        "recorder_enabled": True,
        "recorder_chunk_minutes": 1.0,
        "min_input_bytes": 1,
        "stable_for_seconds": 0,
        "auto_queue": False,
    })
    report_path = root / "qualification_report.json"
    report = {
        "kind": "trackside_hls_controlled_shadow",
        "started_at": datetime.now().isoformat(),
        "input_configured": True,
        "input_redacted": True,
        "profile": settings.recorder_profile,
        "segment_seconds": 60,
        "worker_started": False,
        "detector_started": False,
    }
    engine = create_db_engine(settings)
    init_db(engine)
    sessions = make_session_factory(engine)
    recorder = FFmpegRecorder(settings)
    try:
        report["live_ffprobe"] = _probe_live(settings)
        with sessions() as session:
            seed_settings(session, settings)
            update_settings(session, {
                "recorder_enabled": True,
                "processing_paused": True,
                "autoqueue_closed_recordings": False,
                "automatic_race_day_mode": False,
                "source_deletion_enabled": False,
            })
            _baseline_marker(session)
            first = recorder.start(session)
            report["first_start"] = first.public()
        active = _wait_for(recorder, sessions, lambda state: state.active_path is not None, 45)
        report["active_chunk"] = active.public()
        with sessions() as session:
            discovery = discover_recordings(session, settings, datetime.now() + timedelta(seconds=2))
            active_seen = session.query(Recording).filter_by(filename=active.active_path.name).one_or_none() is not None
        report["active_chunk_protection"] = {"discovery": discovery, "active_record_created": active_seen}
        initial_name = active.active_path.name
        rolled = _wait_for(recorder, sessions, lambda state: state.active_path is not None and state.active_path.name != initial_name, 100)
        report["first_rollover"] = rolled.public()
        with sessions() as session:
            discovery = discover_recordings(session, settings, datetime.now() + timedelta(seconds=2))
            closed = session.query(Recording).filter_by(filename=initial_name).one_or_none()
            report["closed_chunk_discovery"] = {"discovery": discovery, "status": closed.status.value if closed else None}
            stopped = recorder.stop_after_current_chunk(session)
            report["stop_after_requested"] = stopped.public()
        _wait_for(recorder, sessions, lambda state: not state.running, 100)
        with sessions() as session:
            report["stop_after_final"] = recorder.status(session).public()
        with sessions() as session:
            second = recorder.start(session)
            report["second_start"] = second.public()
        second_active = _wait_for(recorder, sessions, lambda state: state.active_path is not None, 45)
        time.sleep(max(5, args.stop_now_seconds))
        with sessions() as session:
            stopping = recorder.stop(session)
            report["stop_now_requested"] = stopping.public()
        _wait_for(recorder, sessions, lambda state: not state.running, 45)
        with sessions() as session:
            report["stop_now_final"] = recorder.status(session).public()
        chunk_reports = [_probe_chunk(path) for path in sorted(chunks.glob("trackside_*.ts"))]
        report["chunks"] = chunk_reports
        report["wall_clock"] = next((_wall_clock(item["filename"], item.get("duration_seconds", 0), settings.broadcast_timezone) for item in chunk_reports if item.get("duration_seconds")), None)
        report["production_size_gate_bytes"] = base.min_input_bytes
        report["trailing_chunks_below_production_size_gate"] = [item["filename"] for item in chunk_reports if item["size_bytes"] < base.min_input_bytes]
        handoff_db = root / "handoff.sqlite"
        handoff = settings.model_copy(update={"database_url": f"sqlite:///{handoff_db.as_posix()}"})
        handoff_engine = create_db_engine(handoff)
        init_db(handoff_engine)
        handoff_sessions = make_session_factory(handoff_engine)
        with handoff_sessions() as session:
            seed_settings(session, handoff)
            update_settings(session, {"processing_paused": True, "autoqueue_closed_recordings": False})
            _baseline_marker(session)
            first_closed = next(path for path in sorted(chunks.glob("trackside_*.ts")) if path.name != second_active.active_path.name)
            ready = discover_recordings(session, handoff, datetime.now() + timedelta(seconds=2))
            record = session.query(Recording).filter_by(filename=first_closed.name).one()
            ready_status = record.status.value
            update_settings(session, {"autoqueue_closed_recordings": True})
            record.status, record.stable_at = RecordingStatus.WAITING, None
            session.commit()
            queued = discover_recordings(session, handoff, datetime.now() + timedelta(seconds=2))
            record = session.query(Recording).filter_by(filename=first_closed.name).one()
            report["isolated_handoff"] = {
                "ready_discovery": ready, "ready_status": ready_status, "queued_discovery": queued,
                "queued_status": record.status.value,
                "paused_claim_is_none": claim_next_job(session, paused=True) is None,
            }
        handoff_engine.dispose()
        report["result"] = "passed"
        return 0
    except Exception as exc:
        report["result"] = "failed"
        report["error"] = _redact_text(str(exc), settings)
        return 1
    finally:
        try:
            with sessions() as session:
                state = recorder.stop(session)
                if state.running:
                    _wait_for(recorder, sessions, lambda item: not item.running, 30)
        except Exception:
            report["cleanup"] = "recorder stop required operator verification"
        report["finished_at"] = datetime.now().isoformat()
        _write_report(report_path, report)
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run an isolated Trackside HLS recorder qualification.")
    parser.add_argument("--confirm-live-input", action="store_true", help="Required acknowledgement before contacting the configured input.")
    parser.add_argument("--probe-only", action="store_true", help="Only run the bounded, sanitized FFprobe validation.")
    parser.add_argument("--stop-now-seconds", type=int, default=12, help="Length of the second graceful-stop sample.")
    args = parser.parse_args()
    if not args.confirm_live_input:
        parser.error("--confirm-live-input is required; this utility contacts the configured live input.")
    if args.probe_only:
        settings = Settings()
        try:
            print(json.dumps({"input_configured": bool(settings.recorder_input), "live_ffprobe": _probe_live(settings)}, sort_keys=True))
        except RuntimeError as exc:
            print(json.dumps({"input_configured": bool(settings.recorder_input), "error": _redact_text(str(exc), settings)}, sort_keys=True))
            return 1
        return 0
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
