from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..models import (
    Candidate, Interview, InterviewContext, InterviewRevision, Job, JobStatus, ProcessingRun, Recording,
    RecordingSession, ReviewEvent,
)


WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
API_PATH = re.compile(r"^/api/")


@dataclass(frozen=True)
class RelocationRoots:
    old_source: str
    new_source: str
    old_data: str
    new_data: str


@dataclass
class PathChange:
    table: str
    column: str
    row_id: int
    old: Any
    new: Any
    target_exists: bool | None = None


@dataclass(frozen=True)
class RelocationIssue:
    table: str
    column: str
    row_id: int
    value: str
    reason: str


@dataclass
class RelocationPlan:
    changes: list[PathChange] = field(default_factory=list)
    unresolved: list[RelocationIssue] = field(default_factory=list)
    missing_targets: list[RelocationIssue] = field(default_factory=list)
    audited: dict[str, dict[str, int]] = field(default_factory=dict)

    def report(self) -> dict[str, Any]:
        affected: dict[str, int] = {}
        for change in self.changes:
            key = f"{change.table}.{change.column}"
            affected[key] = affected.get(key, 0) + 1
        return {
            "audited": self.audited,
            "affected": affected,
            "change_count": len(self.changes),
            "unresolved": [asdict(item) for item in self.unresolved],
            "missing_targets": [asdict(item) for item in self.missing_targets],
        }


PATH_COLUMNS = (
    (Job, "jobs", "log_path", "data"),
    (ProcessingRun, "processing_runs", "job_dir", "data"),
    (InterviewRevision, "interview_revisions", "clip_path", "data"),
    (InterviewRevision, "interview_revisions", "poster_path", "data"),
    (InterviewRevision, "interview_revisions", "manifest_path", "data"),
    (Candidate, "candidates", "image_path", "data"),
    (RecordingSession, "recording_sessions", "log_path", "data"),
    (RecordingSession, "recording_sessions", "active_path", "source"),
)

JSON_COLUMNS = (
    (ProcessingRun, "processing_runs", "summary"),
    (InterviewRevision, "interview_revisions", "manifest"),
    (InterviewContext, "interview_contexts", "evidence"),
    (ReviewEvent, "review_events", "payload"),
)


def validate_roots(roots: RelocationRoots) -> None:
    values = [roots.old_source, roots.new_source, roots.old_data, roots.new_data]
    if any(not value or not _is_absolute(value) for value in values):
        raise ValueError("all relocation roots must be explicit absolute paths")
    if _same_path(roots.old_source, roots.new_source) or _same_path(roots.old_data, roots.new_data):
        raise ValueError("old and new roots must differ")
    for first, second in ((roots.old_source, roots.old_data), (roots.new_source, roots.new_data)):
        if _is_within(first, second) or _is_within(second, first):
            raise ValueError("source and data roots must not overlap")


