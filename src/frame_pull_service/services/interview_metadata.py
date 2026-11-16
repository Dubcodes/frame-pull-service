from __future__ import annotations

import re
import subprocess
import tempfile
from collections import Counter
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import (CalendarMeeting, Interview, InterviewMetadataAnalysis, InterviewRevision,
                      Recording, ReviewEvent, ReviewStatus)
from .media import extract_frame
from .paths import resolve_data_artifact, resolve_recording_source

RACING_CODES={"thoroughbred","harness","greyhound","unknown"}
ROLES=("CO-TRAINER","TRAINER","JOCKEY","DRIVER","OWNER")
NOISE={"TRACKSIDE","SESSION","RACING","RACE","LIVE","RESULT","PROVISIONAL","PLACINGS","SUNDAY"}


def normalize_code(value: str) -> str:
    value=(value or "unknown").strip().lower()
    if value not in RACING_CODES: raise ValueError("racing_code must be thoroughbred, harness, greyhound, or unknown")
    return value


def _ocr(command: str, image: Path) -> dict:
    result=subprocess.run([command,str(image),"stdout","--psm","6","tsv"],check=True,text=True,capture_output=True)
    lines={}
    for raw in result.stdout.splitlines()[1:]:
        fields=raw.split("\t",11)
        if len(fields)<12 or not fields[11].strip(): continue
        try: conf=float(fields[10])
        except ValueError: continue
        key=tuple(fields[i] for i in (1,2,3,4));lines.setdefault(key,[]).append((fields[11].strip(),conf))
    values=[]
    for words in lines.values():
        text=" ".join(word for word,_ in words); valid=[conf for _,conf in words if conf>=0]
        values.append({"text":text,"confidence":round(sum(valid)/len(valid),2) if valid else 0})
    return {"lines":values,"text":" | ".join(v["text"] for v in values)}


def _clean_name(text: str) -> str | None:
    words=re.findall(r"[A-Za-z][A-Za-z'’-]+",text.upper())
    if not 2<=len(words)<=4 or any(word in NOISE or word in ROLES for word in words): return None
    if any(len(word)<2 for word in words): return None
    return " ".join(word.title() for word in words)


def name_consensus(observations: list[dict]) -> dict:
    candidates=[]
    for observation_index,obs in enumerate(observations):
        for line in obs.get("lines",[]):
            name=_clean_name(line.get("text", ""))
            if name and float(line.get("confidence") or 0)>=45: candidates.append((name,float(line["confidence"]),observation_index))
    if not candidates:return {"trusted":None,"proposed":None,"confidence":0,"support":0,"reason":"no_readable_name"}
    groups=[]
    for name,conf,observation_index in candidates:
        group=next((g for g in groups if SequenceMatcher(None,g[0][0].lower(),name.lower()).ratio()>=.84),None)
        if group is None:group=[];groups.append(group)
        group.append((name,conf,observation_index))
    best=max(groups,key=lambda g:(len({x[2] for x in g}),sum(x[1] for x in g)))
    proposed=Counter(x[0] for x in best).most_common(1)[0][0];confidence=round(sum(x[1] for x in best)/len(best),2)
    support=len({x[2] for x in best})
    trusted=proposed if support>=2 and confidence>=60 else None
    return {"trusted":trusted,"proposed":proposed,"confidence":confidence,"support":support,
            "reason":"multi_frame_consensus" if trusted else "insufficient_consensus"}


def _role(observations: list[dict]) -> str | None:
    hits=[role for obs in observations for role in ROLES if role in obs.get("text","").upper()]
    return Counter(hits).most_common(1)[0][0] if hits else None


def _track_and_code(session: Session, recording: Recording, observations: list[dict]) -> tuple[str|None,float|None,str,dict]:
    text=" ".join(obs.get("text","") for obs in observations).upper();compact=re.sub(r"[^A-Z0-9]","",text)
    meetings=session.execute(select(CalendarMeeting).where(CalendarMeeting.race_day_id==recording.race_day_id)).scalars().all() if recording.race_day_id else []
    matches=[];tokens=re.findall(r"[A-Z0-9]+",text)
    for meeting in meetings:
        target=re.sub(r"[^A-Z0-9]","",meeting.track.upper())
        visual_candidates=["".join(tokens[start:start+width]) for width in (1,2,3) for start in range(max(0,len(tokens)-width+1))]
        if target and (target in compact or any(SequenceMatcher(None,target,value).ratio()>=.84 for value in visual_candidates)):
            matches.append(meeting)
    track=matches[0].track if len({m.track for m in matches})==1 else None
    confidence=.9 if track else None
    explicit="harness" if "HARNESS" in text else ("greyhound" if "GREYHOUND" in text else ("thoroughbred" if "THOROUGHBRED" in text else "unknown"))
    if explicit!="unknown": return track,confidence,explicit,{"source":"broadcast_text","text":explicit.upper()}
    known={m.racing_code for m in matches if m.racing_code in RACING_CODES-{"unknown"}}
    code=next(iter(known)) if len(known)==1 else "unknown"
    return track,confidence,code,{"source":"calendar_meeting_visual_match" if code!="unknown" else "none","track":track}


