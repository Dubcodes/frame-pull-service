from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from . import __version__
from .config import Settings, get_settings
from .db import create_db_engine, init_db, make_session_factory
from .models import AppearanceGroup, Candidate, Interview, InterviewRevision, Job, JobStatus, RaceDay, Recording, ReviewEvent, ReviewStatus
from .schemas import CaptureFrameRequest, RejectRequest, ReviewPatch, SelectCandidateRequest
from .services.discovery import discover_recordings
from .services.cleanup import cleanup_plan
from .services.paths import artifact_path
from .services.queue import cancel_queued, queue_recording, queue_selected
from .services.review import approve, capture_frame, reject, save_review, select_candidate, refresh_manifest
from .services.worker import Worker
from .services.operations import get_value, seed_settings, settings_view, update_settings
from .services.race_days import backfill_race_days, list_race_days, race_day_summary
from .services.groups import acknowledge_group_export, acknowledge_interview_export, group_view, list_groups, sync_groups
from .services.source_lifecycle import deletion_eligibility
from .services.calendar import LoveRacingCalendarProvider, refresh_race_day
from .services.context import resolve_interview_context
from .services.orchestrator import planned_recording_window


class ServiceState:
    def __init__(self, settings: Settings, worker_enabled: bool):
        settings.ensure_data_dirs(); self.settings = settings; self.engine = create_db_engine(settings); init_db(self.engine)
        self.sessions = make_session_factory(self.engine)
        self.worker = Worker(self.sessions, settings) if worker_enabled else None
        self.watcher_task: asyncio.Task | None = None


def serialize_recording(recording: Recording) -> dict:
    return {"id": recording.id, "filename": recording.filename, "size_bytes": recording.size_bytes,
            "status": recording.status.value, "historical": recording.historical, "stable_at": recording.stable_at,
            "race_day_id": recording.race_day_id, "started_at": recording.recording_started_at,
            "stopped_at": recording.recording_stopped_at, "duration_seconds": recording.duration_seconds,
            "is_closed": recording.is_closed}


def serialize_interview(session, interview: Interview) -> dict:
    revision = session.get(InterviewRevision, interview.active_revision_id) if interview.active_revision_id else None
    candidate = session.get(Candidate, interview.selected_candidate_id) if interview.selected_candidate_id else None
    return {"id": interview.id, "interview_id": interview.interview_key, "recording": session.get(Recording, interview.recording_id).filename,
            "source_start": interview.source_start, "source_end": interview.source_end, "review_status": interview.review_status.value,
            "export_state": interview.export_state, "final_name": interview.final_name, "final_role": interview.final_role,
            "final_track": interview.final_track, "identity_source": interview.identity_source,
            "trusted_name": revision.trusted_name if revision else None, "ocr_name_candidate": revision.ocr_name_candidate if revision else None,
            "ocr_confidence": revision.ocr_confidence if revision else None, "name_trust_reason": revision.name_trust_reason if revision else None,
            "clip_duration": round(revision.clip_end - revision.clip_start, 3) if revision else 0,
            "has_portrait": candidate is not None, "poster_url": f"/api/interviews/{interview.id}/poster" if revision else None,
            "calendar_context": resolve_interview_context(session, interview)}


