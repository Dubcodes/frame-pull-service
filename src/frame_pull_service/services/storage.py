from __future__ import annotations

import shutil
import time
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import Settings
from .candidate_triage import cleanup_candidate_preview_cache
from .operations import get_value
from .playback import cleanup_playback_cache
from .source_lifecycle import source_lifecycle_status, sweep_source_lifecycle

_CACHE: dict[str, tuple[float, dict]] = {}
_LAST_RELIEF: dict[str, float] = {}


def _tree_bytes(root: Path) -> int:
    if not root.exists(): return 0
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def storage_metrics(session: Session, settings: Settings, *, refresh: bool = False) -> dict:
    key=str(settings.data_dir.resolve()); cached=_CACHE.get(key)
    if cached and not refresh and time.monotonic()-cached[0] < 300: return cached[1]
    disk=shutil.disk_usage(settings.data_dir)
    categories={
        "canonical_recordings":_tree_bytes(settings.source_dir),
        "interview_evidence":_tree_bytes(settings.data_dir/"interviews"),
        "approved_portraits":_tree_bytes(settings.data_dir/"approved"),
        "playback_cache":_tree_bytes(settings.data_dir/"playback_cache"),
        "candidate_previews":_tree_bytes(settings.data_dir/"candidate_preview_cache"),
        "databases_backups":_tree_bytes(settings.data_dir/"backups")+sum(p.stat().st_size for p in settings.data_dir.glob("*.db*")),
        "logs":_tree_bytes(settings.data_dir/"logs"),
    }
    known={"interviews","approved","playback_cache","candidate_preview_cache","backups","logs"}
    categories["other_managed"] = sum(_tree_bytes(p) for p in settings.data_dir.iterdir() if p.is_dir() and p.name not in known)
    managed=sum(categories.values())
    warning=float(get_value(session,"storage_warning_free_gb",80))*1024**3
    critical=float(get_value(session,"storage_critical_free_gb",40))*1024**3
    cap=float(get_value(session,"max_managed_storage_gb",350))*1024**3
    level="critical" if disk.free<=critical else ("warning" if disk.free<=warning else "normal")
    result={"filesystem":{"total":disk.total,"used":disk.used,"free":disk.free,"percent_used":round(disk.used/disk.total*100,1)},
            "managed_bytes":managed,"categories":categories,"level":level,
            "thresholds":{"warning_free_bytes":int(warning),"critical_free_bytes":int(critical),"max_managed_bytes":int(cap)},
            "managed_cap_exceeded":managed>cap,"cached_seconds":300}
    _CACHE[key]=(time.monotonic(),result);return result


def optional_work_allowed(session: Session, settings: Settings) -> tuple[bool,str | None]:
    value=storage_metrics(session,settings)
    if value["level"]=="critical": return False,"critical filesystem reserve reached"
    if value["managed_cap_exceeded"]: return False,"Frame Pull managed storage cap exceeded"
    return True,None


def relieve_storage_pressure(session: Session, settings: Settings, *, scheduled: bool = False) -> dict:
    key=str(settings.data_dir.resolve()); now=time.monotonic()
    if scheduled and now-_LAST_RELIEF.get(key,0) < 900:
        return {"skipped":True,"reason":"storage pressure maintenance cooldown","cleanup_blocked":True}
    _LAST_RELIEF[key]=now
    before=storage_metrics(session,settings,refresh=True)
    playback=cleanup_playback_cache(session,settings,force=True)
    previews=cleanup_candidate_preview_cache(session,settings,force=True)
    after_caches=storage_metrics(session,settings,refresh=True)
    lifecycle=None
    if after_caches["managed_cap_exceeded"]:
        lifecycle=sweep_source_lifecycle(session,settings,limit=settings.source_lifecycle_sweep_limit)
    after=storage_metrics(session,settings,refresh=True)
    blockers=source_lifecycle_status(session,settings)["counts"] if after["managed_cap_exceeded"] else {}
    return {"before":before,"after":after,"eviction_order":["playback_cache","candidate_preview_cache"],
            "playback":playback,"previews":previews,"source_lifecycle":lifecycle,
            "cleanup_blocked":after["managed_cap_exceeded"],"blockers":blockers}