def plan_runtime_relocation(session: Session, roots: RelocationRoots, *, inspect_files: bool = True) -> RelocationPlan:
    validate_roots(roots)
    plan = RelocationPlan()

    recordings = session.query(Recording).order_by(Recording.id).all()
    source_audit = _audit_bucket(plan, "recordings", "source_path")
    for recording in recordings:
        value = recording.source_path
        _audit_value(source_audit, value)
        mapped = _map_recording(recording, roots)
        if mapped is None:
            if not _is_within(value, roots.new_source):
                plan.unresolved.append(RelocationIssue("recordings", "source_path", recording.id, value, "outside approved source roots"))
            continue
        target_exists = _target_exists(mapped) if inspect_files else None
        if target_exists:
            size = Path(mapped).stat().st_size
            if size != recording.size_bytes:
                plan.unresolved.append(RelocationIssue("recordings", "source_path", recording.id, value, "target size conflicts with database identity"))
                continue
        elif inspect_files:
            plan.missing_targets.append(RelocationIssue("recordings", "source_path", recording.id, mapped, "source file missing"))
        plan.changes.append(PathChange("recordings", "source_path", recording.id, value, mapped, target_exists))

    for model, table, column, root_kind in PATH_COLUMNS:
        bucket = _audit_bucket(plan, table, column)
        for row_id, value in session.execute(select(model.id, getattr(model, column)).order_by(model.id)):
            if value in (None, ""):
                continue
            _audit_value(bucket, str(value))
            old_root, new_root = ((roots.old_data, roots.new_data) if root_kind == "data" else (roots.old_source, roots.new_source))
            mapped = map_approved_path(str(value), old_root, new_root)
            if mapped is None:
                if not _is_within(str(value), new_root):
                    plan.unresolved.append(RelocationIssue(table, column, row_id, str(value), f"outside approved {root_kind} roots"))
                continue
            target_exists = _target_exists(mapped) if inspect_files else None
            if inspect_files and not target_exists:
                plan.missing_targets.append(RelocationIssue(table, column, row_id, mapped, "artifact target missing"))
            plan.changes.append(PathChange(table, column, row_id, str(value), mapped, target_exists))

    for model, table, column in JSON_COLUMNS:
        bucket = _audit_bucket(plan, table, column)
        for row in session.query(model).order_by(model.id):
            value = getattr(row, column)
            if not value:
                continue
            rewritten, found, unresolved = _rewrite_json(value, roots)
            bucket["rows"] += 1
            bucket["windows_paths"] += found
            for item in unresolved:
                plan.unresolved.append(RelocationIssue(table, column, row.id, item, "absolute JSON path outside approved roots"))
            if rewritten != value:
                plan.changes.append(PathChange(table, column, row.id, copy.deepcopy(value), rewritten, None))
    return plan


def apply_runtime_relocation(session: Session, plan: RelocationPlan, roots: RelocationRoots) -> None:
    validate_roots(roots)
    if plan.unresolved:
        raise ValueError("relocation has unresolved external paths")
    missing_sources = [item for item in plan.missing_targets if item.table == "recordings"]
    if missing_sources:
        raise ValueError("relocation target is missing recording sources")
    models = {
        "recordings": Recording, "jobs": Job, "processing_runs": ProcessingRun,
        "interview_revisions": InterviewRevision, "candidates": Candidate,
        "recording_sessions": RecordingSession, "interview_contexts": InterviewContext,
        "review_events": ReviewEvent,
    }
    for change in plan.changes:
        model = models[change.table]
        column = getattr(model, change.column)
        current = session.execute(select(column).where(model.id == change.row_id)).scalar_one_or_none()
        if current != change.old:
            raise ValueError(f"relocation plan became stale at {change.table}.{change.column}:{change.row_id}")
        values = {change.column: copy.deepcopy(change.new)}
        if change.table == "recordings" and change.column == "source_path" and Path(change.new).is_file():
            stat = Path(change.new).stat()
            filename = session.execute(select(Recording.filename).where(Recording.id == change.row_id)).scalar_one()
            values.update(mtime_epoch=stat.st_mtime, fingerprint=_recording_fingerprint(filename, stat.st_size, stat.st_mtime_ns))
        session.execute(update(model).where(model.id == change.row_id).values(**values))
    session.flush()
    _validate_relocated_rows(session, roots)


def map_approved_path(value: str, old_root: str, new_root: str) -> str | None:
    if URL.match(value):
        return None
    relative = _relative_to(value, old_root)
    if relative is None:
        return None
    if any(part in ("", ".", "..") for part in relative.parts):
        raise ValueError("unsafe relative path in relocation")
    root = _pure(new_root)
    mapped = root.joinpath(*relative.parts)
    if not _is_within(str(mapped), new_root):
        raise ValueError("relocated path escapes approved root")
    return str(mapped)


def semantic_snapshot(session: Session) -> dict[str, Any]:
    queued = [(row.id, row.recording_id, row.status.value, bool(row.released_for_processing))
              for row in session.query(Job).order_by(Job.id)]
    queue_identity = [(row.id, row.recording_id, row.status.name)
                      for row in session.query(Job).filter(Job.status == JobStatus.QUEUED).order_by(Job.id)]
    payload = json.dumps(queue_identity, separators=(",", ":"))
    return {
        "recordings": session.query(Recording).count(),
        "jobs": queued,
        "queue_sha256": hashlib.sha256(payload.encode()).hexdigest(),
        "reviews": [(row.id, row.interview_key, row.review_status.value, row.export_state, row.final_name,
                     row.identity_source, row.active_revision_id, row.selected_candidate_id)
                    for row in session.query(Interview).order_by(Interview.id)],
    }


