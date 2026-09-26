from __future__ import annotations

import json
from pathlib import Path


def write_manifest(path: Path, manifest: dict) -> None:
    """Replace a manifest atomically so a failed write preserves prior evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    temporary.replace(path)
