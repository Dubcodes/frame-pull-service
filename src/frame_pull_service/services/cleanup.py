from __future__ import annotations

from pathlib import Path

from ..config import Settings


def cleanup_plan(settings: Settings) -> dict:
    """V1 retention inspection. This deliberately performs no deletion."""
    job_root = settings.data_dir / "jobs"
    candidates = [path for path in job_root.rglob("*") if path.is_file()] if job_root.exists() else []
    return {"dry_run": True, "delete_count": 0, "candidates": [str(path.relative_to(settings.data_dir)) for path in candidates],
            "protected": ["source recordings", "approved portraits", "manifests"]}
