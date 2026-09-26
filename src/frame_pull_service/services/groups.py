from __future__ import annotations

import re
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AppearanceGroup, Candidate, Interview, InterviewRevision, Recording


def identity_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def trusted_identity(session: Session, interview: Interview) -> str | None:
    if interview.final_name:
        return interview.final_name
    revision = session.get(InterviewRevision, interview.active_revision_id) if interview.active_revision_id else None
    return revision.trusted_name if revision and revision.trusted_name else None


def sync_groups(session: Session) -> None:
    for interview in session.execute(select(Interview)).scalars():
        name = trusted_identity(session, interview)
        recording = session.get(Recording, interview.recording_id)
        if not name or not recording.race_day_id:
            continue
        key = identity_key(name)
        group = session.execute(select(AppearanceGroup).where(AppearanceGroup.race_day_id == recording.race_day_id, AppearanceGroup.identity_key == key)).scalar_one_or_none()
        if group is None:
            group = AppearanceGroup(race_day_id=recording.race_day_id, identity_key=key, display_name=name)
            session.add(group); session.flush()
        interview.appearance_group_id = group.id
        if interview.final_track and not group.final_track:
            group.final_track, group.location_confidence = interview.final_track, interview.context_confidence or 0.7
        if interview.selected_candidate_id and group.preferred_candidate_id is None:
            group.preferred_candidate_id = interview.selected_candidate_id
    session.commit()


def group_view(session: Session, group: AppearanceGroup) -> dict:
    members = session.execute(select(Interview).where(Interview.appearance_group_id == group.id).order_by(Interview.id)).scalars().all()
    candidates = []
    for item in members:
        revision = session.get(InterviewRevision, item.active_revision_id) if item.active_revision_id else None
        if revision:
            candidates.extend(revision.candidates)
    preferred = session.get(Candidate, group.preferred_candidate_id) if group.preferred_candidate_id else None
    if preferred is None:
        preferred = next((item for item in candidates if item.selected), None) or (candidates[0] if candidates else None)
    fallback_interview = members[0] if members else None
    return {"id": group.id, "race_day_id": group.race_day_id, "name": group.display_name, "interview_ids": [item.id for item in members],
            "interview_count": len(members), "candidate_count": len(candidates), "track": group.final_track,
            "location_confidence": group.location_confidence, "preferred_candidate_id": preferred.id if preferred else None,
            "portrait_url": f"/api/bridge/v1/groups/{group.id}/portrait" if preferred else None,
            "holding_url": f"/api/interviews/{fallback_interview.id}/candidates/{preferred.id}" if preferred else (f"/api/interviews/{fallback_interview.id}/poster" if fallback_interview else None),
            "review_status": "approved" if any(item.review_status.value == "approved" for item in members) else "pending"}


def list_groups(session: Session, race_day_id: int | None = None) -> list[dict]:
    sync_groups(session)
    query = select(AppearanceGroup)
    if race_day_id:
        query = query.where(AppearanceGroup.race_day_id == race_day_id)
    return [group_view(session, group) for group in session.execute(query.order_by(AppearanceGroup.display_name)).scalars()]
