from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Interview, InterviewRevision, Job, JobStatus, Recording, ReviewStatus, SourceDeletionAudit
from .operations import get_value
from .paths import source_path


def deletion_eligibility(session: Session, settings: Settings, recording: Recording) -> dict:
    checks: dict[str, bool] = {}
    try:
        source = source_path(settings.source_dir, Path(recording.source_path).name)
        checks["contained_ts_source"] = source.exists()
    except ValueError:
        checks["contained_ts_source"] = False
    checks["deletion_enabled"] = bool(get_value(session, "source_deletion_enabled", False))
    checks["closed"] = bool(recording.is_closed or recording.stable_at)
    checks["not_processing"] = not bool(session.execute(select(Job).where(Job.recording_id == recording.id, Job.status == JobStatus.PROCESSING)).first())
    checks["no_queued_job"] = not bool(session.execute(select(Job).where(Job.recording_id == recording.id, Job.status == JobStatus.QUEUED)).first())
    checks["completed_run"] = bool(session.execute(select(Job).where(Job.recording_id == recording.id, Job.status == JobStatus.COMPLETE)).first())
    interviews = session.execute(select(Interview).where(Interview.recording_id == recording.id)).scalars().all()
    require_review = bool(get_value(session, "require_review_complete_before_delete", True))
    checks["review_complete"] = (not require_review) or (bool(interviews) and all(item.review_status in {ReviewStatus.APPROVED, ReviewStatus.REJECTED} for item in interviews))
    if bool(get_value(session, "require_export_before_delete", True)):
        checks["export_complete"] = all(item.review_status != ReviewStatus.APPROVED or item.export_state == "exported" for item in interviews)
    retention = timedelta(hours=int(get_value(session, "source_retention_hours", 168)))
    checks["retention_elapsed"] = recording.first_discovered_at <= datetime.utcnow() - retention
    revisions = [session.get(InterviewRevision, item.active_revision_id) for item in interviews]
    checks["evidence_present"] = bool(revisions) and all(revision and Path(revision.clip_path).exists() and Path(revision.poster_path).exists() and Path(revision.manifest_path).exists() for revision in revisions)
    checks["approved_portraits_present"] = all(item.review_status != ReviewStatus.APPROVED or (settings.data_dir / "approved" / f"{item.interview_key}.jpg").exists() for item in interviews)
    return {"recording_id": recording.id, "filename": recording.filename, "safe_to_delete": all(checks.values()), "checks": checks}


def delete_if_eligible(session: Session, settings: Settings, recording: Recording) -> dict:
    result = deletion_eligibility(session, settings, recording)
    if not result["safe_to_delete"]:
        return result
    path = source_path(settings.source_dir, Path(recording.source_path).name)
    # Re-check containment immediately before the only destructive operation.
    if not path.exists() or path.resolve().parent != settings.source_dir.resolve():
        raise ValueError("source no longer passes containment check")
    size = path.stat().st_size
    path.unlink()
    session.add(SourceDeletionAudit(recording_id=recording.id, filename=recording.filename, size_bytes=size,
                                    reason="all safety checks passed", checks=result["checks"]))
    session.commit()
    return {**result, "deleted": True}
