from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..models import Job, JobStatus, Recording, RecordingStatus
from .operations import get_value


def queue_recording(session: Session, recording: Recording) -> Job:
    existing = session.execute(select(Job).where(Job.recording_id == recording.id, Job.status.in_([JobStatus.QUEUED, JobStatus.PROCESSING]))).scalar_one_or_none()
    if existing:
        return existing
    job = Job(recording_id=recording.id, status=JobStatus.QUEUED)
    session.add(job)
    recording.status = RecordingStatus.QUEUED
    session.flush()
    return job


def claim_next_job(session: Session, max_concurrent: int = 1, paused: bool | None = None) -> Job | None:
    if paused is None:
        paused = bool(get_value(session, "processing_paused", False))
    if paused:
        return None
    active = session.scalar(select(func.count()).select_from(Job).where(Job.status == JobStatus.PROCESSING)) or 0
    if active >= max(1, max_concurrent):
        return None
    job = session.execute(select(Job).where(Job.status == JobStatus.QUEUED).order_by(Job.queued_at, Job.id)).scalars().first()
    if not job:
        return None
    now = datetime.utcnow()
    claimed = session.execute(update(Job).where(Job.id == job.id, Job.status == JobStatus.QUEUED).values(
        status=JobStatus.PROCESSING, started_at=now, heartbeat_at=now, attempt_count=Job.attempt_count + 1, progress_stage="starting"))
    if not claimed.rowcount:
        session.rollback()
        return None
    session.get(Recording, job.recording_id).status = RecordingStatus.PROCESSING
    session.commit()
    return session.get(Job, job.id)


def queue_selected(session: Session, recording_ids: list[int], reprocess: bool = False) -> dict:
    queued, existing, skipped = [], [], []
    for recording_id in dict.fromkeys(recording_ids):
        recording = session.get(Recording, recording_id)
        if not recording:
            skipped.append(recording_id); continue
        if recording.status == RecordingStatus.COMPLETE and not reprocess:
            skipped.append(recording_id); continue
        before = session.execute(select(Job).where(Job.recording_id == recording.id, Job.status.in_([JobStatus.QUEUED, JobStatus.PROCESSING]))).scalar_one_or_none()
        job = queue_recording(session, recording)
        (existing if before else queued).append(job.id)
    session.commit()
    return {"queued": queued, "existing": existing, "skipped": skipped}


def cancel_queued(session: Session, recording_ids: list[int]) -> dict:
    cancelled, protected = [], []
    for recording_id in dict.fromkeys(recording_ids):
        jobs = session.execute(select(Job).where(Job.recording_id == recording_id).order_by(Job.id.desc())).scalars().all()
        for job in jobs:
            if job.status == JobStatus.PROCESSING:
                protected.append(job.id)
            elif job.status == JobStatus.QUEUED:
                job.status, job.finished_at, job.progress_stage = JobStatus.FAILED, datetime.utcnow(), "cancelled_before_start"
                job.error_summary = "Cancelled before processing by operator."
                recording = session.get(Recording, recording_id)
                if recording:
                    recording.status = RecordingStatus.READY
                cancelled.append(job.id)
    session.commit()
    return {"cancelled": cancelled, "protected_processing": protected}


def recover_stale_jobs(session: Session, stale_after_seconds: int = 300) -> int:
    cutoff = datetime.utcnow() - timedelta(seconds=stale_after_seconds)
    stale = session.execute(select(Job).where(Job.status == JobStatus.PROCESSING, Job.heartbeat_at < cutoff)).scalars().all()
    for job in stale:
        job.status, job.error_summary = JobStatus.STALE, "Worker heartbeat expired; safe manual retry available."
        session.get(Recording, job.recording_id).status = RecordingStatus.FAILED
    session.commit()
    return len(stale)
