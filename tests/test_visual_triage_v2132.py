from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from frame_pull_service.config import Settings
from frame_pull_service.db import create_db_engine, init_db, make_session_factory
from frame_pull_service.main import create_app
from frame_pull_service.models import DetectorCandidateTriage, DetectorCandidateTriageEvent, Interview, ProcessingRun, RaceDay, Recording, RecordingStatus
from frame_pull_service.services.candidate_triage import candidate_preview, cleanup_candidate_preview_cache, reinspect_candidate, set_candidate_triage
from frame_pull_service.services.operations import seed_settings, update_settings
from frame_pull_service.services.source_review_queue import source_review_progress, source_review_queue


class VisualTriageV2132Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.source=self.root/"recordings"; self.source.mkdir(); self.data=self.root/"data"; self.data.mkdir()
        self.settings=Settings(source_dir=self.source,data_dir=self.data,database_url=f"sqlite:///{(self.root/'db.sqlite').as_posix()}",
            min_input_bytes=1,playback_cache_min_free_gb=0)
        self.settings.ensure_data_dirs(); self.engine=create_db_engine(self.settings); init_db(self.engine); self.sessions=make_session_factory(self.engine)
        with self.sessions() as session:
            seed_settings(session,self.settings); self.day=RaceDay(race_date=date(2026,11,15),label="Sunday")
            session.add(self.day);session.flush(); self.day_id=self.day.id
            path=self.source/"trackside_20261115-1200_00.ts";path.write_bytes(b"canonical-source")
            rec=Recording(filename=path.name,source_path="ignored",fingerprint="v2132",size_bytes=path.stat().st_size,
                mtime_epoch=0,status=RecordingStatus.COMPLETE,stable_at=datetime.utcnow(),is_closed=True,race_day_id=self.day.id,
                recording_started_at=datetime(2026,11,15,12),duration_seconds=60,source_protected=True)
            session.add(rec);session.flush();self.recording_id=rec.id
            work=self.data/"jobs"/"job"/"legacy_work"/"recording";work.mkdir(parents=True)
            audit=[{"start":10,"end":12,"observations":2,"required_observations":2,"best_stable_pair_run":0,"required_stable_pairs":1,"threshold":.7,"authenticated":False}]
            (work/"temporal_authentication_audit.json").write_text(json.dumps(audit))
            run=ProcessingRun(recording_id=rec.id,run_key="visual",job_dir=str(self.data/"jobs"/"job"),status="complete")
            session.add(run);session.commit();self.key=f"run-{run.id}-temporal-0"

    def tearDown(self): self.engine.dispose();self.temp.cleanup()

    @staticmethod
    def fake_frame(_ffmpeg,_source,destination,_time):
        destination.parent.mkdir(parents=True,exist_ok=True);Image.new("RGB",(320,180),(30,80,120)).save(destination,"JPEG")

    def test_preview_generation_reuse_cleanup_and_contained_missing_source(self):
        source_path=self.source/"trackside_20261115-1200_00.ts";before=source_path.read_bytes()
        with self.sessions() as session:
            first=candidate_preview(session,self.settings,self.key,"start",frame_extractor=self.fake_frame)
            second=candidate_preview(session,self.settings,self.key,"start",frame_extractor=lambda *_: self.fail("cache regenerated"))
            self.assertEqual(first,second)
            candidate_preview(session,self.settings,self.key,"midpoint",frame_extractor=self.fake_frame)
            update_settings(session,{"candidate_preview_cache_max_bytes":1})
            result=cleanup_candidate_preview_cache(session,self.settings);self.assertGreater(result["deleted"],0)
        self.assertEqual(source_path.read_bytes(),before)
        source_path.unlink()
        app=create_app(self.settings)
        with TestClient(app) as client: self.assertEqual(client.get(f"/api/detector-candidates/{self.key}/preview/start").status_code,404)

    def test_triage_state_audit_progress_filters_and_completion_safety(self):
        app=create_app(self.settings)
        with TestClient(app) as client:
            self.assertEqual(client.put(f"/api/detector-candidates/{self.key}/triage",json={"state":"not_interview"}).status_code,422)
            response=client.put(f"/api/detector-candidates/{self.key}/triage",json={"state":"not_interview","confirmation":"mark candidate not an interview","note":"visual check"})
            self.assertEqual(response.status_code,200);self.assertEqual(response.json()["state"],"not_interview")
            candidates=client.get(f"/api/race-days/{self.day_id}/detector-candidates").json()["candidates"]
            self.assertEqual(candidates[0]["triage_state"],"not_interview")
            self.assertEqual(client.put(f"/api/race-days/{self.day_id}/source-review",json={"state":"review_complete","confirmation":"source review is complete"}).status_code,200)
        with self.sessions() as session:
            row=session.query(DetectorCandidateTriage).one();self.assertIsNotNone(row.reviewed_at);self.assertEqual(row.operator_note,"visual check")
            event=session.query(DetectorCandidateTriageEvent).one();self.assertEqual((event.from_state,event.to_state),("unreviewed","not_interview"))
            self.assertEqual(source_review_progress(session,self.settings,self.day_id)["remaining"],0)

    def test_deferred_is_unresolved_and_strong_completion_retains_count(self):
        with self.sessions() as session: set_candidate_triage(session,self.settings,self.key,"deferred",note="come back")
        app=create_app(self.settings)
        with TestClient(app) as client:
            self.assertEqual(client.put(f"/api/race-days/{self.day_id}/source-review",json={"state":"review_complete","confirmation":"source review is complete"}).status_code,422)
            response=client.put(f"/api/race-days/{self.day_id}/source-review",json={"state":"review_complete","confirmation":"complete source review with unresolved candidates"})
            self.assertEqual(response.status_code,200);self.assertEqual(response.json()["unresolved_count_at_completion"],1)

    def test_bounded_reinspection_is_derived_and_creates_no_interview(self):
        def analyzer(_command,_image): return {"text":"ALEX SAMPLE TRAINER","confidence":88.5,"observations":[{"text":"ALEX","confidence":90}]}
        with self.sessions() as session:
            before=session.query(Interview).count(); result=reinspect_candidate(session,self.settings,self.key,analyzer=analyzer,frame_extractor=self.fake_frame)
            self.assertEqual(result["kind"],"derived_triage_ocr");self.assertEqual(result["role_text"],"TRAINER")
            self.assertFalse(result["automatic_interview_created"]);self.assertEqual(session.query(Interview).count(),before)
            self.assertEqual(session.query(DetectorCandidateTriage).one().review_state,"unreviewed")

    def test_source_review_queue_and_storage_risk_ordering(self):
        with self.sessions() as session:
            older=RaceDay(race_date=date(2026,11,14),label="Older");session.add(older);session.flush()
            path=self.source/"trackside_20261114-1200_00.ts";path.write_bytes(b"old")
            session.add(Recording(filename=path.name,source_path="ignored",fingerprint="older",size_bytes=3,mtime_epoch=0,
                status=RecordingStatus.COMPLETE,stable_at=datetime.utcnow(),is_closed=True,race_day_id=older.id,
                recording_started_at=datetime(2026,11,14,12),duration_seconds=30));session.commit()
            queue=source_review_queue(session,self.settings)
            self.assertEqual(queue["race_days"][0]["race_day_id"],older.id)
            self.assertEqual(queue["race_days_awaiting_review"],2);self.assertGreater(queue["retained_bytes_source_review_incomplete"],0)
            self.assertIn("bytes_blocked_only_by_source_review",queue);self.assertGreater(queue["free_recording_bytes"],0)

    def test_frontend_has_visual_states_sorting_keyboard_and_queue(self):
        web=Path(__file__).resolve().parents[1]/"src"/"frame_pull_service"/"web"
        js=(web/"static"/"race_day.js").read_text();html=(web/"templates"/"race_day.html").read_text()
        for value in ("candidate-previews","candidate-state","candidate-sort","NOT AN INTERVIEW","Reinspect candidate","event.key.toLowerCase()==='j'","complete source review with unresolved candidates"):
            self.assertIn(value,js+html)
        self.assertTrue((web/"templates"/"source_review.html").is_file());self.assertTrue((web/"static"/"source_review.js").is_file())


if __name__=="__main__": unittest.main()
