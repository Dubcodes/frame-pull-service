from __future__ import annotations

import re
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AppearanceGroup, Candidate, Interview, InterviewRevision, Recording, ReviewEvent, ReviewStatus
from .review import refresh_manifest
from .context import resolve_interview_context


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
    member_by_revision = {item.active_revision_id: item for item in members if item.active_revision_id}
    preferred = session.get(Candidate, group.preferred_candidate_id) if group.preferred_candidate_id else None
    if preferred is not None and preferred.revision_id not in member_by_revision: preferred = None
    if preferred is None:
        selected_ids = {item.selected_candidate_id for item in members if item.selected_candidate_id}
        preferred = next((item for item in candidates if item.id in selected_ids), None)
    if preferred is None and candidates:
        preferred = min(candidates, key=lambda item: (item.rank, item.id))
    preferred_owner = member_by_revision.get(preferred.revision_id) if preferred else None
    approved = [item for item in members if item.review_status == ReviewStatus.APPROVED and item.selected_candidate_id]
    approved_candidate_ids = {item.selected_candidate_id for item in approved}
    bridge_candidate = preferred if preferred and preferred.id in approved_candidate_ids else next(
        (item for item in candidates if item.id in approved_candidate_ids), None
    )
    fallback_interview = members[0] if members else None
    roles = sorted({item.final_role for item in members if item.final_role})
    member_contexts = [resolve_interview_context(session, item) for item in members]
    strong_tracks = {context["resolved_track"] for context in member_contexts if context.get("resolved_track") and context.get("confidence") in {"high", "medium"}}
    human_tracks = {item.final_track for item in members if item.final_track}
    candidate_tracks = human_tracks or strong_tracks
    if len(candidate_tracks) > 1:
        location, location_state = None, "conflict"
    elif candidate_tracks:
        location, location_state = next(iter(candidate_tracks)), "resolved"
    else:
        location, location_state = None, "unknown"
    export_state = "exported" if approved and all(item.export_state == "exported" for item in approved) else "pending"
    return {"id": group.id, "race_day_id": group.race_day_id, "name": group.display_name, "interview_ids": [item.id for item in members],
            "interview_count": len(members), "candidate_count": len(candidates), "track": location,
            "location_state": location_state, "location_confidence": group.location_confidence, "preferred_candidate_id": preferred.id if preferred else None,
            "portrait_url": f"/api/bridge/v1/groups/{group.id}/portrait" if bridge_candidate else None,
            "bridge_candidate_id": bridge_candidate.id if bridge_candidate else None,
            "holding_url": f"/api/interviews/{preferred_owner.id}/candidates/{preferred.id}" if preferred and preferred_owner else (f"/api/interviews/{fallback_interview.id}/poster" if fallback_interview else None),
            "review_status": "approved" if approved else "pending", "export_state": export_state,
            "roles": roles, "interviews": [{"id": item.id, "role": item.final_role, "track": item.final_track,
            "source_start": item.source_start, "source_end": item.source_end, "review_status": item.review_status.value,
            "candidate_count": len(session.get(InterviewRevision, item.active_revision_id).candidates) if item.active_revision_id else 0,
            "calendar_context": context}
            for item, context in zip(members, member_contexts)]}


def list_groups(session: Session, race_day_id: int | None = None) -> list[dict]:
    sync_groups(session)
    query = select(AppearanceGroup)
    if race_day_id:
        query = query.where(AppearanceGroup.race_day_id == race_day_id)
    return [group_view(session, group) for group in session.execute(query.order_by(AppearanceGroup.display_name)).scalars()]


def acknowledge_group_export(session: Session, group: AppearanceGroup) -> dict:
    """Mark only approved evidence as exported after a downstream commit succeeds."""
    members = session.execute(select(Interview).where(Interview.appearance_group_id == group.id)).scalars().all()
    approved = [item for item in members if item.review_status == ReviewStatus.APPROVED and item.selected_candidate_id]
    if not approved:
        raise ValueError("group has no approved portrait to export")
    for item in approved:
        if item.export_state != "exported":
            item.export_state = "exported"
            session.add(ReviewEvent(interview_id=item.id, event_type="export_acknowledged", payload={"scope": "appearance_group", "group_id": group.id}))
            refresh_manifest(session, item)
    group.export_state = "exported"
    session.commit()
    return group_view(session, group)


def acknowledge_interview_export(session: Session, interview: Interview) -> dict:
    if interview.review_status != ReviewStatus.APPROVED or not interview.selected_candidate_id:
        raise ValueError("only approved interviews with a selected portrait can be exported")
    if interview.export_state != "exported":
        interview.export_state = "exported"
        session.add(ReviewEvent(interview_id=interview.id, event_type="export_acknowledged", payload={"scope": "interview"}))
        refresh_manifest(session, interview)
        session.commit()
    return {"id": interview.id, "export_state": interview.export_state}
