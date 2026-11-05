from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Interview, InterviewRevision, Job, JobStatus, Recording, ReviewStatus, SourceDeletionAudit
from .operations import get_value
from .paths import resolve_data_artifact, resolve_recording_source


LOG = logging.getLogger(__name__)


def deletion_eligibility(session: Session, settings: Settings, recording: Recording) -> dict:
    checks: dict[str, bool] = {}
    try:
        resolve_recording_source(settings, recording, verify_size=True)
        checks["contained_ts_source"] = True
    except (ValueError, FileNotFoundError, OSError):
        checks["contained_ts_source"] = False
    checks["deletion_enabled"] = bool(get_value(session, "source_deletion_enabled", False))
    checks["closed"] = bool(recording.is_closed or recording.stable_at)
    checks["not_processing"] = not bool(session.execute(select(Job).where(Job.recording_id == recording.id, Job.status == JobStatus.PROCESSING)).first())
    checks["no_queued_job"] = not bool(session.execute(select(Job).where(Job.recording_id == recording.id, Job.status == JobStatus.QUEUED)).first())
    checks["completed_run"] = bool(session.execute(select(Job).where(Job.recording_id == recording.id, Job.status == JobStatus.COMPLETE)).first())
    interviews = session.execute(select(Interview).where(Interview.recording_id == recording.id)).scalars().all()
    require_review = bool(get_value(session, "require_review_complete_before_delete", True))
    # A completed run with no interviews has no editorial work to finish. These
    # universal gates are intentionally true for an empty result set.
    checks["review_complete"] = (not require_review) or all(
        item.review_status in {ReviewStatus.APPROVED, ReviewStatus.REJECTED} for item in interviews
    )
    require_export = bool(get_value(session, "require_export_before_delete", True))
    checks["export_complete"] = (not require_export) or all(
        item.review_status != ReviewStatus.APPROVED or item.export_state == "exported" for item in interviews
    )
    retention = timedelta(hours=int(get_value(session, "source_retention_hours", 168)))
    checks["retention_elapsed"] = recording.first_discovered_at <= datetime.utcnow() - retention
    revisions = [session.get(InterviewRevision, item.active_revision_id) for item in interviews]
    try:
        checks["evidence_present"] = all(
            revision
            and resolve_data_artifact(settings, revision.clip_path).exists()
            and resolve_data_artifact(settings, revision.poster_path).exists()
            and resolve_data_artifact(settings, revision.manifest_path).exists()
            for revision in revisions
        )
    except (ValueError, FileNotFoundError, OSError):
        checks["evidence_present"] = False
    checks["approved_portraits_present"] = all(
        item.review_status != ReviewStatus.APPROVED
        or (settings.data_dir / "approved" / f"{item.interview_key}.jpg").is_file()
        for item in interviews
    )
    return {
        "recording_id": recording.id,
        "filename": recording.filename,
        "safe_to_delete": all(checks.values()),
        "checks": checks,
    }


def delete_if_eligible(session: Session, settings: Settings, recording: Recording) -> dict:
    result = deletion_eligibility(session, settings, recording)
    if not result["safe_to_delete"]:
        return result
    # Re-resolve identity and containment immediately before the only destructive
    # operation. Persisted host paths are never trusted here.
    path = resolve_recording_source(settings, recording, verify_size=True)
    if path.resolve().parent != settings.source_dir.resolve():
        raise ValueError("source no longer passes containment check")
    size = path.stat().st_size
    path.unlink()
    session.add(SourceDeletionAudit(
        recording_id=recording.id,
        filename=recording.filename,
        size_bytes=size,
        reason="all safety checks passed",
        checks=result["checks"],
    ))
    session.commit()
    return {**result, "deleted": True}


def sweep_source_lifecycle(
    session: Session,
    settings: Settings,
    *,
    limit: int,
    after_recording_id: int = 0,
) -> dict:
    """Inspect at most ``limit`` rows, isolating every candidate failure."""
    limit = max(1, min(int(limit), 100))
    rows = session.execute(
        select(Recording).where(Recording.id > after_recording_id).order_by(Recording.id).limit(limit)
    ).scalars().all()
    wrapped = False
    if not rows and after_recording_id:
        rows = session.execute(select(Recording).order_by(Recording.id).limit(limit)).scalars().all()
        wrapped = True

    result = {
        "enabled": bool(get_value(session, "source_deletion_enabled", False)),
        "limit": limit,
        "inspected": len(rows),
        "eligible": 0,
        "deleted": 0,
        "failed": 0,
        "wrapped": wrapped,
        "next_after_recording_id": rows[-1].id if rows else 0,
    }
    for recording in rows:
        try:
            outcome = delete_if_eligible(session, settings, recording)
            result["eligible"] += int(outcome["safe_to_delete"])
            result["deleted"] += int(bool(outcome.get("deleted")))
        except Exception:
            session.rollback()
            result["failed"] += 1
            LOG.exception("source lifecycle candidate failed", extra={"recording_id": recording.id})
    if result["deleted"] or result["failed"]:
        LOG.info("source lifecycle sweep completed", extra={"source_lifecycle": result})
    return result


def source_lifecycle_status(session: Session, settings: Settings, *, recent_limit: int = 10) -> dict:
    counts = {
        "total_recordings": 0,
        "eligible": 0,
        "waiting_retention": 0,
        "waiting_review": 0,
        "waiting_export": 0,
        "processing_or_queued": 0,
        "missing_evidence": 0,
        "missing_source": 0,
    }
    for recording in session.execute(select(Recording).order_by(Recording.id)).scalars():
        result = deletion_eligibility(session, settings, recording)
        checks = result["checks"]
        counts["total_recordings"] += 1
        counts["eligible"] += int(result["safe_to_delete"])
        counts["waiting_retention"] += int(not checks["retention_elapsed"])
        counts["waiting_review"] += int(not checks["review_complete"])
        counts["waiting_export"] += int(not checks["export_complete"])
        counts["processing_or_queued"] += int(not checks["not_processing"] or not checks["no_queued_job"])
        counts["missing_evidence"] += int(not checks["evidence_present"] or not checks["approved_portraits_present"])
        counts["missing_source"] += int(not checks["contained_ts_source"])
    audits = session.execute(
        select(SourceDeletionAudit).order_by(SourceDeletionAudit.id.desc()).limit(max(0, min(recent_limit, 50)))
    ).scalars().all()
    return {
        "enabled": bool(get_value(session, "source_deletion_enabled", False)),
        "retention_hours": int(get_value(session, "source_retention_hours", 168)),
        "require_review": bool(get_value(session, "require_review_complete_before_delete", True)),
        "require_export": bool(get_value(session, "require_export_before_delete", True)),
        "sweep_limit": settings.source_lifecycle_sweep_limit,
        "counts": counts,
        "recent_deletions": [
            {
                "recording_id": item.recording_id,
                "filename": item.filename,
                "size_bytes": item.size_bytes,
                "reason": item.reason,
                "deleted_at": item.deleted_at,
            }
            for item in audits
        ],
    }
