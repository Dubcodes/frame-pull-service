from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from PIL import Image
from PIL.ExifTags import Base
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Candidate, Interview, InterviewRevision, ProcessingRun, Recording, ReviewEvent
from .legacy_engine import LegacyResult
from .media import clip_bounds, create_clip, extract_frame, ffprobe_duration
from .paths import relative_artifact


def interview_key(recording: str, start: float, end: float) -> str:
    material = f"{recording}|{round(start * 1000)}|{round(end * 1000)}".encode("utf-8")
    return "int_" + hashlib.sha256(material).hexdigest()[:24]


def read_jpg_metadata(path: Path) -> dict:
    with Image.open(path) as image:
        value = image.getexif().get(Base.UserComment, b"")
    if isinstance(value, bytes):
        value = value[8:].decode("ascii", "replace") if value.startswith(b"ASCII") else value.decode("utf-8", "replace")
    try:
        return json.loads(value) if value else {}
    except json.JSONDecodeError:
        return {}


def _legacy_candidates(result: LegacyResult, segment_index: int) -> list[tuple[Path, dict]]:
    found: list[tuple[Path, dict]] = []
    for folder in ("best_selected", "selected", "needs_name_review"):
        for image in (result.output_dir / folder).glob("*.jpg"):
            metadata = read_jpg_metadata(image)
            identifier = str(metadata.get("interview_identifier", ""))
            if identifier.endswith(f"segment:{segment_index:03d}"):
                found.append((image, metadata))
    return sorted(found, key=lambda item: (int(item[1].get("rank", 999)), item[1].get("source_timestamp", 0)))


def _manifest(interview: Interview, revision: InterviewRevision, candidates: list[Candidate], recording: Recording, data_root: Path) -> dict:
    return {
        "schema_version": 1, "interview_id": interview.interview_key,
        "source": {"recording": recording.filename, "detected_start": interview.source_start,
                   "detected_end": interview.source_end, "bounds_basis": interview.bounds_basis},
        "clip": {"start": revision.clip_start, "end": revision.clip_end, "duration": round(revision.clip_end - revision.clip_start, 3),
                 "url": f"/api/interviews/{interview.id}/clip"},
        "identity": {"trusted_name": revision.trusted_name, "ocr_candidate": revision.ocr_name_candidate,
                     "ocr_confidence": revision.ocr_confidence, "trust_reason": revision.name_trust_reason,
                     "final_name": interview.final_name, "identity_source": interview.identity_source},
        "role": {"raw": revision.role_raw, "clean": revision.role_clean, "final": interview.final_role},
        "track": {"value": revision.track_value, "confidence": revision.track_confidence, "final": interview.final_track},
        "review": {"status": interview.review_status.value, "export_state": interview.export_state},
        "artifacts": {"poster": relative_artifact(data_root, Path(revision.poster_path))},
        "candidates": [{"id": item.id, "rank": item.rank, "source_timestamp": item.source_timestamp,
                         "source": item.source, "selected": item.selected,
                         "url": f"/api/interviews/{interview.id}/candidates/{item.id}"} for item in candidates],
    }


def normalize_run(session: Session, settings: Settings, recording: Recording, processing_run: ProcessingRun, result: LegacyResult) -> list[Interview]:
    source = Path(recording.source_path)
    duration = ffprobe_duration("ffprobe", source)
    interviews: list[Interview] = []
    for segment in result.segments.get("segments", []):
        index = int(segment["index"])
        start, end = float(segment.get("start", segment["detected_start"])), float(segment.get("end", segment["detected_end"]))
        key = interview_key(recording.filename, start, end)
        interview = session.execute(select(Interview).where(Interview.interview_key == key)).scalar_one_or_none()
        if interview is None:
            interview = Interview(interview_key=key, recording_id=recording.id, source_start=start, source_end=end, bounds_basis="authenticated_segment")
            session.add(interview); session.flush()
        previous = session.execute(select(InterviewRevision).where(InterviewRevision.interview_id == interview.id).order_by(InterviewRevision.revision_number.desc())).scalars().first()
        revision_no = 1 if previous is None else previous.revision_number + 1
        package = settings.data_dir / "interviews" / key / f"revision_{revision_no:03d}"
        candidates_dir = package / "candidates"; candidates_dir.mkdir(parents=True, exist_ok=True)
        clip_start, clip_end = clip_bounds(start, end, duration, settings.clip_padding_seconds)
        clip_path, poster_path, manifest_path = package / "clip.mp4", package / "poster.jpg", package / "manifest.json"
        create_clip("ffmpeg", source, clip_path, clip_start, clip_end)
        legacy_candidates = _legacy_candidates(result, index)
        if legacy_candidates:
            shutil.copy2(legacy_candidates[0][0], poster_path)
        else:
            extract_frame("ffmpeg", source, poster_path, min(max(start, 0.0), duration))
        revision = InterviewRevision(interview_id=interview.id, processing_run_id=processing_run.id, revision_number=revision_no,
            trusted_name=segment.get("name_clean") or None, ocr_name_candidate=segment.get("ocr_name_candidate") or segment.get("name_raw") or None,
            ocr_confidence=segment.get("name_confidence"), name_trust_reason=segment.get("name_trust_reason"),
            name_consensus_evidence=segment.get("name_consensus_evidence") or {}, role_raw=segment.get("role_raw") or None,
            role_clean=segment.get("role_clean") or None, track_value=segment.get("track_clean") or None,
            track_confidence=segment.get("track_confidence"), clip_start=clip_start, clip_end=clip_end,
            clip_path=str(clip_path), poster_path=str(poster_path), manifest_path=str(manifest_path))
        session.add(revision); session.flush()
        mapped: list[Candidate] = []
        for rank, (image, metadata) in enumerate(legacy_candidates, 1):
            destination = candidates_dir / f"candidate_{rank:03d}.jpg"; shutil.copy2(image, destination)
            candidate = Candidate(revision_id=revision.id, rank=rank, source_timestamp=float(metadata.get("source_timestamp", start)),
                                  image_path=str(destination), source="legacy_auto", quality=metadata.get("score"), selected=(rank == 1))
            session.add(candidate); session.flush(); mapped.append(candidate)
        if interview.review_status.value != "approved":
            interview.active_revision_id = revision.id
            if mapped:
                interview.selected_candidate_id = mapped[0].id
            if not interview.final_name and revision.trusted_name:
                interview.final_name, interview.identity_source = revision.trusted_name, "ocr_trusted"
        session.flush()
        revision.manifest = _manifest(interview, revision, mapped, recording, settings.data_dir)
        manifest_path.write_text(json.dumps(revision.manifest, indent=2), encoding="utf-8")
        interviews.append(interview)
    processing_run.summary = result.summary
    return interviews
