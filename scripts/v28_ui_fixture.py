from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from frame_pull_service.config import Settings
from frame_pull_service.db import create_db_engine, init_db, make_session_factory
from frame_pull_service.models import Job, JobStatus, RaceDay, Recording, RecordingStatus
from frame_pull_service.services.operations import seed_settings, update_settings


ROOT = Path(__file__).resolve().parents[1] / "data" / "v28-ui-qualification"


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=False)
    source = ROOT / "source"
    source.mkdir()
    settings = Settings(source_dir=source, data_dir=ROOT / "data",
                        database_url=f"sqlite:///{(ROOT / 'frame_pull.db').as_posix()}")
    engine = create_db_engine(settings)
    init_db(engine)
    sessions = make_session_factory(engine)
    states = [
        ("trackside_20261013-0900_01.ts", JobStatus.QUEUED, False, RecordingStatus.QUEUED),
        ("trackside_20261013-1000_02.ts", JobStatus.QUEUED, True, RecordingStatus.QUEUED),
        ("trackside_20261013-1100_03.ts", JobStatus.PROCESSING, True, RecordingStatus.PROCESSING),
        ("trackside_20261013-1200_04.ts", JobStatus.COMPLETE, True, RecordingStatus.COMPLETE),
        ("trackside_20261013-1300_05.ts", JobStatus.FAILED, False, RecordingStatus.FAILED),
    ]
    with sessions() as session:
        seed_settings(session, settings)
        update_settings(session, {"processing_paused": True, "autoqueue_closed_recordings": False,
                                  "automatic_race_day_mode": False, "source_deletion_enabled": False})
        race_day = RaceDay(race_date=date(2026, 10, 13), label="Tuesday 13 October 2026")
        session.add(race_day)
        session.flush()
        for offset, (filename, job_status, released, recording_status) in enumerate(states):
            recording = Recording(filename=filename, source_path=str(source / filename), fingerprint=filename,
                                  size_bytes=50_000_000, mtime_epoch=1, status=recording_status,
                                  historical=False, race_day_id=race_day.id,
                                  recording_started_at=datetime(2026, 10, 13, 9 + offset, 0),
                                  is_closed=True)
            session.add(recording)
            session.flush()
            session.add(Job(recording_id=recording.id, status=job_status,
                            released_for_processing=released, progress_stage=job_status.value))
        session.commit()
    engine.dispose()
    print(ROOT)


if __name__ == "__main__":
    main()