def analysis_view(row: InterviewMetadataAnalysis | None) -> dict | None:
    if row is None:return None
    return {key:getattr(row,key) for key in ("id","interview_id","created_at","analysis_version","analysis_basis","source_timestamps",
        "name_observations","name_consensus","proposed_name","trusted_name","name_confidence","role_observations","proposed_role",
        "track_observations","proposed_track","track_confidence","racing_code","racing_code_evidence","technical_warnings","applied_fields")}


def analyze_interview(session: Session, settings: Settings, interview: Interview, *, analyzer=_ocr, frame_extractor=extract_frame,
                      apply_safe: bool=True) -> dict:
    recording=session.get(Recording,interview.recording_id);revision=session.get(InterviewRevision,interview.active_revision_id) if interview.active_revision_id else None
    if not recording or not revision: raise ValueError("Interview source evidence is unavailable")
    try: source=resolve_recording_source(settings,recording,verify_size=True);basis="canonical_source";base=0.0
    except (ValueError,FileNotFoundError,OSError):
        source=resolve_data_artifact(settings,revision.clip_path);basis="review_clip";base=revision.clip_start
    duration=max(.5,interview.source_end-interview.source_start); offsets=(.08,.28,.55,.82)
    source_times=[interview.source_start+duration*x for x in offsets];name_obs=[];role_obs=[];track_obs=[];warnings=[]
    with tempfile.TemporaryDirectory(dir=settings.data_dir) as tmp:
        for index,source_time in enumerate(source_times):
            frame=Path(tmp)/f"frame_{index}.jpg";frame_extractor("ffmpeg",source,frame,max(0,source_time-base))
            with Image.open(frame) as image:
                w,h=image.size;name_crop=Path(tmp)/f"name_{index}.jpg";track_crop=Path(tmp)/f"track_{index}.jpg"
                image.crop((0,int(h*.5),int(w*.82),h)).save(name_crop,"JPEG",quality=92)
                image.save(track_crop,"JPEG",quality=90)
            try:
                lower=analyzer(settings.legacy_tesseract_cmd or "tesseract",name_crop);full=analyzer(settings.legacy_tesseract_cmd or "tesseract",track_crop)
            except subprocess.SubprocessError as exc: warnings.append(f"ocr frame {index} failed: {type(exc).__name__}");continue
            name_obs.append({"source_time":source_time,**lower});role_obs.append({"source_time":source_time,**lower});track_obs.append({"source_time":source_time,**full})
    consensus=name_consensus(name_obs);role=_role(role_obs);track,track_conf,code,code_evidence=_track_and_code(session,recording,track_obs)
    row=InterviewMetadataAnalysis(interview_id=interview.id,analysis_basis=basis,source_timestamps=source_times,name_observations=name_obs,
        name_consensus=consensus,proposed_name=consensus["proposed"],trusted_name=consensus["trusted"],name_confidence=consensus["confidence"],
        role_observations=role_obs,proposed_role=role,track_observations=track_obs,proposed_track=track,track_confidence=track_conf,
        racing_code=code,racing_code_evidence=code_evidence,technical_warnings=warnings,applied_fields=[])
    session.add(row);session.flush();applied=[]
    safe=interview.review_status==ReviewStatus.PENDING and interview.export_state!="exported"
    if apply_safe and safe:
        if not interview.final_name and consensus["trusted"] and interview.identity_source not in {"manual","operator"}:
            interview.final_name=consensus["trusted"];interview.identity_source="metadata_reanalysis";applied.append("final_name")
        if not interview.final_role and role:interview.final_role=role;applied.append("final_role")
        if not interview.final_track and track:interview.final_track=track;applied.append("final_track")
        if interview.racing_code=="unknown" and code!="unknown":
            interview.racing_code=code;interview.racing_code_source=code_evidence.get("source");interview.racing_code_confidence=.9;applied.append("racing_code")
    row.applied_fields=applied
    session.add(ReviewEvent(interview_id=interview.id,event_type="metadata_reanalysis",payload={"analysis_id":row.id,"applied_fields":applied,"safe_auto_apply":safe}))
    session.commit()
    if applied:
        from .context import refresh_race_day_contexts
        from .groups import sync_groups
        if recording.race_day_id:refresh_race_day_contexts(session,recording.race_day_id,settings.broadcast_timezone)
        sync_groups(session)
    return analysis_view(row)


def latest_analysis(session: Session, interview_id: int) -> InterviewMetadataAnalysis | None:
    return session.execute(select(InterviewMetadataAnalysis).where(InterviewMetadataAnalysis.interview_id==interview_id).order_by(InterviewMetadataAnalysis.id.desc())).scalars().first()


def bulk_candidates(session: Session, limit: int=25) -> list[Interview]:
    rows=session.execute(select(Interview).order_by(Interview.id.desc())).scalars().all()
    unresolved=[row for row in rows if (not row.final_name or not row.final_track or not row.final_role)]
    def priority(row: Interview) -> tuple:
        revision=session.get(InterviewRevision,row.active_revision_id) if row.active_revision_id else None
        return (int(not row.final_name),int(not row.final_track or row.context_confidence in {None,0}),
                int(not row.final_role),int(bool(revision and revision.ocr_name_candidate and not revision.trusted_name)),row.id)
    return sorted(unresolved,key=priority,reverse=True)[:max(1,min(limit,100))]
