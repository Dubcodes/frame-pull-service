from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import text

from frame_pull_service.config import Settings
from frame_pull_service.db import create_db_engine, init_db, make_session_factory
from frame_pull_service.main import create_app
from frame_pull_service.models import Candidate, Interview, InterviewRevision, Job, JobStatus, ProcessingRun, Recording, RecordingStatus, ReviewEvent, ReviewStatus
from frame_pull_service.services.cleanup import cleanup_plan
from frame_pull_service.services.discovery import discover_recordings
from frame_pull_service.services.legacy_engine import LegacySubprocessEngine
from frame_pull_service.services.media import clip_bounds, contained_clip_bounds, create_clip, extract_frame, ffprobe_duration
from frame_pull_service.services.manifests import write_manifest
from frame_pull_service.services.normalizer import interview_key
from frame_pull_service.services.paths import artifact_path, contained_path, relative_artifact, source_path
from frame_pull_service.services.queue import claim_next_job, queue_recording, recover_stale_jobs
from frame_pull_service.services.review import approve, capture_frame, reject, refresh_manifest, save_review, select_candidate


class QualificationCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name); self.source = self.root / "source"; self.source.mkdir()
        self.legacy = self.root / "legacy"; (self.legacy / ".venv" / "Scripts").mkdir(parents=True); (self.legacy / "config.yaml").write_text('input_file: "old.ts"\noutput_root: "old-output"\nwork_root: "old-work"\n')
        self.settings = Settings(source_dir=self.source, legacy_engine_root=self.legacy, data_dir=self.root / "data", database_url=f"sqlite:///{(self.root/'db.sqlite').as_posix()}", min_input_bytes=32, stable_for_seconds=10, auto_queue=True)
        self.engine = create_db_engine(self.settings); init_db(self.engine); self.sessions = make_session_factory(self.engine)

    def tearDown(self): self.engine.dispose(); self.temp.cleanup()

    def add_recording(self, name="recording.ts", size=128, age=30):
        path=self.source/name; path.write_bytes(b"x"*size); stamp=datetime.now().timestamp()-age; __import__('os').utime(path,(stamp,stamp)); return path

    def session_fixture(self, with_candidate=True):
        self.settings.ensure_data_dirs(); path=self.add_recording(); package=self.settings.data_dir/'interviews'/'fixture'/'revision_001'; package.mkdir(parents=True)
        Image.new('RGB',(64,36),(20,40,60)).save(package/'poster.jpg'); (package/'clip.mp4').write_bytes(b'video')
        with self.sessions() as db:
            rec=Recording(filename=path.name,source_path=str(path),fingerprint='fixture',size_bytes=128,mtime_epoch=1,status=RecordingStatus.COMPLETE); db.add(rec);db.flush()
            run=ProcessingRun(recording_id=rec.id,run_key='fixture-run',job_dir=str(self.settings.data_dir/'jobs'/'fixture'),status='complete');db.add(run);db.flush()
            interview=Interview(interview_key='int_fixture',recording_id=rec.id,source_start=1,source_end=2,bounds_basis='authenticated_segment');db.add(interview);db.flush()
            revision=InterviewRevision(interview_id=interview.id,processing_run_id=run.id,revision_number=1,trusted_name='BOB VANCE',ocr_name_candidate='BOB VANCE',ocr_confidence=.8,name_trust_reason='trusted',name_consensus_evidence={'timestamps':2},role_raw='TRAINER',role_clean='TRAINER',track_value=None,track_confidence=None,clip_start=0,clip_end=3,clip_path=str(package/'clip.mp4'),poster_path=str(package/'poster.jpg'),manifest_path=str(package/'manifest.json'),manifest={})
            db.add(revision);db.flush(); interview.active_revision_id=revision.id; interview.final_name='BOB VANCE';interview.identity_source='ocr_trusted'
            candidate=None
            if with_candidate:
                image=package/'candidates'/'candidate_001.jpg';image.parent.mkdir();Image.new('RGB',(64,36),(10,20,30)).save(image);candidate=Candidate(revision_id=revision.id,rank=1,source_timestamp=1.2,image_path=str(image),selected=True);db.add(candidate);db.flush();interview.selected_candidate_id=candidate.id
            db.commit(); return rec.id,interview.id,revision.id,candidate.id if candidate else None

    def test_db_reopens_and_relationship_rows_persist(self):
        _, iid, rid, cid=self.session_fixture(); self.engine.dispose(); engine=create_db_engine(self.settings); factory=make_session_factory(engine)
        with factory() as db: self.assertEqual(db.get(Interview,iid).active_revision_id,rid); self.assertEqual(db.get(Candidate,cid).revision_id,rid)
        engine.dispose()

    def test_database_pragmas(self):
        with self.engine.connect() as db: self.assertEqual(db.execute(text('PRAGMA foreign_keys')).scalar(),1);self.assertEqual(db.execute(text('PRAGMA journal_mode')).scalar().lower(),'wal')

    def test_discovery_is_idempotent(self):
        self.add_recording('old.ts')
        with self.sessions() as db: discover_recordings(db,self.settings);discover_recordings(db,self.settings);self.assertEqual(db.query(Recording).count(),1);self.assertEqual(db.query(Job).count(),0)

    def test_new_post_baseline_stable_file_autoqueues(self):
        self.add_recording('old.ts')
        with self.sessions() as db: discover_recordings(db,self.settings)
        self.add_recording('new.ts')
        with self.sessions() as db: discover_recordings(db,self.settings);new=db.query(Recording).filter_by(filename='new.ts').one();self.assertFalse(new.historical);self.assertEqual(new.status,RecordingStatus.QUEUED);self.assertEqual(db.query(Job).count(),1)

    def test_unstable_new_file_waits(self):
        self.add_recording('old.ts')
        with self.sessions() as db: discover_recordings(db,self.settings)
        self.add_recording('new.ts',age=0)
        with self.sessions() as db: discover_recordings(db,self.settings);self.assertEqual(db.query(Recording).filter_by(filename='new.ts').one().status,RecordingStatus.WAITING)

    def test_small_and_zero_new_files_never_queue(self):
        self.add_recording('old.ts')
        with self.sessions() as db: discover_recordings(db,self.settings)
        self.add_recording('small.ts',size=2);self.add_recording('zero.ts',size=0)
        with self.sessions() as db: discover_recordings(db,self.settings);self.assertEqual(db.query(Job).count(),0)

    def test_autoqueue_can_be_disabled(self):
        self.settings.auto_queue=False; self.add_recording('old.ts')
        with self.sessions() as db: discover_recordings(db,self.settings)
        self.add_recording('new.ts')
        with self.sessions() as db: discover_recordings(db,self.settings);self.assertEqual(db.query(Job).count(),0);self.assertEqual(db.query(Recording).filter_by(filename='new.ts').one().status,RecordingStatus.READY)

    def test_manual_queue_historical(self):
        self.add_recording()
        with self.sessions() as db: discover_recordings(db,self.settings);record=db.query(Recording).one();job=queue_recording(db,record);db.commit();self.assertEqual(job.status,JobStatus.QUEUED)

    def test_one_claim_at_a_time(self):
        with self.sessions() as db:
            for n in ('a.ts','b.ts'):
                r=Recording(filename=n,source_path=n,fingerprint=n,size_bytes=100,mtime_epoch=1,status=RecordingStatus.READY);db.add(r);db.flush();queue_recording(db,r)
            db.commit();first=claim_next_job(db);self.assertIsNotNone(first);self.assertIsNone(claim_next_job(db));self.assertEqual(db.query(Job).filter_by(status=JobStatus.QUEUED).count(),1)

    def test_stale_recovery_marks_recording_failed(self):
        with self.sessions() as db:
            r=Recording(filename='a.ts',source_path='a',fingerprint='a',size_bytes=1,mtime_epoch=1,status=RecordingStatus.READY);db.add(r);db.flush();queue_recording(db,r);db.commit();job=claim_next_job(db);job.heartbeat_at=datetime.now()-timedelta(days=1);db.commit();self.assertEqual(recover_stale_jobs(db,30),1);self.assertEqual(db.get(Recording,r.id).status,RecordingStatus.FAILED)

    def test_adapter_config_is_unique_and_isolated(self):
        engine=LegacySubprocessEngine(self.settings); source=self.add_recording();one=engine._job_config(source,self.settings.data_dir/'jobs'/'one');two=engine._job_config(source,self.settings.data_dir/'jobs'/'two');self.assertNotEqual(one,two);content=one.read_text();self.assertIn((self.settings.data_dir/'jobs'/'one'/'legacy_output').as_posix(),content);self.assertIn((self.settings.data_dir/'jobs'/'one'/'legacy_work').as_posix(),content);self.assertEqual((self.legacy/'config.yaml').read_text(),'input_file: "old.ts"\noutput_root: "old-output"\nwork_root: "old-work"\n')

    def test_interview_key_variants(self):
        self.assertEqual(interview_key('a.ts',1,2),interview_key('a.ts',1.0,2.0));self.assertNotEqual(interview_key('a.ts',1,2),interview_key('b.ts',1,2));self.assertNotEqual(interview_key('a.ts',1,2),interview_key('a.ts',1,2.001))

    def test_manifest_refresh_tracks_review_selection_and_export_state(self):
        _,iid,_,cid=self.session_fixture()
        with self.sessions() as db:
            interview=db.get(Interview,iid);save_review(db,interview,{'final_name':'Robert Vance','final_role':'Trainer','final_track':'Taupo'});select_candidate(db,interview,db.get(Candidate,cid));interview.export_state='exported';refresh_manifest(db,interview);db.commit();manifest=json.loads(Path(db.get(InterviewRevision,interview.active_revision_id).manifest_path).read_text());self.assertEqual(manifest['identity']['final_name'],'Robert Vance');self.assertEqual(manifest['review']['export_state'],'exported');self.assertNotIn(str(self.root),json.dumps(manifest))

    def test_approval_requires_name_and_candidate(self):
        _,iid,_,_=self.session_fixture(False)
        with self.sessions() as db:
            interview=db.get(Interview,iid);interview.final_name=None
            with self.assertRaises(ValueError): approve(db,self.settings,interview)

    def test_manual_capture_rejects_negative_clip_time(self):
        _,iid,_,_=self.session_fixture()
        with self.sessions() as db:
            with self.assertRaises(ValueError): capture_frame(db,self.settings,db.get(Interview,iid),-0.01)

    def test_approval_preserves_source_candidate_and_reject_audits(self):
        _,iid,_,cid=self.session_fixture()
        with self.sessions() as db:
            interview=db.get(Interview,iid);candidate=db.get(Candidate,cid);path=approve(db,self.settings,interview);db.commit();self.assertTrue(path.exists());self.assertTrue(Path(candidate.image_path).exists());reject(db,interview,'false_positive');db.commit();self.assertEqual(interview.rejection_reason,'false_positive');self.assertGreaterEqual(db.query(ReviewEvent).filter_by(interview_id=iid).count(),2)

    def test_cleanup_is_dry_and_does_not_propose_source(self):
        self.add_recording();(self.settings.data_dir/'jobs'/'stale').mkdir(parents=True);(self.settings.data_dir/'jobs'/'stale'/'log.txt').write_text('x');plan=cleanup_plan(self.settings);self.assertTrue(plan['dry_run']);self.assertTrue((self.source/'recording.ts').exists());self.assertTrue(all('source' not in item for item in plan['candidates']))

    def test_path_helpers_block_common_traversal(self):
        root=self.settings.data_dir;root.mkdir()
        for value in (root/'..'/'x',Path('../x'),Path('C:/Windows/system.ini')):
            with self.assertRaises(ValueError): contained_path(root,value)
        with self.assertRaises(ValueError): artifact_path(root,'../outside.jpg')
        with self.assertRaises(ValueError): source_path(self.source,'../outside.ts')

    def test_api_health_routes_and_errors(self):
        _,iid,_,cid=self.session_fixture();app=create_app(self.settings)
        with TestClient(app) as client:
            self.assertEqual(client.get('/api/health').status_code,200);self.assertEqual(client.get('/').status_code,200);self.assertIn('Frame Pull Service',client.get('/').text);self.assertEqual(client.get('/review').status_code,200);self.assertIn('Use this frame',client.get(f'/review/{iid}').text)
            self.assertEqual(client.get('/api/interviews/9999').status_code,404);self.assertEqual(client.get('/api/interviews/9999/clip').status_code,404);self.assertEqual(client.post('/api/interviews/9999/approve').status_code,404);self.assertEqual(client.post(f'/api/interviews/{iid}/select-candidate',json={'candidate_id':9999}).status_code,404)

    def test_media_helpers_with_synthetic_audio_and_silent_video(self):
        audio=self.root/'audio.mp4';silent=self.root/'silent.mp4';
        subprocess.run(['ffmpeg','-y','-f','lavfi','-i','testsrc=size=128x72:rate=10','-f','lavfi','-i','sine=frequency=440','-t','2','-pix_fmt','yuv420p',str(audio)],check=True,capture_output=True)
        subprocess.run(['ffmpeg','-y','-f','lavfi','-i','testsrc=size=128x72:rate=10','-t','2','-pix_fmt','yuv420p',str(silent)],check=True,capture_output=True)
        self.assertAlmostEqual(ffprobe_duration('ffprobe',audio),2,places=1);out=self.root/'clip.mp4';create_clip('ffmpeg',audio,out,0,1);self.assertTrue(out.exists());frame=self.root/'frame.jpg';extract_frame('ffmpeg',silent,frame,0.5)
        with Image.open(frame) as image: self.assertEqual(image.size,(128,72))
        self.assertEqual(clip_bounds(0.2,1.8,2,5),(0.0,2))

    def test_clip_bounds_preserve_inner_window(self): self.assertEqual(clip_bounds(5,8,20,2),(3,10))

    def test_manifest_write_replaces_existing_json(self):
        path=self.root/'manifest.json';path.write_text('{"old": true}',encoding='utf-8');write_manifest(path,{'new':'value'})
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')),{'new':'value'});self.assertFalse(path.with_suffix('.json.tmp').exists())

    def test_containment_window_prevents_padding_across_known_cut(self):
        segment={'detected_start':10,'detected_end':15,'start':6,'end':20,'candidate_window_start':10,'candidate_window_end':16,'shot_boundaries':[{'time':16}]}
        self.assertEqual(contained_clip_bounds(segment,30,5),(10.0,15.0,'authenticated_interval_containment'))

    def test_containment_uses_overlap_when_candidate_window_is_wider_than_authentication(self):
        segment={'detected_start':12,'detected_end':14,'start':6,'end':20,'candidate_window_start':8,'candidate_window_end':18,'shot_boundaries':[{'time':18}]}
        self.assertEqual(contained_clip_bounds(segment,30,5),(12.0,14.0,'authenticated_interval_containment'))

    def test_containment_falls_back_to_segment_padding_without_boundaries(self):
        segment={'detected_start':10,'detected_end':15,'start':6,'end':20,'candidate_window_start':10,'candidate_window_end':16,'shot_boundaries':[]}
        self.assertEqual(contained_clip_bounds(segment,30,5),(1.0,25.0,'segment_padding'))

    def test_clip_bounds_clamp_start_only(self): self.assertEqual(clip_bounds(1,8,20,4),(0.0,12))

    def test_clip_bounds_clamp_end_only(self): self.assertEqual(clip_bounds(15,19,20,3),(12,20))

    def test_relative_artifact_is_portable(self):
        path=self.settings.data_dir/'interviews'/'x'/'poster.jpg';path.parent.mkdir(parents=True);path.write_bytes(b'x');self.assertEqual(relative_artifact(self.settings.data_dir,path),'interviews/x/poster.jpg')

    def test_source_path_requires_ts_and_containment(self):
        self.add_recording('ok.ts');self.assertEqual(source_path(self.source,'ok.ts').name,'ok.ts')
        with self.assertRaises(ValueError): source_path(self.source,'note.txt')

    def test_failed_job_can_be_requeued(self):
        with self.sessions() as db:
            r=Recording(filename='a.ts',source_path='a',fingerprint='a',size_bytes=1,mtime_epoch=1,status=RecordingStatus.FAILED);db.add(r);db.flush();j=queue_recording(db,r);db.commit();self.assertEqual(j.status,JobStatus.QUEUED)

    def test_queue_persists_across_reopen(self):
        with self.sessions() as db:
            r=Recording(filename='a.ts',source_path='a',fingerprint='a',size_bytes=1,mtime_epoch=1,status=RecordingStatus.READY);db.add(r);db.flush();queue_recording(db,r);db.commit()
        with self.sessions() as db:self.assertEqual(db.query(Job).filter_by(status=JobStatus.QUEUED).count(),1)

    def test_review_events_capture_old_and_new_values(self):
        _,iid,_,_=self.session_fixture()
        with self.sessions() as db:
            i=db.get(Interview,iid);save_review(db,i,{'final_name':'New Name','final_role':None,'final_track':None});db.commit();event=db.query(ReviewEvent).filter_by(interview_id=iid,event_type='review_saved').one();self.assertEqual(event.payload['before']['final_name'],'BOB VANCE');self.assertEqual(event.payload['after']['final_name'],'New Name')

    def test_reject_preserves_review_evidence(self):
        _,iid,_,_=self.session_fixture()
        with self.sessions() as db:i=db.get(Interview,iid);reject(db,i,'false_positive');db.commit();self.assertEqual(i.review_status,ReviewStatus.REJECTED);self.assertEqual(i.rejection_reason,'false_positive')

    def test_manifest_json_is_valid_after_refresh(self):
        _,iid,rid,_=self.session_fixture()
        with self.sessions() as db:i=db.get(Interview,iid);refresh_manifest(db,i);db.commit();self.assertIsInstance(json.loads(Path(db.get(InterviewRevision,rid).manifest_path).read_text()),dict)

    def test_api_recording_unknown_is_404(self):
        app=create_app(self.settings)
        with TestClient(app) as client:self.assertEqual(client.get('/api/recordings/999').status_code,404);self.assertEqual(client.post('/api/recordings/999/queue').status_code,404)

    def test_api_range_serving_and_manifest_no_paths(self):
        _,iid,rid,_=self.session_fixture();app=create_app(self.settings)
        with TestClient(app) as client:
            response=client.get(f'/api/interviews/{iid}/clip',headers={'Range':'bytes=0-2'});self.assertEqual(response.status_code,206);self.assertEqual(response.headers['content-range'],'bytes 0-2/5');manifest=client.get(f'/api/interviews/{iid}/manifest').json();self.assertNotIn(str(self.root),json.dumps(manifest))

    def test_api_review_and_approval_contract(self):
        _,iid,_,_=self.session_fixture();app=create_app(self.settings)
        with TestClient(app) as client:
            self.assertEqual(client.patch(f'/api/interviews/{iid}/review',json={'final_name':'Bob Vance','final_role':'Trainer','final_track':'Taupo'}).status_code,200);self.assertEqual(client.post(f'/api/interviews/{iid}/approve').status_code,200);self.assertEqual(len(client.get('/api/interviews',params={'status':'approved','export_state':'pending'}).json()),1);self.assertEqual(client.post(f'/api/interviews/{iid}/mark-exported').status_code,200);self.assertEqual(client.post(f'/api/interviews/{iid}/mark-exported').status_code,200)

    def test_api_reject_excludes_export_pending(self):
        _,iid,_,_=self.session_fixture();app=create_app(self.settings)
        with TestClient(app) as client:
            self.assertEqual(client.post(f'/api/interviews/{iid}/reject',json={'reason':'false_positive'}).status_code,200);self.assertEqual(client.get('/api/interviews',params={'status':'approved','export_state':'pending'}).json(),[])

    def test_health_does_not_leak_source_path(self):
        app=create_app(self.settings)
        with TestClient(app) as client:self.assertNotIn(str(self.source),client.get('/api/health').text)

    def test_cleanup_plan_keeps_approved_and_manifests_protected(self):
        plan=cleanup_plan(self.settings);self.assertIn('approved portraits',plan['protected']);self.assertIn('manifests',plan['protected'])

    def test_legacy_config_preserves_unknown_settings(self):
        (self.legacy/'config.yaml').write_text('sample_fps: 4\ninput_file: old\noutput_root: old\nwork_root: old\n');source=self.add_recording();config=LegacySubprocessEngine(self.settings)._job_config(source,self.settings.data_dir/'job');self.assertIn('sample_fps: 4',config.read_text())

    def test_candidate_selection_cannot_cross_active_revision(self):
        _,iid,rid,cid=self.session_fixture()
        with self.sessions() as db:
            i=db.get(Interview,iid);other=Candidate(revision_id=rid,rank=2,source_timestamp=2,image_path=str(self.root/'other.jpg'));db.add(other);db.flush();i.active_revision_id=None
            with self.assertRaises(ValueError):select_candidate(db,i,other)

    def test_approved_portrait_is_returned_by_api(self):
        _,iid,_,_=self.session_fixture();app=create_app(self.settings)
        with TestClient(app) as client:client.post(f'/api/interviews/{iid}/approve');self.assertEqual(client.get(f'/api/interviews/{iid}/portrait').status_code,200)

    def test_manifest_selected_candidate_matches_database(self):
        _,iid,_,cid=self.session_fixture()
        with self.sessions() as db:
            i=db.get(Interview,iid);refresh_manifest(db,i);db.commit();manifest=db.get(InterviewRevision,i.active_revision_id).manifest;self.assertTrue(next(c for c in manifest['candidates'] if c['id']==cid)['selected'])

    def test_full_clip_response_is_successful(self):
        _,iid,_,_=self.session_fixture();app=create_app(self.settings)
        with TestClient(app) as client:self.assertEqual(client.get(f'/api/interviews/{iid}/clip').status_code,200)

    def test_duplicate_queue_returns_existing_job(self):
        with self.sessions() as db:
            r=Recording(filename='once.ts',source_path='once',fingerprint='once',size_bytes=1,mtime_epoch=1,status=RecordingStatus.READY);db.add(r);db.flush();a=queue_recording(db,r);b=queue_recording(db,r);self.assertEqual(a.id,b.id);self.assertEqual(db.query(Job).count(),1)

    def test_launch_scripts_do_not_invoke_legacy_all_mode(self):
        scripts=Path(__file__).parents[1]/'scripts';content='\n'.join(path.read_text() for path in scripts.glob('*.ps1'));self.assertNotIn('profile_extractor.py --all',content);self.assertNotIn(' --rerun',content)

    def test_run_service_script_uses_localhost_port(self):
        content=(Path(__file__).parents[1]/'scripts'/'run_service.ps1').read_text();self.assertIn('--host 127.0.0.1',content);self.assertIn('--port 8094',content)