def _map_recording(recording: Recording, roots: RelocationRoots) -> str | None:
    if _is_within(recording.source_path, roots.new_source):
        return None
    if _relative_to(recording.source_path, roots.old_source) is None:
        return None
    if Path(recording.filename).name != recording.filename or Path(recording.filename).suffix.lower() != ".ts":
        raise ValueError(f"unsafe recording filename for row {recording.id}")
    return str(_pure(roots.new_source).joinpath(recording.filename))


def _rewrite_json(value: Any, roots: RelocationRoots) -> tuple[Any, int, list[str]]:
    found = 0
    unresolved: list[str] = []
    if isinstance(value, dict):
        rewritten = {}
        for key, item in value.items():
            mapped, count, issues = _rewrite_json(item, roots)
            rewritten[key] = mapped; found += count; unresolved.extend(issues)
        return rewritten, found, unresolved
    if isinstance(value, list):
        rewritten = []
        for item in value:
            mapped, count, issues = _rewrite_json(item, roots)
            rewritten.append(mapped); found += count; unresolved.extend(issues)
        return rewritten, found, unresolved
    if not isinstance(value, str) or URL.match(value) or API_PATH.match(value) or not _is_absolute(value):
        return value, 0, []
    found = 1 if WINDOWS_ABSOLUTE.match(value) else 0
    for old_root, new_root in ((roots.old_source, roots.new_source), (roots.old_data, roots.new_data)):
        mapped = map_approved_path(value, old_root, new_root)
        if mapped is not None:
            return mapped, found, []
    if _is_within(value, roots.new_source) or _is_within(value, roots.new_data):
        return value, found, []
    return value, found, [value]


def _validate_relocated_rows(session: Session, roots: RelocationRoots) -> None:
    for recording in session.query(Recording):
        if not _is_within(recording.source_path, roots.new_source):
            raise ValueError(f"recording path did not relocate: {recording.id}")
    for model, _table, column, root_kind in PATH_COLUMNS:
        root = roots.new_data if root_kind == "data" else roots.new_source
        for row_id, value in session.execute(select(model.id, getattr(model, column))):
            if value and not _is_within(str(value), root):
                raise ValueError(f"artifact path did not relocate: {model.__tablename__}.{column}:{row_id}")


def _audit_bucket(plan: RelocationPlan, table: str, column: str) -> dict[str, int]:
    return plan.audited.setdefault(f"{table}.{column}", {"rows": 0, "windows_paths": 0})


def _audit_value(bucket: dict[str, int], value: str) -> None:
    bucket["rows"] += 1
    bucket["windows_paths"] += int(bool(WINDOWS_ABSOLUTE.match(value)))


def _target_exists(value: str) -> bool:
    return Path(value).exists()


def _recording_fingerprint(filename: str, size: int, mtime_ns: int) -> str:
    return hashlib.sha256(f"{filename}|{size}|{mtime_ns}".encode()).hexdigest()[:32]


def _pure(value: str):
    return PureWindowsPath(value) if WINDOWS_ABSOLUTE.match(value) else PurePosixPath(value)


def _is_absolute(value: str) -> bool:
    return bool(WINDOWS_ABSOLUTE.match(value) or PurePosixPath(value).is_absolute()) and not URL.match(value)


def _relative_to(value: str, root: str):
    path, base = _pure(value), _pure(root)
    if type(path) is not type(base):
        return None
    try:
        return path.relative_to(base)
    except ValueError:
        return None


def _is_within(value: str, root: str) -> bool:
    relative = _relative_to(value, root)
    return relative is not None


def _same_path(first: str, second: str) -> bool:
    return type(_pure(first)) is type(_pure(second)) and _pure(first) == _pure(second)
