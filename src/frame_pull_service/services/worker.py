from __future__ import annotations

import re
import threading
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..models import Job, JobStatus, ProcessingRun, Recording, RecordingStatus
from .legacy_engine import LegacySubprocessEngine
from .normalizer import normalize_run
from .queue import claim_next_job, recover_stale_jobs
from .operations import get_value


class Worker:
    def __init__(self, factory: sessionmaker[Session], settings: Settings):
        self.factory, self.settings = factory, settings
        self.stop_event, self.thread, self.jobs_lock = threading.Event(), None, threading.Lock()
        self.job_threads: dict[int, threading.Thread] = {}

    @property
    def active(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self) -> None:
        if self.active: return
        with self.factory() as session: recover_stale_jobs(session)
        self.stop_event.clear(); self.thread = threading.Thread(target=self._loop, daemon=True, name="frame-pull-worker"); self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def _loop(self) -> None:
        while not self.stop_event.wait(1):
            with self.jobs_lock:
                self.job_threads = {key: value for key, value in self.job_threads.items() if value.is_alive()}
            with self.factory() as session:
                slots = int(get_value(session, "max_concurrent_recordings", self.settings.max_concurrent_jobs))
                job = claim_next_job(session, max_concurrent=slots)
            if job is None:
                continue
            thread = threading.Thread(target=self._process, args=(job.id,), daemon=True, name=f"frame-pull-job-{job.id}")
            with self.jobs_lock:
                self.job_threads[job.id] = thread
            thread.start()

    def _process(self, job_id: int) -> None:
        with self.factory() as session:
            job = session.get(Job, job_id); recording = session.get(Recording, job.recording_id)
            run = ProcessingRun(recording_id=recording.id, run_key=f"run_{job.id}_{int(datetime.utcnow().timestamp())}",
                                job_dir=str(self.settings.data_dir / "jobs" / f"job_{job.id:06d}"))
            session.add(run); session.flush(); job.processing_run_id, job.log_path = run.id, str((self.settings.data_dir / "jobs" / f"job_{job.id:06d}" / "legacy_engine.log")); session.commit()
        def progress(line: str) -> None:
            with self.factory() as update:
                current = update.get(Job, job_id); current.heartbeat_at = datetime.utcnow(); current.progress_stage = "legacy_detector"
                match = re.search(r"(\d{1,3})%", line)
                if match: current.progress_percent = min(90.0, float(match.group(1)) * 0.9)
                update.commit()
        try:
            result = LegacySubprocessEngine(self.settings).run(__import__("pathlib").Path(recording.source_path), __import__("pathlib").Path(run.job_dir), progress)
            with self.factory() as session:
                job, recording, run = session.get(Job, job_id), session.get(Recording, recording.id), session.get(ProcessingRun, run.id)
                normalize_run(session, self.settings, recording, run, result)
                run.status, run.completed_at = "complete", datetime.utcnow(); job.status, job.finished_at, job.progress_stage, job.progress_percent = JobStatus.COMPLETE, datetime.utcnow(), "complete", 100.0; recording.status = RecordingStatus.COMPLETE; session.commit()
        except Exception as exc:
            with self.factory() as session:
                job, recording, run = session.get(Job, job_id), session.get(Recording, recording.id), session.get(ProcessingRun, run.id)
                run.status, job.status, job.error_summary, job.finished_at = "failed", JobStatus.FAILED, str(exc), datetime.utcnow(); recording.status = RecordingStatus.FAILED; session.commit()
