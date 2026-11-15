from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import PlaybackProxy, Recording
from .operations import get_value
from .paths import artifact_path, resolve_recording_source


def _probe(source: Path, runner=subprocess.run) -> dict:
    result = runner(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(source)],
        text=True, capture_output=True,
    )
    if result.returncode != 0:
        raise ValueError("source codecs could not be inspected")
    return json.loads(result.stdout or "{}")


def proxy_path(settings: Settings, recording: Recording) -> Path:
    return settings.data_dir / "playback_cache" / f"recording_{recording.id}.mp4"


def remove_playback_proxy(session: Session, settings: Settings, recording_id: int) -> int:
    row = session.execute(select(PlaybackProxy).where(PlaybackProxy.recording_id == recording_id)).scalar_one_or_none()
    if row is None:
        return 0
    try:
        path = artifact_path(settings.data_dir, row.relative_path)
        if path.exists():
            path.unlink()
    finally:
        session.delete(row)
        session.flush()
    return row.size_bytes


def cleanup_playback_cache(session: Session, settings: Settings, *, preserve_recording_id: int | None = None) -> dict:
    maximum = int(get_value(session, "playback_cache_max_bytes", settings.playback_cache_max_bytes))
    rows = session.execute(select(PlaybackProxy).order_by(PlaybackProxy.last_accessed_at.asc())).scalars().all()
    total = sum(row.size_bytes for row in rows)
    deleted = 0
    for row in rows:
        if total <= maximum:
            break
        if row.recording_id == preserve_recording_id:
            continue
        total -= remove_playback_proxy(session, settings, row.recording_id)
        deleted += 1
    session.commit()
    return {"max_bytes": maximum, "bytes": total, "deleted": deleted, "count": session.query(PlaybackProxy).count()}


def create_playback_proxy(session: Session, settings: Settings, recording: Recording, *, runner=subprocess.run) -> PlaybackProxy:
    source = resolve_recording_source(settings, recording, verify_size=True)
    existing = session.execute(select(PlaybackProxy).where(PlaybackProxy.recording_id == recording.id)).scalar_one_or_none()
    if existing:
        try:
            path = artifact_path(settings.data_dir, existing.relative_path)
            if not path.is_file():
                raise FileNotFoundError("playback proxy is unavailable")
            existing.last_accessed_at = datetime.utcnow(); session.commit()
            return existing
        except FileNotFoundError:
            session.delete(existing); session.flush()
    minimum_free = float(get_value(session, "playback_cache_min_free_gb", settings.playback_cache_min_free_gb)) * 1024 ** 3
    maximum = int(get_value(session, "playback_cache_max_bytes", settings.playback_cache_max_bytes))
    if recording.size_bytes > maximum:
        raise ValueError("recording is larger than the configured playback cache limit")
    if shutil.disk_usage(settings.data_dir).free - max(recording.size_bytes, 0) < minimum_free:
        raise ValueError("insufficient free space for a safe playback proxy")
    streams = _probe(source, runner).get("streams") or []
    video = next((row for row in streams if row.get("codec_type") == "video"), None)
    audio = next((row for row in streams if row.get("codec_type") == "audio"), None)
    if video is None:
        raise ValueError("recording has no usable video stream")
    remux = video.get("codec_name") == "h264" and (audio is None or audio.get("codec_name") == "aac")
    mode = "remux" if remux else "transcode"
    target = proxy_path(settings, recording); target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".creating.mp4")
    command = ["ffmpeg", "-y", "-i", str(source), "-map", "0:v:0", "-map", "0:a?"]
    if remux:
        command += ["-c", "copy"]
    else:
        command += ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac"]
    command += ["-movflags", "+faststart", str(temporary)]
    result = runner(command, text=True, capture_output=True)
    if result.returncode != 0 or not temporary.is_file():
        temporary.unlink(missing_ok=True)
        raise ValueError("playback proxy generation failed")
    os.replace(temporary, target)
    row = PlaybackProxy(
        recording_id=recording.id,
        relative_path=str(target.relative_to(settings.data_dir)),
        size_bytes=target.stat().st_size,
        mode=mode,
        last_accessed_at=datetime.utcnow(),
    )
    session.add(row); session.commit()
    cleanup_playback_cache(session, settings, preserve_recording_id=recording.id)
    return row


def playback_proxy_view(session: Session, settings: Settings, recording_id: int) -> dict:
    row = session.execute(select(PlaybackProxy).where(PlaybackProxy.recording_id == recording_id)).scalar_one_or_none()
    if row is None:
        return {"state": "not_generated", "recording_id": recording_id}
    try:
        path = artifact_path(settings.data_dir, row.relative_path)
        if not path.is_file():
            raise FileNotFoundError("playback proxy is unavailable")
    except FileNotFoundError:
        return {"state": "missing", "recording_id": recording_id}
    return {"state": "ready", "recording_id": recording_id, "mode": row.mode, "size_bytes": path.stat().st_size,
            "created_at": row.created_at, "last_accessed_at": row.last_accessed_at,
            "url": f"/api/recordings/{recording_id}/playback"}


def playback_cache_status(session: Session, settings: Settings) -> dict:
    rows = session.query(PlaybackProxy).all()
    return {
        "count": len(rows), "bytes": sum(row.size_bytes for row in rows),
        "max_bytes": int(get_value(session, "playback_cache_max_bytes", settings.playback_cache_max_bytes)),
        "free_bytes": shutil.disk_usage(settings.data_dir).free,
    }
