from __future__ import annotations

from pathlib import Path

from ..config import Settings
from ..models import Recording


def contained_path(root: Path, candidate: Path) -> Path:
    resolved_root = root.resolve()
    resolved = candidate.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError("path escapes service data directory")
    return resolved


def relative_artifact(root: Path, path: Path) -> str:
    return contained_path(root, path).relative_to(root.resolve()).as_posix()


def artifact_path(root: Path, relative_path: str) -> Path:
    return contained_path(root, root / relative_path)


def resolve_data_artifact(settings: Settings, stored_path: str, *, require_exists: bool = True) -> Path:
    path = contained_path(settings.data_dir, Path(stored_path))
    if require_exists and not path.exists():
        raise FileNotFoundError("service artifact is unavailable")
    return path


def source_path(root: Path, filename: str) -> Path:
    path = contained_path(root, root / filename)
    if path.suffix.lower() != ".ts":
        raise ValueError("source recording must be a .ts file")
    return path


def resolve_recording_source(settings: Settings, recording: Recording, *, require_exists: bool = True,
                             verify_size: bool = False) -> Path:
    """Resolve a recording from runtime configuration, never its persisted host path."""
    if Path(recording.filename).name != recording.filename:
        raise ValueError("recording filename must not contain path components")
    path = source_path(settings.source_dir, recording.filename)
    if require_exists and not path.is_file():
        raise FileNotFoundError(f"recording source is unavailable: {recording.filename}")
    if verify_size and path.stat().st_size != recording.size_bytes:
        raise ValueError(f"recording source size does not match database identity: {recording.filename}")
    return path