def create_app(settings: Settings | None = None, worker_enabled: bool = False) -> FastAPI:
    settings = settings or get_settings(); state = ServiceState(settings, worker_enabled)
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "web" / "templates"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with state.sessions() as session:
            seed_settings(session, state.settings); backfill_race_days(session); discover_recordings(session, state.settings)
        if state.worker: state.worker.start()
        async def watch() -> None:
            while True:
                await asyncio.sleep(state.settings.discovery_interval_seconds)
                try:
                    with state.sessions() as session: discover_recordings(session, state.settings)
                except Exception:
                    # Discovery is best-effort; a temporary filesystem error must
                    # not take down review or queue operations.
                    continue
        state.watcher_task = asyncio.create_task(watch())
        yield
        if state.watcher_task: state.watcher_task.cancel()
        if state.worker: state.worker.stop()
        state.engine.dispose()

    app = FastAPI(title="Frame Pull Service", version=__version__, lifespan=lifespan)
    app.state.service = state
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "web" / "static")), name="static")

    @app.get("/api/health")
    def health() -> dict:
        with state.sessions() as session:
            active = session.execute(select(Job).where(Job.status == JobStatus.PROCESSING)).scalars().all()
            paused = bool(get_value(session, "processing_paused", False))
            return {"version": __version__, "database": "ok", "watcher_active": state.watcher_task is not None,
                    "worker_active": bool(state.worker and state.worker.active), "processing_paused": paused,
                    "current_job": active[0].id if active else None, "active_jobs": [{"id": item.id, "stage": item.progress_stage, "percent": item.progress_percent} for item in active],
                    "queue_depth": session.query(Job).filter(Job.status == JobStatus.QUEUED).count(),
                    "max_concurrent_recordings": get_value(session, "max_concurrent_recordings", 1),
                    "ffmpeg_available": True, "ffprobe_available": True, "legacy_engine_available": state.settings.legacy_python.exists()}

    @app.get("/api/recordings")
    def recordings() -> list[dict]:
        with state.sessions() as session: return [serialize_recording(item) for item in session.execute(select(Recording).order_by(Recording.id.desc())).scalars()]

    @app.get("/api/recordings/{recording_id}")
    def recording(recording_id: int) -> dict:
        with state.sessions() as session:
            item = session.get(Recording, recording_id)
            if not item: raise HTTPException(404, "recording not found")
            data = serialize_recording(item); data["jobs"] = [{"id": job.id, "status": job.status.value, "stage": job.progress_stage, "percent": job.progress_percent, "error": job.error_summary} for job in item.jobs]; return data

    @app.post("/api/recordings/discover")
    def discover() -> dict:
        with state.sessions() as session: return discover_recordings(session, state.settings)

    @app.post("/api/recordings/{recording_id}/queue")
    def queue(recording_id: int) -> dict:
        with state.sessions() as session:
            item = session.get(Recording, recording_id)
            if not item: raise HTTPException(404, "recording not found")
            job = queue_recording(session, item); session.commit(); return {"job_id": job.id, "status": job.status.value}

    @app.post("/api/recordings/{recording_id}/reprocess")
    def reprocess(recording_id: int) -> dict:
        return queue(recording_id)

    @app.post("/api/recordings/bulk-queue")
    def bulk_queue(body: dict) -> dict:
        with state.sessions() as session:
            return queue_selected(session, [int(item) for item in body.get("recording_ids", [])], bool(body.get("reprocess", False)))

    @app.post("/api/recordings/bulk-cancel")
    def bulk_cancel(body: dict) -> dict:
        with state.sessions() as session:
            return cancel_queued(session, [int(item) for item in body.get("recording_ids", [])])

    @app.get("/api/operations/settings")
    def operation_settings() -> dict:
        with state.sessions() as session: return settings_view(session)

    @app.put("/api/operations/settings")
    def save_operation_settings(body: dict) -> dict:
        with state.sessions() as session:
            try: return update_settings(session, body)
            except ValueError as exc: raise HTTPException(422, str(exc))

    @app.post("/api/operations/pause")
    def pause_processing() -> dict:
        with state.sessions() as session:
            values = update_settings(session, {"processing_paused": True})
            active = session.query(Job).filter(Job.status == JobStatus.PROCESSING).count()
            return {"state": "pausing" if active else "paused", "active_jobs": active, "settings": values}

    @app.post("/api/operations/resume")
    def resume_processing() -> dict:
        with state.sessions() as session: return {"state": "active", "settings": update_settings(session, {"processing_paused": False})}

    @app.get("/api/race-days")
    def race_days() -> list[dict]:
        with state.sessions() as session: return list_race_days(session)

    @app.get("/api/race-days/{race_day_id}")
    def race_day(race_day_id: int) -> dict:
        with state.sessions() as session:
            item = session.get(RaceDay, race_day_id)
            if not item: raise HTTPException(404, "race day not found")
            return race_day_summary(session, item)

    @app.get("/api/race-days/{race_day_id}/calendar")
    def race_day_calendar(race_day_id: int) -> dict:
        with state.sessions() as session:
            item = session.get(RaceDay, race_day_id)
            if not item: raise HTTPException(404, "race day not found")
            return {"race_day": race_day_summary(session, item), "recording_plan": planned_recording_window(session, item).__dict__}

    @app.post("/api/race-days/{race_day_id}/calendar/refresh")
    def refresh_calendar(race_day_id: int) -> dict:
        with state.sessions() as session:
            item = session.get(RaceDay, race_day_id)
            if not item: raise HTTPException(404, "race day not found")
            return refresh_race_day(session, item, LoveRacingCalendarProvider())

    @app.get("/api/recordings/{recording_id}/deletion-eligibility")
    def source_eligibility(recording_id: int) -> dict:
        with state.sessions() as session:
            item = session.get(Recording, recording_id)
            if not item: raise HTTPException(404, "recording not found")
            return deletion_eligibility(session, state.settings, item)

    @app.get("/api/interviews")
    def interviews(status: ReviewStatus | None = None, export_state: str | None = None) -> list[dict]:
        with state.sessions() as session:
            query = select(Interview)
            if status: query = query.where(Interview.review_status == status)
            if export_state: query = query.where(Interview.export_state == export_state)
            return [serialize_interview(session, item) for item in session.execute(query.order_by(Interview.id.desc())).scalars()]

    @app.get("/api/appearance-groups")
    def appearance_groups(race_day_id: int | None = None) -> list[dict]:
        with state.sessions() as session: return list_groups(session, race_day_id)

    @app.post("/api/appearance-groups/{group_id}/select-candidate")
    def select_group_candidate(group_id: int, body: dict) -> dict:
        with state.sessions() as session:
            group = session.get(AppearanceGroup, group_id); candidate = session.get(Candidate, int(body.get("candidate_id", 0)))
            if not group or not candidate: raise HTTPException(404, "group or candidate not found")
            member_ids = {item.id for item in session.execute(select(Interview).where(Interview.appearance_group_id == group.id)).scalars()}
            revision = session.get(InterviewRevision, candidate.revision_id)
            if not revision or revision.interview_id not in member_ids: raise HTTPException(400, "candidate is not part of this group")
            group.preferred_candidate_id = candidate.id; session.commit(); return group_view(session, group)

    def get_interview(session, interview_id: int) -> Interview:
        item = session.get(Interview, interview_id)
        if not item: raise HTTPException(404, "interview not found")
        return item

    @app.get("/api/interviews/{interview_id}")
    def interview(interview_id: int) -> dict:
        with state.sessions() as session: return serialize_interview(session, get_interview(session, interview_id))

    @app.get("/api/interviews/{interview_id}/manifest")
    def manifest(interview_id: int) -> dict:
        with state.sessions() as session:
            item = get_interview(session, interview_id); revision = session.get(InterviewRevision, item.active_revision_id)
            return revision.manifest

    def media(interview_id: int, kind: str, candidate_id: int | None = None):
        with state.sessions() as session:
            item = get_interview(session, interview_id); revision = session.get(InterviewRevision, item.active_revision_id)
            if kind == "clip": path = Path(revision.clip_path); media_type = "video/mp4"
            elif kind == "poster": path = Path(revision.poster_path); media_type = "image/jpeg"
            elif kind == "portrait":
                if not item.selected_candidate_id: raise HTTPException(404, "no selected portrait")
                approved = state.settings.data_dir / "approved" / f"{item.interview_key}.jpg"
                path = approved if item.review_status == ReviewStatus.APPROVED and approved.exists() else Path(session.get(Candidate, item.selected_candidate_id).image_path); media_type = "image/jpeg"
            else:
                candidate = session.get(Candidate, candidate_id)
                if not candidate or candidate.revision_id != revision.id: raise HTTPException(404, "candidate not found")
                path = Path(candidate.image_path); media_type = "image/jpeg"
            try: artifact_path(state.settings.data_dir, str(path.resolve().relative_to(state.settings.data_dir.resolve())))
            except (ValueError, OSError): raise HTTPException(404, "artifact unavailable")
            if not path.exists(): raise HTTPException(404, "artifact unavailable")
            return FileResponse(path, media_type=media_type)

    @app.get("/api/interviews/{interview_id}/clip")
    def clip(interview_id: int): return media(interview_id, "clip")
    @app.get("/api/interviews/{interview_id}/poster")
    def poster(interview_id: int): return media(interview_id, "poster")
    @app.get("/api/interviews/{interview_id}/portrait")
    def portrait(interview_id: int): return media(interview_id, "portrait")
    @app.get("/api/interviews/{interview_id}/candidates/{candidate_id}")
    def candidate(interview_id: int, candidate_id: int): return media(interview_id, "candidate", candidate_id)
    @app.get("/api/interviews/{interview_id}/candidates")
    def candidates(interview_id: int) -> list[dict]:
        with state.sessions() as session:
            item=get_interview(session, interview_id); revision=session.get(InterviewRevision, item.active_revision_id)
            return [{"id": c.id, "rank": c.rank, "source_timestamp": c.source_timestamp, "source": c.source, "selected": c.selected, "url": f"/api/interviews/{item.id}/candidates/{c.id}"} for c in revision.candidates]

    @app.post("/api/interviews/{interview_id}/select-candidate")
    def choose(interview_id: int, body: SelectCandidateRequest) -> dict:
        with state.sessions() as session:
            item=get_interview(session, interview_id); candidate=session.get(Candidate, body.candidate_id)
            if not candidate: raise HTTPException(404, "candidate not found")
            try: select_candidate(session, item, candidate); session.commit()
            except ValueError as exc: raise HTTPException(400, str(exc))
            return serialize_interview(session, item)

    @app.post("/api/interviews/{interview_id}/capture-frame")
    def capture(interview_id: int, body: CaptureFrameRequest) -> dict:
        with state.sessions() as session:
            try: candidate=capture_frame(session, state.settings, get_interview(session, interview_id), body.clip_time); session.commit()
            except ValueError as exc: raise HTTPException(400, str(exc))
            return {"candidate_id": candidate.id, "source_timestamp": candidate.source_timestamp}

    @app.patch("/api/interviews/{interview_id}/review")
    def patch_review(interview_id: int, body: ReviewPatch) -> dict:
        with state.sessions() as session:
            item=get_interview(session, interview_id); save_review(session, item, body.model_dump()); session.commit(); return serialize_interview(session, item)

    @app.post("/api/interviews/{interview_id}/approve")
    def approve_route(interview_id: int) -> dict:
        with state.sessions() as session:
            try: path=approve(session, state.settings, get_interview(session, interview_id)); session.commit()
            except ValueError as exc: raise HTTPException(400, str(exc))
            return {"approved": True, "portrait_url": f"/api/interviews/{interview_id}/portrait", "artifact": path.name}

    @app.post("/api/interviews/{interview_id}/reject")
    def reject_route(interview_id: int, body: RejectRequest) -> dict:
        with state.sessions() as session:
            item=get_interview(session, interview_id); reject(session, item, body.reason); session.commit(); return {"rejected": True}

    @app.post("/api/interviews/{interview_id}/mark-exported")
    def mark_exported(interview_id: int) -> dict:
        with state.sessions() as session:
            try: return acknowledge_interview_export(session, get_interview(session, interview_id))
            except ValueError as exc: raise HTTPException(409, str(exc))

    @app.get("/api/admin/cleanup-plan")
    def cleanup_dry_run() -> dict:
        return cleanup_plan(state.settings)

    def require_bridge(authorization: str | None = Header(default=None)) -> None:
        token = state.settings.bridge_token
        if token and authorization != f"Bearer {token}":
            raise HTTPException(401, "bridge authentication required")

    @app.get("/api/bridge/v1/health", dependencies=[Depends(require_bridge)])
    def bridge_health() -> dict:
        return {"service": "frame-pull", "version": 1, "status": "ok"}

    @app.get("/api/bridge/v1/race-days", dependencies=[Depends(require_bridge)])
    def bridge_race_days() -> list[dict]:
        with state.sessions() as session: return list_race_days(session)

    @app.get("/api/bridge/v1/race-days/{race_day_id}", dependencies=[Depends(require_bridge)])
    def bridge_race_day(race_day_id: int): return race_day(race_day_id)

    @app.get("/api/bridge/v1/people", dependencies=[Depends(require_bridge)])
    def bridge_people(race_day_id: int | None = None, review_status: str | None = None, export_state: str | None = None) -> list[dict]:
        with state.sessions() as session:
            rows = list_groups(session, race_day_id)
            if review_status: rows = [item for item in rows if item["review_status"] == review_status]
            if export_state: rows = [item for item in rows if item["export_state"] == export_state]
            return rows

    @app.get("/api/bridge/v1/people/{group_id}", dependencies=[Depends(require_bridge)])
    def bridge_person(group_id: int) -> dict:
        with state.sessions() as session:
            group = session.get(AppearanceGroup, group_id)
            if not group: raise HTTPException(404, "group not found")
            return group_view(session, group)

    @app.get("/api/bridge/v1/interviews", dependencies=[Depends(require_bridge)])
    def bridge_interviews(review_status: ReviewStatus | None = None, export_state: str | None = None, ungrouped_only: bool = False) -> list[dict]:
        with state.sessions() as session:
            query = select(Interview)
            if review_status: query = query.where(Interview.review_status == review_status)
            if export_state: query = query.where(Interview.export_state == export_state)
            if ungrouped_only: query = query.where(Interview.appearance_group_id.is_(None))
            return [serialize_interview(session, item) for item in session.execute(query.order_by(Interview.id.desc())).scalars()]

    @app.get("/api/bridge/v1/interviews/{interview_id}", dependencies=[Depends(require_bridge)])
    def bridge_interview(interview_id: int): return interview(interview_id)

    @app.get("/api/bridge/v1/interviews/{interview_id}/portrait", dependencies=[Depends(require_bridge)])
    def bridge_interview_portrait(interview_id: int):
        with state.sessions() as session:
            item = get_interview(session, interview_id)
            if item.review_status != ReviewStatus.APPROVED or not item.selected_candidate_id:
                raise HTTPException(404, "approved interview portrait not found")
            return media(interview_id, "portrait")

    @app.get("/api/bridge/v1/candidates/{candidate_id}/image", dependencies=[Depends(require_bridge)])
    def bridge_candidate_image(candidate_id: int):
        with state.sessions() as session:
            candidate = session.get(Candidate, candidate_id)
            if not candidate: raise HTTPException(404, "candidate not found")
            revision = session.get(InterviewRevision, candidate.revision_id)
            return media(revision.interview_id, "candidate", candidate.id)

    @app.get("/api/bridge/v1/groups/{group_id}/portrait", dependencies=[Depends(require_bridge)])
    def bridge_group_portrait(group_id: int):
        with state.sessions() as session:
            group = session.get(AppearanceGroup, group_id)
            if not group: raise HTTPException(404, "group portrait not found")
            view = group_view(session, group)
            if not view["preferred_candidate_id"]: raise HTTPException(404, "group portrait not found")
            candidate = session.get(Candidate, view["preferred_candidate_id"]); revision = session.get(InterviewRevision, candidate.revision_id)
            return media(revision.interview_id, "candidate", candidate.id)

    @app.post("/api/bridge/v1/groups/{group_id}/mark-exported", dependencies=[Depends(require_bridge)])
    def bridge_mark_exported(group_id: int) -> dict:
        with state.sessions() as session:
            group = session.get(AppearanceGroup, group_id)
            if not group: raise HTTPException(404, "group not found")
            try: return acknowledge_group_export(session, group)
            except ValueError as exc: raise HTTPException(409, str(exc))

    @app.post("/api/bridge/v1/interviews/{interview_id}/mark-exported", dependencies=[Depends(require_bridge)])
    def bridge_mark_interview_exported(interview_id: int) -> dict:
        with state.sessions() as session:
            try: return acknowledge_interview_export(session, get_interview(session, interview_id))
            except ValueError as exc: raise HTTPException(409, str(exc))

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        with state.sessions() as session:
            return templates.TemplateResponse(request, "dashboard.html", {"recordings": session.query(Recording).count(), "pending": session.query(Interview).filter(Interview.review_status==ReviewStatus.PENDING).count(), "approved": session.query(Interview).filter(Interview.review_status==ReviewStatus.APPROVED).count()})
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request): return templates.TemplateResponse(request, "settings.html", {})
    @app.get("/review", response_class=HTMLResponse)
    def review_queue(request: Request): return templates.TemplateResponse(request, "queue.html", {})
    @app.get("/review/{interview_id}", response_class=HTMLResponse)
    def review_detail(request: Request, interview_id: int): return templates.TemplateResponse(request, "detail.html", {"interview_id": interview_id})
    return app


app = create_app()
