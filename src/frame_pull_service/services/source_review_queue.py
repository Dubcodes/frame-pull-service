from __future__ import annotations

import shutil
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import CalendarMeeting, Interview, RaceDay, Recording, RecordingMediaAudit
from .detector_candidates import detector_candidates_for_race_day
from .operations import get_value
from .paths import resolve_recording_source
from .source_lifecycle import deletion_eligibility


def source_review_progress(session: Session, settings: Settings, race_day_id: int) -> dict:
    candidates=detector_candidates_for_race_day(session,settings,race_day_id)
    counts={state:sum(row["triage_state"]==state for row in candidates) for state in ("unreviewed","not_interview","recovered","deferred")}
    return {"total":len(candidates),"reviewed":counts["not_interview"]+counts["recovered"],
            "remaining":counts["unreviewed"]+counts["deferred"],**counts}


def source_review_queue(session: Session, settings: Settings) -> dict:
    rows=[]; blocked_bytes=0; solely_blocked_bytes=0; oldest=None
    retention_hours = int(get_value(session,"source_retention_hours",168))
    for day in session.execute(select(RaceDay).where(RaceDay.source_review_state != "review_complete")).scalars():
        records=session.execute(select(Recording).where(Recording.race_day_id==day.id).order_by(Recording.recording_started_at,Recording.id)).scalars().all()
        available=[]
        for record in records:
            try: resolve_recording_source(settings,record,verify_size=True);available.append(record)
            except (ValueError,FileNotFoundError,OSError): pass
        if not available: continue
        progress=source_review_progress(session,settings,day.id)
        starts=[row.recording_started_at for row in available if row.recording_started_at]
        earliest=min(starts) if starts else None
        if earliest and (oldest is None or earliest<oldest): oldest=earliest
        health=[]
        for record in available:
            audit=session.execute(select(RecordingMediaAudit).where(RecordingMediaAudit.recording_id==record.id).order_by(RecordingMediaAudit.id.desc())).scalars().first()
            health.append(audit.classification if audit else "not_checked")
            blocked_bytes+=record.size_bytes
            eligibility=deletion_eligibility(session,settings,record,deletion_enabled_override=True)
            other={key:value for key,value in eligibility["checks"].items() if key!="race_day_source_review_complete"}
            solely_blocked_bytes+=record.size_bytes if all(other.values()) else 0
        interviews=session.execute(select(Interview).where(Interview.recording_id.in_([r.id for r in records]))).scalars().all()
        auto=sum(item.bounds_basis!="manual_operator" for item in interviews); recovered=len(interviews)-auto
        retained=sum(r.size_bytes for r in available); duration=sum(r.duration_seconds or 0 for r in available)
        age_hours=((datetime.utcnow()-earliest).total_seconds()/3600 if earliest else None)
        past_retention=bool(age_hours is not None and age_hours >= retention_hours)
        meetings=[row.track for row in session.execute(select(CalendarMeeting).where(CalendarMeeting.race_day_id==day.id)).scalars()]
        rows.append({"race_day_id":day.id,"date":day.race_date.isoformat(),"label":day.label or day.race_date.isoformat(),
            "source_review":day.source_review_state,"meetings":meetings,"stored_bytes":retained,"stored_duration_seconds":duration,
            "automated_interviews":auto,"recovered_interviews":recovered,"candidate_progress":progress,
            "health":{"healthy":health.count("healthy"),"warning":health.count("warning"),"failed":health.count("failed"),"not_checked":health.count("not_checked")},
            "protected_sources":sum(r.source_protected for r in available),"source_count":len(available),
            "oldest_source":earliest,"age_hours":age_hours,
            "past_retention":past_retention})
    rows.sort(key=lambda row:(0 if row["age_hours"] is not None and row["age_hours"]>=retention_hours else 1,
                              0 if row["source_review"]=="in_progress" else 1,-(row["age_hours"] or 0)))
    return {"race_days_awaiting_review":len(rows),"oldest_unreviewed_race_day":oldest,
            "retained_bytes_source_review_incomplete":blocked_bytes,
            "bytes_blocked_only_by_source_review":solely_blocked_bytes,
            "free_recording_bytes":shutil.disk_usage(settings.source_dir).free,"race_days":rows}
