from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from frame_pull_service.config import Settings
from frame_pull_service.db import create_db_engine, init_db, make_session_factory
from frame_pull_service.main import create_app
from frame_pull_service.models import Job, JobStatus, ProcessingRun, RaceDay, Recording, RecordingMediaAudit, RecordingStatus
from frame_pull_service.services.detector_candidates import detector_candidates_for_race_day
from frame_pull_service.services.operations import get_value, seed_settings, update_settings
from frame_pull_service.services.source_lifecycle import deletion_eligibility, source_lifecycle_dry_run


class DetectorTriageV2131Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.source = self.root / "recordings"; self.source.mkdir()
        self.data = self.root / "data"; self.data.mkdir()
        self.settings = Settings(source_dir=self.source, data_dir=self.data,
            database_url=f"sqlite:///{(self.root / 'frame_pull.db').as_posix()}", min_input_bytes=1)
        self.settings.ensure_data_dirs(); self.engine = create_db_engine(self.settings); init_db(self.engine)
        self.sessions = make_session_factory(self.engine)
        with self.sessions() as session:
            seed_settings(session, self.settings)
            day = RaceDay(race_date=date(2026, 11, 15), label="Race day"); session.add(day); session.flush()
            path = self.source / "trackside_20261115-1200_00.ts"; path.write_bytes(b"media")
            recording = Recording(filename=path.name, source_path=str(path), fingerprint="fixture", size_bytes=5,
                mtime_epoch=path.stat().st_mtime, status=RecordingStatus.READY, stable_at=datetime.utcnow(), is_closed=True,
                race_day_id=day.id, recording_started_at=datetime(2026, 11, 15, 12), duration_seconds=60)
            session.add(recording); session.flush()
            job_dir = self.data / "jobs" / "job_1"; audit_dir = job_dir / "legacy_work" / "trackside"; audit_dir.mkdir(parents=True)
            audit = [
                {"start":10,"end":12,"observations":2,"required_observations":3,"best_stable_pair_run":0,"required_stable_pairs":2,"threshold":0.7,"authenticated":False},
                {"start":20,"end":24,"observations":4,"required_observations":3,"best_stable_pair_run":1,"required_stable_pairs":2,"threshold":0.7,"authenticated":False},
                {"start":30,"end":35,"observations":4,"required_observations":3,"best_stable_pair_run":2,"required_stable_pairs":2,"authenticated":True},
            ]
            (audit_dir / "temporal_authentication_audit.json").write_text(json.dumps(audit), encoding="utf-8")
            run = ProcessingRun(recording_id=recording.id, run_key="run", job_dir=str(job_dir), status="complete")
            session.add(run); session.commit(); self.day_id=day.id; self.recording_id=recording.id

    def tearDown(self):
        self.engine.dispose(); self.temp.cleanup()

    def test_rejected_artifacts_are_safely_normalized_without_invented_ocr(self):
        with self.sessions() as session:
            rows = detector_candidates_for_race_day(session, self.settings, self.day_id)
            self.assertEqual(len(rows), 2); self.assertEqual(rows[0]["hit_count"], 2)
            self.assertIsNone(rows[0]["ocr_text"]); self.assertIn("insufficient temporal observations", rows[0]["rejection_reason"])
            self.assertIn("insufficient stable hit pairs", rows[1]["rejection_reason"])
            self.assertEqual(rows[0]["proposed_start"], 8); self.assertEqual(rows[0]["proposed_end"], 17)
            self.assertNotIn(str(self.root), json.dumps(rows))

    def test_source_review_confirmation_and_zero_interview_deletion_gate(self):
        app = create_app(self.settings)
        with TestClient(app) as client:
            response = client.put(f"/api/race-days/{self.day_id}/source-review", json={"state":"review_complete"})
            self.assertEqual(response.status_code, 422)
            response = client.put(f"/api/race-days/{self.day_id}/source-review", json={"state":"in_progress","note":"triage"})
            self.assertEqual(response.status_code, 200); self.assertEqual(response.json()["state"], "in_progress")
        with self.sessions() as session:
            session.add(Job(recording_id=self.recording_id, status=JobStatus.COMPLETE))
            session.add(RecordingMediaAudit(recording_id=self.recording_id, tool_version="fixture", probe_ok=True,
                full_scan_ok=True, classification="healthy", has_video=True, has_audio=True, error_count=0,
                summary="healthy", evidence={})); session.commit()
            update_settings(session,{"source_deletion_enabled":True,"source_retention_hours":0})
            result=deletion_eligibility(session,self.settings,session.get(Recording,self.recording_id))
            self.assertFalse(result["safe_to_delete"]); self.assertFalse(result["checks"]["race_day_source_review_complete"])
        with TestClient(app) as client:
            response=client.put(f"/api/race-days/{self.day_id}/source-review",json={"state":"review_complete","confirmation":"source review is complete"})
            self.assertEqual(response.status_code,200)
        with self.sessions() as session:
            self.assertTrue(deletion_eligibility(session,self.settings,session.get(Recording,self.recording_id))["checks"]["race_day_source_review_complete"])

    def test_lifecycle_dry_run_simulates_enabled_without_mutation_or_deletion(self):
        before=(self.source / "trackside_20261115-1200_00.ts").read_bytes()
        with self.sessions() as session:
            self.assertFalse(get_value(session,"source_deletion_enabled"))
            result=source_lifecycle_dry_run(session,self.settings)
            self.assertTrue(result["dry_run"]); self.assertTrue(result["simulated_source_deletion_enabled"])
            self.assertFalse(get_value(session,"source_deletion_enabled"))
        self.assertEqual((self.source / "trackside_20261115-1200_00.ts").read_bytes(),before)

    def test_api_and_frontend_expose_candidates_filters_seek_prefill_and_review(self):
        app=create_app(self.settings)
        with TestClient(app) as client:
            response=client.get(f"/api/race-days/{self.day_id}/detector-candidates")
            self.assertEqual(response.status_code,200); self.assertEqual(response.json()["count"],2)
            self.assertNotIn(str(self.root),response.text)
        web=Path(__file__).resolve().parents[1]/"src"/"frame_pull_service"/"web"
        js=(web/"static"/"race_day.js").read_text(); html=(web/"templates"/"race_day.html").read_text()
        for text in ("candidate-search","candidate-chunk","candidate-reason","candidate-unresolved","detector_candidate_key","previewCandidate","prefillCandidate"):
            self.assertIn(text,js+html)
        self.assertIn("source review is complete",js); self.assertIn("Possible missed interviews",html)


if __name__ == "__main__": unittest.main()
