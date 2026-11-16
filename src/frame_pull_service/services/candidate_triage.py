from __future__ import annotations

import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import DetectorCandidateTriage, DetectorCandidateTriageEvent, ProcessingRun, Recording
from .detector_candidates import get_detector_candidate
from .media import extract_frame
from .operations import get_value
from .paths import contained_path, resolve_recording_source


TRIAGE_STATES = {"unreviewed", "not_interview", "recovered", "deferred"}
PREVIEW_SLOTS = ("start", "midpoint", "end")
_PREVIEW_LOCKS: dict[str, threading.Lock] = {}
_PREVIEW_LOCKS_GUARD = threading.Lock()


def _preview_lock(path: Path) -> threading.Lock:
    with _PREVIEW_LOCKS_GUARD:
        return _PREVIEW_LOCKS.setdefault(str(path), threading.Lock())


def _cache_root(settings: Settings) -> Path:
    return contained_path(settings.data_dir, settings.data_dir / "candidate_preview_cache")


def _candidate_dir(settings: Settings, candidate: dict) -> Path:
    safe_key = candidate["candidate_key"].replace("/", "_").replace("\\", "_")
    return contained_path(_cache_root(settings), _cache_root(settings) / f"run_{candidate['processing_run_id']}" / safe_key)


def preview_times(candidate: dict) -> dict[str, float]:
    start, end = float(candidate["source_start"]), float(candidate["source_end"])
    return {"start": start, "midpoint": (start + end) / 2.0, "end": max(end, start + 0.5)}


def cleanup_candidate_preview_cache(session: Session, settings: Settings, *, preserve: Path | None = None) -> dict:
    root = _cache_root(settings); root.mkdir(parents=True, exist_ok=True)
    maximum = int(get_value(session, "candidate_preview_cache_max_bytes", 2 * 1024 ** 3))
    files = sorted((p for p in root.rglob("*.jpg") if p.is_file()), key=lambda p: p.stat().st_mtime)
    total, deleted = sum(p.stat().st_size for p in files), 0
    for path in files:
        if total <= maximum: break
        if preserve is not None and path.resolve() == preserve.resolve(): continue
        size = path.stat().st_size; path.unlink(); total -= size; deleted += 1
    for directory in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
        try: directory.rmdir()
        except OSError: pass
    return {"count": len(files) - deleted, "bytes": total, "max_bytes": maximum, "deleted": deleted,
            "free_bytes": shutil.disk_usage(settings.data_dir).free}


def candidate_preview(
    session: Session, settings: Settings, candidate_key: str, slot: str, *, frame_extractor=extract_frame,
) -> Path:
    if slot not in PREVIEW_SLOTS: raise ValueError("preview slot must be start, midpoint, or end")
    candidate = get_detector_candidate(session, settings, candidate_key)
    recording = session.get(Recording, candidate["recording_id"])
    if recording is None: raise ValueError("detector candidate recording is unavailable")
    source = resolve_recording_source(settings, recording, verify_size=True)
    minimum_free = float(get_value(session, "playback_cache_min_free_gb", 10)) * 1024 ** 3
    if shutil.disk_usage(settings.data_dir).free < minimum_free: raise ValueError("insufficient free space for derived preview")
    destination = _candidate_dir(settings, candidate) / f"{slot}_{int(preview_times(candidate)[slot] * 1000):010d}.jpg"
    with _preview_lock(destination):
        if not destination.is_file() or destination.stat().st_size == 0:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f"{destination.stem}.{uuid4().hex}.tmp.jpg")
            try:
                frame_extractor("ffmpeg", source, temporary, preview_times(candidate)[slot])
                if not temporary.is_file() or temporary.stat().st_size == 0:
                    raise ValueError("preview extraction produced no image")
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
    destination.touch()
    cleanup_candidate_preview_cache(session, settings, preserve=destination)
    return contained_path(_cache_root(settings), destination)


def preview_view(candidate: dict) -> list[dict]:
    times = preview_times(candidate)
    return [{"slot": slot, "source_time": times[slot],
             "url": f"/api/detector-candidates/{candidate['candidate_key']}/preview/{slot}"} for slot in PREVIEW_SLOTS]


