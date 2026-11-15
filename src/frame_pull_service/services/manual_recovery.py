from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Candidate, Interview, InterviewRevision, ProcessingRun, Recording, ReviewEvent
from .manifests import write_manifest
from .media import create_clip, extract_frame
from .normalizer import _manifest
from .paths import resolve_recording_source


def _manual_key(recording: Recording, start: float, end: float) -> str:
    value = f"manual|{recording.filename}|{start:.3f}|{end:.3f}|{uuid4().hex}".encode()
    return "manual_int_" + hashlib.sha256(value).hexdigest()[:20]


def validate_manual_interval(settings: Settings, recording: Recording, start: float, end: float) -> None:
    if not recording.is_closed:
        raise ValueError("manual recovery requires a closed recording")
    if start < 0 or end <= start:
        raise ValueError("interview start must be before interview end")
    if end - start > settings.manual_interview_max_seconds:
        raise ValueError("manual interview interval exceeds the configured maximum")
    if recording.duration_seconds is not None and end > recording.duration_seconds + 0.25:
        raise ValueError("manual interview interval is outside the recording")


def create_manual_interview(
    session: Session,
    settings: Settings,
    recording: Recording,
    *,
    start: float,
    end: float,
    final_name: str | None = None,
    final_role: str | None = None,
) -> Interview:
    validate_manual_interval(settings, recording, start, end)
    source = resolve_recording_source(settings, recording, verify_size=True)
    key = _manual_key(recording, start, end)
    package = settings.data_dir / "interviews" / key / "revision_001"
    candidates_dir = package / "candidates"; candidates_dir.mkdir(parents=True, exist_ok=True)
    clip_path, poster_path, manifest_path = package / "clip.mp4", package / "poster.jpg", package / "manifest.json"
    create_clip("ffmpeg", source, clip_path, start, end)
    sample_times = [start + (end - start) * fraction for fraction in (0.25, 0.5, 0.75)]
    for rank, source_time in enumerate(sample_times, 1):
        extract_frame("ffmpeg", source, candidates_dir / f"candidate_{rank:03d}.jpg", source_time)
    extract_frame("ffmpeg", source, poster_path, sample_times[1])

    run = ProcessingRun(
        recording_id=recording.id,
        run_key=f"manual-{uuid4().hex}",
        created_at=datetime.utcnow(), completed_at=datetime.utcnow(), status="manual_recovery",
        job_dir=str(package),
        summary={"source": "manual_operator", "start": start, "end": end},
    )
    session.add(run); session.flush()
    interview = Interview(
        interview_key=key, recording_id=recording.id, source_start=start, source_end=end,
        bounds_basis="manual_operator", final_name=(final_name or "").strip() or None,
        final_role=(final_role or "").strip() or None,
        identity_source="manual" if final_name else "unknown",
    )
    session.add(interview); session.flush()
    revision = InterviewRevision(
        interview_id=interview.id, processing_run_id=run.id, revision_number=1,
        name_trust_reason="identity unresolved; manually recovered interval",
        name_consensus_evidence={"manual_recovery": True},
        clip_start=start, clip_end=end, clip_path=str(clip_path), poster_path=str(poster_path), manifest_path=str(manifest_path),
    )
    session.add(revision); session.flush()
    candidates: list[Candidate] = []
    for rank, source_time in enumerate(sample_times, 1):
        row = Candidate(
            revision_id=revision.id, rank=rank, source_timestamp=source_time,
            image_path=str(candidates_dir / f"candidate_{rank:03d}.jpg"), source="manual_recovery",
            selected=rank == 1,
        )
        session.add(row); session.flush(); candidates.append(row)
    interview.active_revision_id = revision.id
    interview.selected_candidate_id = candidates[0].id
    session.add(ReviewEvent(
        interview_id=interview.id, event_type="manual_recovery_created",
        payload={"recording_id": recording.id, "start": start, "end": end, "bounds_basis": "manual_operator"},
    ))
    session.flush()
    revision.manifest = _manifest(interview, revision, candidates, recording, settings.data_dir)
    revision.manifest["recovery"] = {"manual": True, "created_at": datetime.utcnow().isoformat() + "Z"}
    write_manifest(Path(revision.manifest_path), revision.manifest)
    session.commit()
    return interview
