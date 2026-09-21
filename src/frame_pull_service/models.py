from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.utcnow()


class RecordingStatus(str, enum.Enum):
    HISTORICAL = "historical"
    DISCOVERED = "discovered"
    WAITING = "waiting_for_stability"
    READY = "ready"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETE = "complete"
    FAILED = "failed"
    IGNORED = "ignored"


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETE = "complete"
    FAILED = "failed"
    STALE = "stale"


class ReviewStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class Recording(Base):
    __tablename__ = "recordings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    source_path: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(128), unique=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    mtime_epoch: Mapped[float] = mapped_column(Float)
    first_discovered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    stable_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[RecordingStatus] = mapped_column(Enum(RecordingStatus), default=RecordingStatus.DISCOVERED)
    historical: Mapped[bool] = mapped_column(Boolean, default=False)
    jobs: Mapped[list["Job"]] = relationship(back_populates="recording")


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recording_id: Mapped[int] = mapped_column(ForeignKey("recordings.id"), index=True)
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.QUEUED, index=True)
    queued_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    progress_stage: Mapped[str] = mapped_column(String(120), default="queued")
    progress_percent: Mapped[float] = mapped_column(Float, default=0.0)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    log_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    processing_run_id: Mapped[int | None] = mapped_column(ForeignKey("processing_runs.id"), nullable=True)
    recording: Mapped[Recording] = relationship(back_populates="jobs")


class ProcessingRun(Base):
    __tablename__ = "processing_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recording_id: Mapped[int] = mapped_column(ForeignKey("recordings.id"), index=True)
    run_key: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="processing")
    job_dir: Mapped[str] = mapped_column(Text)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)


class Interview(Base):
    __tablename__ = "interviews"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    interview_key: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    recording_id: Mapped[int] = mapped_column(ForeignKey("recordings.id"), index=True)
    source_start: Mapped[float] = mapped_column(Float)
    source_end: Mapped[float] = mapped_column(Float)
    bounds_basis: Mapped[str] = mapped_column(String(64))
    review_status: Mapped[ReviewStatus] = mapped_column(Enum(ReviewStatus), default=ReviewStatus.PENDING, index=True)
    export_state: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    active_revision_id: Mapped[int | None] = mapped_column(ForeignKey("interview_revisions.id"), nullable=True)
    selected_candidate_id: Mapped[int | None] = mapped_column(ForeignKey("candidates.id"), nullable=True)
    final_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    final_role: Mapped[str | None] = mapped_column(String(255), nullable=True)
    final_track: Mapped[str | None] = mapped_column(String(255), nullable=True)
    identity_source: Mapped[str] = mapped_column(String(32), default="unknown")
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class InterviewRevision(Base):
    __tablename__ = "interview_revisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    interview_id: Mapped[int] = mapped_column(ForeignKey("interviews.id"), index=True)
    processing_run_id: Mapped[int] = mapped_column(ForeignKey("processing_runs.id"), index=True)
    revision_number: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    trusted_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ocr_name_candidate: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ocr_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    name_trust_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    name_consensus_evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    role_raw: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role_clean: Mapped[str | None] = mapped_column(String(255), nullable=True)
    track_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    track_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    clip_start: Mapped[float] = mapped_column(Float)
    clip_end: Mapped[float] = mapped_column(Float)
    clip_path: Mapped[str] = mapped_column(Text)
    poster_path: Mapped[str] = mapped_column(Text)
    manifest_path: Mapped[str] = mapped_column(Text)
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    candidates: Mapped[list["Candidate"]] = relationship(back_populates="revision")
    __table_args__ = (UniqueConstraint("interview_id", "revision_number"),)


class Candidate(Base):
    __tablename__ = "candidates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("interview_revisions.id"), index=True)
    rank: Mapped[int] = mapped_column(Integer)
    source_timestamp: Mapped[float] = mapped_column(Float)
    image_path: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(64), default="legacy_auto")
    quality: Mapped[float | None] = mapped_column(Float, nullable=True)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[InterviewRevision] = relationship(back_populates="candidates")


class ReviewEvent(Base):
    __tablename__ = "review_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    interview_id: Mapped[int] = mapped_column(ForeignKey("interviews.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
