from __future__ import annotations

from pathlib import Path


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


def source_path(root: Path, filename: str) -> Path:
    path = contained_path(root, root / filename)
    if path.suffix.lower() != ".ts":
        raise ValueError("source recording must be a .ts file")
    return path
