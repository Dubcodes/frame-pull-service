from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Candidate, Interview, InterviewRevision, Recording, ReviewEvent, ReviewStatus
from .media import extract_frame


def refresh_manifest(session: Session, interview: Interview) -> None:
    revision = active_revision(session, interview)
    manifest = dict(revision.manifest or {})
    manifest["identity"] = {**manifest.get("identity", {}), "final_name": interview.final_name, "identity_source": interview.identity_source}
    manifest["role"] = {**manifest.get("role", {}), "final": interview.final_role}
    manifest["track"] = {**manifest.get("track", {}), "final": interview.final_track}
    manifest["review"] = {"status": interview.review_status.value, "export_state": interview.export_state}
    manifest["candidates"] = [{"id": c.id, "rank": c.rank, "source_timestamp": c.source_timestamp, "source": c.source,
                                "selected": c.selected, "url": f"/api/interviews/{interview.id}/candidates/{c.id}"} for c in revision.candidates]
    revision.manifest = manifest
    Path(revision.manifest_path).write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def active_revision(session: Session, interview: Interview) -> InterviewRevision:
    revision = session.get(InterviewRevision, interview.active_revision_id)
    if revision is None:
        raise ValueError("interview has no active revision")
    return revision


def event(session: Session, interview: Interview, event_type: str, payload: dict) -> None:
    session.add(ReviewEvent(interview_id=interview.id, event_type=event_type, payload=payload))


def save_review(session: Session, interview: Interview, values: dict) -> None:
    previous = {key: getattr(interview, key) for key in ("final_name", "final_role", "final_track")}
    for key, value in values.items():
        if value is not None:
            setattr(interview, key, value.strip() or None)
    if values.get("final_name") is not None:
        interview.identity_source = "manual"
    event(session, interview, "review_saved", {"before": previous, "after": values})
    refresh_manifest(session, interview)


def select_candidate(session: Session, interview: Interview, candidate: Candidate) -> None:
    if candidate.revision_id != interview.active_revision_id:
        raise ValueError("candidate is not in the active revision")
    for item in session.query(Candidate).filter(Candidate.revision_id == candidate.revision_id):
        item.selected = item.id == candidate.id
    interview.selected_candidate_id = candidate.id
    event(session, interview, "candidate_selected", {"candidate_id": candidate.id, "source": candidate.source})
    refresh_manifest(session, interview)


def capture_frame(session: Session, settings: Settings, interview: Interview, clip_time: float) -> Candidate:
    revision = active_revision(session, interview)
    if clip_time > revision.clip_end - revision.clip_start + 0.25:
        raise ValueError("clip timestamp is outside this evidence clip")
    source_time = revision.clip_start + clip_time
    source = Path(session.get(Recording, interview.recording_id).source_path)
    rank = (max((candidate.rank for candidate in revision.candidates), default=0) + 1)
    target = Path(revision.manifest_path).parent / "candidates" / f"manual_{rank:03d}.jpg"
    extract_frame("ffmpeg", source, target, source_time)
    candidate = Candidate(revision_id=revision.id, rank=rank, source_timestamp=source_time, image_path=str(target), source="manual_clip_capture", selected=True)
    session.add(candidate); session.flush(); select_candidate(session, interview, candidate)
    event(session, interview, "frame_captured", {"clip_time": clip_time, "source_time": source_time, "candidate_id": candidate.id})
    return candidate


def approve(session: Session, settings: Settings, interview: Interview) -> Path:
    if not interview.selected_candidate_id:
        raise ValueError("approval requires a selected portrait")
    if not interview.final_name:
        raise ValueError("approval requires a final name")
    candidate = session.get(Candidate, interview.selected_candidate_id)
    destination = settings.data_dir / "approved" / f"{interview.interview_key}.jpg"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(candidate.image_path, destination)
    interview.review_status, interview.approved_at, interview.export_state = ReviewStatus.APPROVED, datetime.utcnow(), "pending"
    event(session, interview, "approved", {"candidate_id": candidate.id, "portrait": destination.name})
    refresh_manifest(session, interview)
    return destination


def reject(session: Session, interview: Interview, reason: str) -> None:
    interview.review_status, interview.rejection_reason = ReviewStatus.REJECTED, reason
    event(session, interview, "rejected", {"reason": reason})
    refresh_manifest(session, interview)
