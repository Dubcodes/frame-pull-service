from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from frame_pull_service.config import Settings
from frame_pull_service.db import create_db_engine, init_db, make_session_factory
from frame_pull_service.main import create_app
from frame_pull_service.models import Candidate, Interview, InterviewRevision, Job, JobStatus, ProcessingRun, RaceDay, Recording, RecordingStatus, ReviewEvent, ReviewStatus
from frame_pull_service.services.groups import list_groups
from frame_pull_service.services.operations import get_value, seed_settings, update_settings
from frame_pull_service.services.queue import cancel_queued, claim_next_job, queue_selected
from frame_pull_service.services.race_days import attach_recording_to_race_day, parse_recording_filename
from frame_pull_service.services.source_lifecycle import deletion_eligibility


class OperationalV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name); self.source = self.root / "source"; self.source.mkdir()
        self.settings = Settings(source_dir=self.source, data_dir=self.root / "data", database_url=f"sqlite:///{(self.root/'service.sqlite').as_posix()}", min_input_bytes=1)
        self.engine = create_db_engine(self.settings); init_db(self.engine); self.sessions = make_session_factory(self.engine)
        with self.sessions() as session: seed_settings(session, self.settings)

    def tearDown(self): self.engine.dispose(); self.temp.cleanup()

    def record(self, name="trackside_20260921-0750_021.ts", status=RecordingStatus.READY):
        path=self.source/name; path.write_bytes(b"x"*100)
        with self.sessions() as session:
            item=Recording(filename=name,source_path=str(path),fingerprint=name,size_bytes=100,mtime_epoch=0,status=status,stable_at=datetime.utcnow(),is_closed=True)
            session.add(item);session.flush();attach_recording_to_race_day(session,item);session.commit();return item.id

    def test_settings_defaults_are_safe_and_persist_pause(self):
        with self.sessions() as session:
            self.assertFalse(get_value(session,"source_deletion_enabled"));self.assertFalse(get_value(session,"autoqueue_closed_recordings"));update_settings(session,{"processing_paused":True,"max_concurrent_recordings":9})
        with self.sessions() as session: self.assertTrue(get_value(session,"processing_paused"));self.assertEqual(get_value(session,"max_concurrent_recordings"),4)

    def test_filename_parser_and_race_day_attachment(self):
        self.assertEqual(
            parse_recording_filename("trackside_20260921-0750_021.ts"),
            datetime(2026, 9, 21, 7, 50, tzinfo=ZoneInfo("Pacific/Auckland")),
        );self.assertIsNone(parse_recording_filename("other.ts"));rid=self.record()
        with self.sessions() as session: self.assertEqual(session.get(Recording,rid).race_day_id,session.query(RaceDay).one().id)

    def test_pause_and_concurrency_claim_distinct_jobs(self):
        first=self.record();second=self.record("trackside_20260921-0850_022.ts")
        with self.sessions() as session:
            queue_selected(session,[first,second]);one=claim_next_job(session,max_concurrent=2,paused=False);two=claim_next_job(session,max_concurrent=2,paused=False);self.assertNotEqual(one.id,two.id);self.assertIsNone(claim_next_job(session,max_concurrent=2,paused=False))
        third=self.record("trackside_20260921-0950_023.ts")
        with self.sessions() as session:
            queue_selected(session,[third]);self.assertIsNone(claim_next_job(session,max_concurrent=4,paused=True));self.assertEqual(session.get(Recording,third).status,RecordingStatus.QUEUED)

    def test_simultaneous_separate_sessions_never_claim_the_same_job(self):
        first=self.record(); second=self.record("trackside_20260921-0850_022.ts")
        with self.sessions() as session: queue_selected(session,[first,second])
        barrier=threading.Barrier(2); claimed=[]; errors=[]
        def claim():
            try:
                with self.sessions() as session:
                    barrier.wait(); job=claim_next_job(session,max_concurrent=2,paused=False)
                    if job: claimed.append(job.id)
            except Exception as exc: errors.append(exc)
        workers=[threading.Thread(target=claim),threading.Thread(target=claim)]
        for worker in workers: worker.start()
        for worker in workers: worker.join()
        self.assertFalse(errors); self.assertEqual(len(claimed),2); self.assertEqual(len(set(claimed)),2)

    def test_bulk_queue_is_idempotent_and_cancel_never_cancels_processing(self):
        queued=self.record();active=self.record("trackside_20260921-0850_022.ts")
        with self.sessions() as session:
            first=queue_selected(session,[queued,active]);second=queue_selected(session,[queued]);self.assertEqual(len(first["queued"]),2);self.assertEqual(len(second["existing"]),1);job=claim_next_job(session,max_concurrent=2,paused=False);result=cancel_queued(session,[queued,active]);self.assertIn(job.id,result["protected_processing"]);self.assertEqual(len(result["cancelled"]),1)

    def test_groups_only_trusted_or_manual_identity(self):
        rid=self.record()
        with self.sessions() as session:
            rec=session.get(Recording,rid);run=ProcessingRun(recording_id=rid,run_key="run-test",job_dir="x",status="complete");session.add(run);session.flush()
            known=Interview(interview_key="known",recording_id=rid,source_start=1,source_end=2,bounds_basis="test",final_name="Alex Smith",review_status=ReviewStatus.PENDING);unknown=Interview(interview_key="unknown",recording_id=rid,source_start=3,source_end=4,bounds_basis="test",review_status=ReviewStatus.PENDING);session.add_all([known,unknown]);session.flush()
            revision=InterviewRevision(interview_id=known.id,processing_run_id=run.id,revision_number=1,trusted_name=None,clip_start=0,clip_end=1,clip_path="x",poster_path="x",manifest_path="x");session.add(revision);session.flush();candidate=Candidate(revision_id=revision.id,rank=1,source_timestamp=1,image_path="x",selected=True);session.add(candidate);session.flush();known.active_revision_id=revision.id;known.selected_candidate_id=candidate.id;session.commit();groups=list_groups(session);self.assertEqual(len(groups),1);self.assertEqual(groups[0]["name"],"Alex Smith");self.assertEqual(groups[0]["candidate_count"],1)

    def test_source_deletion_disabled_blocks_even_completed_source(self):
        rid=self.record()
        with self.sessions() as session:
            rec=session.get(Recording,rid);job=Job(recording_id=rid,status=JobStatus.COMPLETE);session.add(job);session.commit();result=deletion_eligibility(session,self.settings,rec);self.assertFalse(result["safe_to_delete"]);self.assertFalse(result["checks"]["deletion_enabled"])

    def test_bridge_requires_token_when_configured_and_no_paths(self):
        settings=Settings(source_dir=self.source,data_dir=self.root/'bridge-data',database_url=f"sqlite:///{(self.root/'bridge.sqlite').as_posix()}",bridge_token="secret")
        app=create_app(settings)
        with TestClient(app) as client:
            self.assertEqual(client.get('/api/bridge/v1/health').status_code,401);self.assertEqual(client.get('/api/bridge/v1/health',headers={'Authorization':'Bearer secret'}).status_code,200);self.assertNotIn(str(self.root),client.get('/api/bridge/v1/race-days',headers={'Authorization':'Bearer secret'}).text)

    def test_bridge_group_acknowledgement_updates_approved_evidence_idempotently(self):
        rid=self.record()
        manifest=self.settings.data_dir/'evidence'/'manifest.json'; image=self.settings.data_dir/'evidence'/'portrait.jpg'; image.parent.mkdir(parents=True); image.write_bytes(b'portrait')
        with self.sessions() as session:
            run=ProcessingRun(recording_id=rid,run_key='bridge-run',job_dir='x',status='complete');session.add(run);session.flush()
            interview=Interview(interview_key='bridge-person',recording_id=rid,source_start=1,source_end=2,bounds_basis='test',final_name='Ryan Foote',final_role='Trainer',review_status=ReviewStatus.APPROVED);session.add(interview);session.flush()
            revision=InterviewRevision(interview_id=interview.id,processing_run_id=run.id,revision_number=1,clip_start=0,clip_end=1,clip_path='x',poster_path='x',manifest_path=str(manifest),manifest={});session.add(revision);session.flush()
            candidate=Candidate(revision_id=revision.id,rank=1,source_timestamp=1,image_path=str(image),selected=True);session.add(candidate);session.flush();interview.active_revision_id=revision.id;interview.selected_candidate_id=candidate.id;session.commit();group_id=list_groups(session)[0]['id']
        app=create_app(self.settings)
        with TestClient(app) as client:
            self.assertEqual(client.get('/api/bridge/v1/interviews').status_code,200)
            self.assertEqual(client.get(f'/api/bridge/v1/interviews/{interview.id}/portrait').status_code,200)
            first=client.post(f'/api/bridge/v1/groups/{group_id}/mark-exported');second=client.post(f'/api/bridge/v1/groups/{group_id}/mark-exported')
        self.assertEqual(first.status_code,200);self.assertEqual(second.status_code,200);self.assertEqual(first.json()['export_state'],'exported')
        with self.sessions() as session:
            updated=session.get(Interview,interview.id);self.assertEqual(updated.export_state,'exported');self.assertEqual(session.query(ReviewEvent).filter(ReviewEvent.interview_id==interview.id,ReviewEvent.event_type=='export_acknowledged').count(),1);self.assertIn('exported',manifest.read_text())
