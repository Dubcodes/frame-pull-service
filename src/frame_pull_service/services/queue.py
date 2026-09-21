from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Job, JobStatus, Recording, RecordingStatus


def queue_recording(session: Session, recording: Recording) -> Job:
    existing = session.execute(select(Job).where(Job.recording_id == recording.id, Job.status.in_([JobStatus.QUEUED, JobStatus.PROCESSING]))).scalar_one_or_none()
    if existing:
        return existing
    job = Job(recording_id=recording.id, status=JobStatus.QUEUED)
    session.add(job)
    recording.status = RecordingStatus.QUEUED
    session.flush()
    return job


def claim_next_job(session: Session) -> Job | None:
    active = session.execute(select(Job).where(Job.status == JobStatus.PROCESSING)).scalar_one_or_none()
    if active:
        return None
    job = session.execute(select(Job).where(Job.status == JobStatus.QUEUED).order_by(Job.queued_at, Job.id)).scalars().first()
    if not job:
        return None
    job.status, job.started_at, job.heartbeat_at = JobStatus.PROCESSING, datetime.utcnow(), datetime.utcnow()
    job.attempt_count += 1
    job.progress_stage = "starting"
    session.get(Recording, job.recording_id).status = RecordingStatus.PROCESSING
    session.commit()
    return job


def recover_stale_jobs(session: Session, stale_after_seconds: int = 300) -> int:
    cutoff = datetime.utcnow() - timedelta(seconds=stale_after_seconds)
    stale = session.execute(select(Job).where(Job.status == JobStatus.PROCESSING, Job.heartbeat_at < cutoff)).scalars().all()
    for job in stale:
        job.status, job.error_summary = JobStatus.STALE, "Worker heartbeat expired; safe manual retry available."
        session.get(Recording, job.recording_id).status = RecordingStatus.FAILED
    session.commit()
    return len(stale)
