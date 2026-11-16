from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import DetectorCandidateRecovery, DetectorCandidateTriage, Interview, ProcessingRun, RaceDay, Recording
from .paths import resolve_data_artifact
from .time_model import iso_broadcast


CANDIDATE_KEY = re.compile(r"^run-(\d+)-temporal-(\d+)$")
SOURCE_REVIEW_STATES = {"not_reviewed", "in_progress", "review_complete"}
MISS_CATEGORIES = {
    "detector_saw_strap_normalization_rejected",
    "ocr_insufficient",
    "temporal_grouping_issue",
    "candidate_bounds_wrong",
    "detector_had_no_useful_evidence",
    "unknown",
}


def _temporal_audit(settings: Settings, run: ProcessingRun) -> list[dict]:
    try:
        root = resolve_data_artifact(settings, run.job_dir)
    except (ValueError, FileNotFoundError, OSError):
        return []
    matches = list(root.glob("legacy_work/*/temporal_authentication_audit.json"))
    if len(matches) != 1:
        return []
    path = resolve_data_artifact(settings, str(matches[0]))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    return value if isinstance(value, list) else []


def _reason(item: dict) -> str:
    observations = int(item.get("observations") or 0)
    required_observations = int(item.get("required_observations") or 0)
    stable = int(item.get("best_stable_pair_run") or 0)
    required_stable = int(item.get("required_stable_pairs") or 0)
    if observations < required_observations:
        return f"insufficient temporal observations ({observations}/{required_observations})"
    if stable < required_stable:
        return f"insufficient stable hit pairs ({stable}/{required_stable})"
    return "temporal authentication rejected"


def _overlaps(start: float, end: float, interviews: list[Interview]) -> bool:
    right = max(start, end)
    return any(item.source_start <= right and item.source_end >= start for item in interviews)


def detector_candidates_for_recording(
    session: Session,
    settings: Settings,
    recording: Recording,
) -> list[dict]:
    run = session.execute(
        select(ProcessingRun).where(
            ProcessingRun.recording_id == recording.id,
            ProcessingRun.status == "complete",
        ).order_by(ProcessingRun.id.desc())
    ).scalars().first()
    if run is None:
        return []
    interviews = session.execute(select(Interview).where(Interview.recording_id == recording.id)).scalars().all()
    recoveries = {
        row.candidate_key: row for row in session.execute(
            select(DetectorCandidateRecovery).where(DetectorCandidateRecovery.recording_id == recording.id)
        ).scalars()
    }
    triage_rows = {
        row.candidate_key: row for row in session.execute(
            select(DetectorCandidateTriage).where(DetectorCandidateTriage.recording_id == recording.id)
        ).scalars()
    }
    rows: list[dict] = []
    for index, item in enumerate(_temporal_audit(settings, run)):
        if not isinstance(item, dict) or bool(item.get("authenticated")):
            continue
        start = max(0.0, float(item.get("start") or 0.0))
        end = max(start, float(item.get("end") or start))
        duration = max(float(recording.duration_seconds or 0), end + 5.0)
        proposed_start = max(0.0, start - 2.0)
        proposed_end = min(duration, max(end + 5.0, proposed_start + 5.0))
        observations = int(item.get("observations") or 0)
        required_observations = max(1, int(item.get("required_observations") or 1))
        stable = int(item.get("best_stable_pair_run") or 0)
        required_stable = max(1, int(item.get("required_stable_pairs") or 1))
        key = f"run-{run.id}-temporal-{index}"
        recovery = recoveries.get(key)
        triage = triage_rows.get(key)
        rows.append({
            "candidate_key": key,
            "processing_run_id": run.id,
            "recording_id": recording.id,
            "source_start": start,
            "source_end": end,
            "duration_seconds": end - start,
            "proposed_start": proposed_start,
            "proposed_end": proposed_end,
            "local_time": iso_broadcast(
                recording.recording_started_at + timedelta(seconds=start)
                if recording.recording_started_at else None
            ),
            # Temporal rejects never reached the OCR persistence stage. Keep
            # these fields explicit and empty rather than inferring identity.
            "ocr_text": None,
            "ocr_confidence": None,
            "role_text": None,
            "hit_count": observations,
            "temporal_evidence": {
                "observations": observations,
                "required_observations": required_observations,
                "best_stable_pair_run": stable,
                "required_stable_pairs": required_stable,
                "threshold": float(item.get("threshold") or 0.0),
            },
            "rejection_stage": "temporal_authentication",
            "rejection_reason": _reason(item),
            "overlaps_existing_interview": _overlaps(start, end, interviews),
            "evidence_quality": round(
                min(1.0, observations / required_observations) * 0.7
                + min(1.0, stable / required_stable) * 0.3,
                4,
            ),
            "recovered_interview_id": recovery.interview_id if recovery else None,
            "miss_category": recovery.miss_category if recovery else None,
            "triage_state": "recovered" if recovery else (triage.review_state if triage else "unreviewed"),
            "triage_reviewed_at": triage.reviewed_at if triage else None,
            "triage_note": triage.operator_note if triage else None,
            "derived_reinspection": triage.derived_reinspection if triage else None,
            "previews": [
                {"slot": slot, "url": f"/api/detector-candidates/{key}/preview/{slot}"}
                for slot in ("start", "midpoint", "end")
            ],
        })
    return rows


def detector_candidates_for_race_day(session: Session, settings: Settings, race_day_id: int) -> list[dict]:
    records = session.execute(
        select(Recording).where(Recording.race_day_id == race_day_id).order_by(
            Recording.recording_started_at, Recording.id
        )
    ).scalars().all()
    rows = [row for recording in records for row in detector_candidates_for_recording(session, settings, recording)]
    return sorted(rows, key=lambda row: (row["local_time"] or "", row["recording_id"], row["source_start"]))


def get_detector_candidate(
    session: Session,
    settings: Settings,
    candidate_key: str,
    *,
    recording_id: int | None = None,
) -> dict:
    match = CANDIDATE_KEY.fullmatch(candidate_key or "")
    if not match:
        raise ValueError("detector candidate key is invalid")
    run = session.get(ProcessingRun, int(match.group(1)))
    if run is None or run.status != "complete" or (recording_id is not None and run.recording_id != recording_id):
        raise ValueError("detector candidate is unavailable")
    recording = session.get(Recording, run.recording_id)
    if recording is None:
        raise ValueError("detector candidate recording is unavailable")
    rows = detector_candidates_for_recording(session, settings, recording)
    candidate = next((row for row in rows if row["candidate_key"] == candidate_key), None)
    if candidate is None:
        raise ValueError("detector candidate is unavailable")
    return candidate


def set_source_review_state(session: Session, race_day: RaceDay, state: str, *, note: str | None = None,
                            unresolved_count: int | None = None) -> dict:
    if state not in SOURCE_REVIEW_STATES:
        raise ValueError("source review state must be not_reviewed, in_progress, or review_complete")
    race_day.source_review_state = state
    race_day.source_review_updated_at = datetime.utcnow()
    race_day.source_review_note = (note or "").strip()[:2000] or None
    race_day.source_review_unresolved_count = unresolved_count if state == "review_complete" else None
    session.commit()
    return source_review_view(race_day)


def source_review_view(race_day: RaceDay) -> dict:
    return {
        "state": race_day.source_review_state or "not_reviewed",
        "updated_at": race_day.source_review_updated_at,
        "note": race_day.source_review_note,
        "unresolved_count_at_completion": race_day.source_review_unresolved_count,
    }
