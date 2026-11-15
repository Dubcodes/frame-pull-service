from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Job, JobStatus, Recording, RecordingMediaAudit, RecordingSession, RecordingStatus
from .paths import resolve_recording_source


Runner = Callable[..., subprocess.CompletedProcess[str]]
ERROR_PATTERN = re.compile(
    r"corrupt|invalid data|invalid nal|decode error|error while decoding|packet corrupt|"
    r"non-monoton|continuity|timestamp discontinu|missing picture|concealing|truncat",
    re.I,
)


def latest_media_audit(session: Session, recording_id: int) -> RecordingMediaAudit | None:
    return session.execute(
        select(RecordingMediaAudit)
        .where(RecordingMediaAudit.recording_id == recording_id)
        .order_by(RecordingMediaAudit.id.desc())
    ).scalars().first()


def audit_is_allowed(session: Session, recording: Recording) -> tuple[bool, str]:
    if not recording.is_closed or not recording.stable_at:
        return False, "recording is active or not stable"
    if recording.status in {RecordingStatus.DISCOVERED, RecordingStatus.WAITING, RecordingStatus.PROCESSING}:
        return False, "recording is not closed and ready"
    if recording.status == RecordingStatus.IGNORED:
        return False, "recording is ignored"
    recorder_busy = session.execute(select(RecordingSession.id).where(
        RecordingSession.status.in_(["recording", "stopping", "finalizing"])
    )).first()
    if recorder_busy:
        return False, "recorder is active"
    processing = session.execute(select(Job.id).where(Job.status == JobStatus.PROCESSING)).first()
    if processing:
        return False, "detector is active"
    return True, "eligible"


def _version(executable: str, runner: Runner) -> str:
    try:
        result = runner([executable, "-version"], text=True, capture_output=True, timeout=15)
        return (result.stdout or result.stderr or executable).splitlines()[0][:200]
    except Exception:
        return f"{executable} version unavailable"


def _probe(ffprobe: str, source: Path, runner: Runner) -> tuple[bool, dict, str]:
    command = [
        ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(source),
    ]
    try:
        result = runner(command, text=True, capture_output=True, timeout=120)
        data = json.loads(result.stdout or "{}") if result.returncode == 0 else {}
        return result.returncode == 0, data, (result.stderr or "")
    except Exception as exc:
        return False, {}, str(exc)


def _scan(ffmpeg: str, source: Path, runner: Runner) -> tuple[int, str, float | None, float]:
    command = [
        ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-v", "warning", "-progress", "pipe:1", "-i", str(source),
        "-map", "0:v:0?", "-map", "0:a?", "-f", "null", "-",
    ]
    started = time.monotonic()
    try:
        result = runner(command, text=True, capture_output=True)
        traversed = None
        matches = re.findall(r"out_time_(?:ms|us)=(\d+)", result.stdout or "")
        if matches:
            traversed = int(matches[-1]) / 1_000_000
        return result.returncode, result.stderr or "", traversed, time.monotonic() - started
    except Exception as exc:
        return 127, str(exc), None, time.monotonic() - started


def audit_recording(
    session: Session,
    settings: Settings,
    recording: Recording,
    *,
    runner: Runner = subprocess.run,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> RecordingMediaAudit:
    allowed, reason = audit_is_allowed(session, recording)
    if not allowed:
        raise ValueError(reason)
    source = resolve_recording_source(settings, recording, verify_size=True)
    probe_ok, probe, probe_error = _probe(ffprobe, source, runner)
    streams = probe.get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    format_row = probe.get("format") or {}
    try:
        duration = float(format_row.get("duration"))
    except (TypeError, ValueError):
        duration = None

    scan_exit, scan_log, traversed, elapsed = _scan(ffmpeg, source, runner) if probe_ok and video else (1, probe_error, None, 0.0)
    sanitized = (probe_error + "\n" + scan_log).replace(str(source), recording.filename)
    diagnostic_lines = [line.strip() for line in sanitized.splitlines() if ERROR_PATTERN.search(line)]
    error_count = len(diagnostic_lines)
    if not probe_ok or not video or not duration or duration <= 0 or scan_exit != 0:
        classification = "failed"
    elif error_count or not audio:
        classification = "warning"
    else:
        classification = "healthy"
    if classification == "healthy":
        summary = "Full source scan completed without media errors."
    elif classification == "warning":
        summary = f"Source is recoverable with {error_count} media warning(s){' and no audio stream' if not audio else ''}."
    else:
        summary = "Source could not be fully validated; retain it and investigate before deletion."

    audit = RecordingMediaAudit(
        recording_id=recording.id,
        checked_at=datetime.utcnow(),
        tool_version=f"{_version(ffprobe, runner)} | {_version(ffmpeg, runner)}",
        probe_ok=probe_ok,
        full_scan_ok=scan_exit == 0,
        classification=classification,
        duration_seconds=duration,
        traversed_seconds=traversed,
        has_video=video is not None,
        has_audio=audio is not None,
        error_count=error_count,
        summary=summary,
        evidence={
            "format": format_row.get("format_name"),
            "start_time": format_row.get("start_time"),
            "stream_count": len(streams),
            "video": None if video is None else {
                "codec": video.get("codec_name"), "width": video.get("width"), "height": video.get("height"),
                "frame_rate": video.get("avg_frame_rate") or video.get("r_frame_rate"),
            },
            "audio": None if audio is None else {"codec": audio.get("codec_name")},
            "scan_exit_code": scan_exit,
            "scan_elapsed_seconds": round(elapsed, 3),
            "diagnostics": diagnostic_lines[:200],
        },
    )
    session.add(audit)
    if duration and not recording.duration_seconds:
        recording.duration_seconds = duration
        recording.metadata_probed_at = datetime.utcnow()
    if classification in {"warning", "failed"}:
        recording.source_protected = True
    session.commit()
    return audit


def next_integrity_candidate(session: Session, *, min_recording_id: int = 0) -> Recording | None:
    audited = select(RecordingMediaAudit.recording_id)
    return session.execute(
        select(Recording)
        .where(
            Recording.is_closed.is_(True),
            Recording.stable_at.is_not(None),
            Recording.historical.is_(False),
            Recording.id >= max(0, int(min_recording_id)),
            Recording.status.not_in([RecordingStatus.DISCOVERED, RecordingStatus.WAITING, RecordingStatus.PROCESSING, RecordingStatus.IGNORED]),
            Recording.id.not_in(audited),
        )
        .order_by(Recording.id)
    ).scalars().first()


def media_audit_view(audit: RecordingMediaAudit | None) -> dict:
    if audit is None:
        return {"classification": "not_checked", "summary": "Not checked", "checked_at": None}
    return {
        "id": audit.id,
        "classification": audit.classification,
        "summary": audit.summary,
        "checked_at": audit.checked_at,
        "duration_seconds": audit.duration_seconds,
        "traversed_seconds": audit.traversed_seconds,
        "has_video": audit.has_video,
        "has_audio": audit.has_audio,
        "error_count": audit.error_count,
        "evidence": audit.evidence,
    }
