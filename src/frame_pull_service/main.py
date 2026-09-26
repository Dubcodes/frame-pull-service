from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from . import __version__
from .config import Settings, get_settings
from .db import create_db_engine, init_db, make_session_factory
from .models import Candidate, Interview, InterviewRevision, Job, JobStatus, Recording, ReviewEvent, ReviewStatus
from .schemas import CaptureFrameRequest, RejectRequest, ReviewPatch, SelectCandidateRequest
from .services.discovery import discover_recordings
from .services.cleanup import cleanup_plan
from .services.paths import artifact_path
from .services.queue import queue_recording
from .services.review import approve, capture_frame, reject, save_review, select_candidate, refresh_manifest
from .services.worker import Worker


class ServiceState:
    def __init__(self, settings: Settings, worker_enabled: bool):
        settings.ensure_data_dirs(); self.settings = settings; self.engine = create_db_engine(settings); init_db(self.engine)
        self.sessions = make_session_factory(self.engine); self.worker = Worker(self.sessions, settings) if worker_enabled else None
        self.watcher_task: asyncio.Task | None = None


def serialize_recording(recording: Recording) -> dict:
    return {"id": recording.id, "filename": recording.filename, "size_bytes": recording.size_bytes,
            "status": recording.status.value, "historical": recording.historical, "stable_at": recording.stable_at}


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
            "has_portrait": candidate is not None, "poster_url": f"/api/interviews/{interview.id}/poster" if revision else None}


def create_app(settings: Settings | None = None, worker_enabled: bool = False) -> FastAPI:
    settings = settings or get_settings(); state = ServiceState(settings, worker_enabled)
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "web" / "templates"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with state.sessions() as session: discover_recordings(session, state.settings)
        if state.worker: state.worker.start()
        async def watch() -> None:
            while True:
                await asyncio.sleep(state.settings.discovery_interval_seconds)
                with state.sessions() as session: discover_recordings(session, state.settings)
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
            current = session.execute(select(Job).where(Job.status == JobStatus.PROCESSING)).scalar_one_or_none()
            return {"version": __version__, "database": "ok", "watcher_active": state.watcher_task is not None,
                    "worker_active": bool(state.worker and state.worker.active), "current_job": current.id if current else None,
                    "queue_depth": session.query(Job).filter(Job.status == JobStatus.QUEUED).count(),
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

    @app.get("/api/interviews")
    def interviews(status: ReviewStatus | None = None, export_state: str | None = None) -> list[dict]:
        with state.sessions() as session:
            query = select(Interview)
            if status: query = query.where(Interview.review_status == status)
            if export_state: query = query.where(Interview.export_state == export_state)
            return [serialize_interview(session, item) for item in session.execute(query.order_by(Interview.id.desc())).scalars()]

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
            item=get_interview(session, interview_id)
            if item.review_status != ReviewStatus.APPROVED: raise HTTPException(409, "only approved interviews can be exported")
            item.export_state="exported"; session.add(ReviewEvent(interview_id=item.id, event_type="export_acknowledged", payload={})); refresh_manifest(session, item); session.commit(); return {"export_state": item.export_state}

    @app.get("/api/admin/cleanup-plan")
    def cleanup_dry_run() -> dict:
        return cleanup_plan(state.settings)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        with state.sessions() as session:
            return templates.TemplateResponse(request, "dashboard.html", {"recordings": session.query(Recording).count(), "pending": session.query(Interview).filter(Interview.review_status==ReviewStatus.PENDING).count(), "approved": session.query(Interview).filter(Interview.review_status==ReviewStatus.APPROVED).count()})
    @app.get("/review", response_class=HTMLResponse)
    def review_queue(request: Request): return templates.TemplateResponse(request, "queue.html", {})
    @app.get("/review/{interview_id}", response_class=HTMLResponse)
    def review_detail(request: Request, interview_id: int): return templates.TemplateResponse(request, "detail.html", {"interview_id": interview_id})
    return app


app = create_app()