def triage_view(row: DetectorCandidateTriage | None) -> dict:
    return {"state": row.review_state if row else "unreviewed", "reviewed_at": row.reviewed_at if row else None,
            "note": row.operator_note if row else None,
            "recovered_interview_id": row.recovered_interview_id if row else None,
            "derived_reinspection": row.derived_reinspection if row else None}


def set_candidate_triage(session: Session, settings: Settings, candidate_key: str, state: str, *, note: str | None = None) -> dict:
    if state not in TRIAGE_STATES or state == "recovered":
        raise ValueError("triage state must be unreviewed, not_interview, or deferred")
    candidate = get_detector_candidate(session, settings, candidate_key)
    row = session.execute(select(DetectorCandidateTriage).where(DetectorCandidateTriage.candidate_key == candidate_key)).scalar_one_or_none()
    if row is None:
        row = DetectorCandidateTriage(candidate_key=candidate_key, processing_run_id=candidate["processing_run_id"],
                                      recording_id=candidate["recording_id"]); session.add(row)
    if row.recovered_interview_id: raise ValueError("recovered candidate triage cannot be changed")
    prior_state = row.review_state or "unreviewed"
    row.review_state, row.operator_note = state, (note or "").strip()[:2000] or None
    row.reviewed_at = datetime.utcnow() if state != "unreviewed" else None
    session.add(DetectorCandidateTriageEvent(
        candidate_key=candidate_key, processing_run_id=candidate["processing_run_id"],
        recording_id=candidate["recording_id"], from_state=prior_state, to_state=state,
        operator_note=row.operator_note,
    ))
    session.commit(); return triage_view(row)


def _tesseract_tsv(command: str, image: Path) -> dict:
    result = subprocess.run([command, str(image), "stdout", "--psm", "6", "tsv"], check=True, text=True, capture_output=True)
    observations=[]
    for line in result.stdout.splitlines()[1:]:
        parts=line.split("\t", 11)
        if len(parts) < 12 or not parts[11].strip(): continue
        try: confidence=float(parts[10])
        except ValueError: continue
        observations.append({"text":parts[11].strip(),"confidence":confidence})
    text=" ".join(item["text"] for item in observations)
    positive=[item["confidence"] for item in observations if item["confidence"] >= 0]
    return {"text":text or None,"confidence":round(sum(positive)/len(positive),2) if positive else None,
            "observations":observations}


def reinspect_candidate(session: Session, settings: Settings, candidate_key: str, *, analyzer=_tesseract_tsv,
                        frame_extractor=extract_frame) -> dict:
    candidate=get_detector_candidate(session, settings, candidate_key)
    results=[]
    for slot in PREVIEW_SLOTS:
        preview=candidate_preview(session, settings, candidate_key, slot, frame_extractor=frame_extractor)
        crop=preview.with_name(preview.stem+"_lower_third.jpg")
        with Image.open(preview) as image:
            width,height=image.size; image.crop((0,int(height*0.55),width,height)).save(crop,"JPEG",quality=90)
        try: result=analyzer(settings.legacy_tesseract_cmd or "tesseract", crop)
        finally: crop.unlink(missing_ok=True)
        results.append({"slot":slot,"source_time":preview_times(candidate)[slot],**result})
    texts=[row["text"] for row in results if row.get("text")]
    role=next((role for role in ("TRAINER","JOCKEY") if any(role.lower() in text.lower() for text in texts)),None)
    derived={"kind":"derived_triage_ocr","analysis_timestamp":datetime.utcnow().isoformat()+"Z",
             "candidate_key":candidate_key,"observations":results,"combined_text":" | ".join(texts) or None,"role_text":role,
             "automatic_interview_created":False}
    row=session.execute(select(DetectorCandidateTriage).where(DetectorCandidateTriage.candidate_key==candidate_key)).scalar_one_or_none()
    if row is None:
        row=DetectorCandidateTriage(candidate_key=candidate_key,processing_run_id=candidate["processing_run_id"],recording_id=candidate["recording_id"]);session.add(row)
    row.derived_reinspection=derived;session.commit();return derived
