from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from frame_pull_service.config import Settings
from frame_pull_service.db import create_db_engine, init_db, make_session_factory
from frame_pull_service.models import (
    Candidate, Interview, InterviewRevision, Job, JobStatus, ProcessingRun, Recording,
    RecordingStatus, ReviewStatus,
)
from frame_pull_service.services.discovery import discover_recordings, recording_fingerprint
from frame_pull_service.services.paths import resolve_recording_source
from frame_pull_service.services.relocation import (
    MissingSourceAllowance, RelocationRoots, apply_runtime_relocation, build_missing_source_manifest,
    load_missing_source_manifest, map_approved_path, plan_runtime_relocation, semantic_snapshot,
)


class RuntimeRelocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_source = self.root / "windows-recordings"
        self.new_source = self.root / "linux-recordings"
        self.old_data = self.root / "windows-data"
        self.new_data = self.root / "linux-data"
        for path in (self.old_source, self.new_source, self.old_data, self.new_data):
            path.mkdir()
        self.database = self.root / "state.sqlite"
        self.settings = Settings(source_dir=self.new_source, data_dir=self.new_data,
                                 database_url=f"sqlite:///{self.database.as_posix()}", min_input_bytes=1,
                                 stable_for_seconds=0)
        self.engine = create_db_engine(self.settings)
        init_db(self.engine)
        self.sessions = make_session_factory(self.engine)
        self.source = self.new_source / "abc.ts"
        self.source.write_bytes(b"same-recording")
        stat = self.source.stat()
        old_source = self.old_source / self.source.name

        package = self.new_data / "interviews" / "int_test" / "revision_001"
        candidates = package / "candidates"
        candidates.mkdir(parents=True)
        for path, content in ((package / "clip.mp4", b"clip"), (package / "poster.jpg", b"poster"),
                              (package / "manifest.json", b"{}"), (candidates / "candidate_001.jpg", b"image")):
            path.write_bytes(content)
        job_dir = self.new_data / "jobs" / "job_000001"
        job_dir.mkdir(parents=True)
        (job_dir / "legacy_engine.log").write_text("log")

        with self.sessions() as session:
            recording = Recording(filename=self.source.name, source_path=str(old_source), fingerprint="windows-fingerprint",
                                  size_bytes=stat.st_size, mtime_epoch=stat.st_mtime, status=RecordingStatus.QUEUED,
                                  historical=True, stable_at=datetime.now(), is_closed=True)
            session.add(recording); session.flush()
            job = Job(recording_id=recording.id, status=JobStatus.QUEUED, released_for_processing=False,
                      log_path=str(self.old_data / "jobs" / "job_000001" / "legacy_engine.log"))
            session.add(job); session.flush()
            run = ProcessingRun(recording_id=recording.id, run_key="run-test", status="complete",
                                job_dir=str(self.old_data / "jobs" / "job_000001"),
                                summary={"bounded_path": str(self.old_data / "jobs" / "job_000001")})
            session.add(run); session.flush()
            interview = Interview(interview_key="int_test", recording_id=recording.id, source_start=1, source_end=2,
                                  bounds_basis="test", review_status=ReviewStatus.APPROVED, export_state="exported",
                                  final_name="Ryan Foote", identity_source="manual")
            session.add(interview); session.flush()
            revision = InterviewRevision(
                interview_id=interview.id, processing_run_id=run.id, revision_number=1, clip_start=1, clip_end=2,
                clip_path=str(self.old_data / "interviews" / "int_test" / "revision_001" / "clip.mp4"),
                poster_path=str(self.old_data / "interviews" / "int_test" / "revision_001" / "poster.jpg"),
                manifest_path=str(self.old_data / "interviews" / "int_test" / "revision_001" / "manifest.json"),
                manifest={"artifacts": {"poster": "interviews/int_test/revision_001/poster.jpg"}},
            )
            session.add(revision); session.flush()
            candidate = Candidate(revision_id=revision.id, rank=1, source_timestamp=1.5,
                                  image_path=str(self.old_data / "interviews" / "int_test" / "revision_001" / "candidates" / "candidate_001.jpg"),
                                  selected=True)
            session.add(candidate); session.flush()
            interview.active_revision_id = revision.id
            interview.selected_candidate_id = candidate.id
            job.processing_run_id = run.id
            session.commit()
            self.ids = (recording.id, job.id, run.id, interview.id, revision.id, candidate.id)
        self.roots = RelocationRoots(str(self.old_source), str(self.new_source), str(self.old_data), str(self.new_data))

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    def test_cross_platform_examples_map_lexically(self):
        self.assertEqual(
            map_approved_path(r"J:\frame-pull\trackside_recordings\abc.ts", r"J:\frame-pull\trackside_recordings", "/recordings"),
            "/recordings/abc.ts",
        )
        self.assertEqual(
            map_approved_path(r"J:\projects\frame-pull-service\data\interviews\x\clip.mp4",
                              r"J:\projects\frame-pull-service\data", "/data"),
            "/data/interviews/x/clip.mp4",
        )

    def test_dry_run_apply_preserves_semantics_and_is_idempotent(self):
        with self.sessions() as session:
            before = semantic_snapshot(session)
            plan = plan_runtime_relocation(session, self.roots)
            self.assertEqual(plan.unresolved, [])
            self.assertGreaterEqual(len(plan.changes), 8)
            session.rollback()
        with self.sessions() as session:
            recording = session.get(Recording, self.ids[0])
            self.assertEqual(recording.source_path, str(self.old_source / "abc.ts"))
            before_apply = semantic_snapshot(session)
            plan = plan_runtime_relocation(session, self.roots)
            apply_runtime_relocation(session, plan, self.roots)
            self.assertEqual(semantic_snapshot(session), before_apply)
            session.commit()
        with self.sessions() as session:
            self.assertEqual(semantic_snapshot(session), before)
            recording = session.get(Recording, self.ids[0])
            self.assertEqual(recording.source_path, str(self.source))
            self.assertEqual((recording.id, session.get(Job, self.ids[1]).id), self.ids[:2])
            interview = session.get(Interview, self.ids[3])
            self.assertEqual((interview.review_status, interview.export_state, interview.final_name),
                             (ReviewStatus.APPROVED, "exported", "Ryan Foote"))
            second = plan_runtime_relocation(session, self.roots)
            self.assertEqual(second.changes, [])
            self.assertEqual(second.unresolved, [])

    def test_unknown_external_path_is_reported_and_blocks_apply(self):
        with self.sessions() as session:
            session.get(Job, self.ids[1]).log_path = str(self.root / "external" / "log.txt")
            session.commit()
            plan = plan_runtime_relocation(session, self.roots)
            self.assertTrue(any(item.table == "jobs" and item.column == "log_path" for item in plan.unresolved))
            with self.assertRaisesRegex(ValueError, "unresolved"):
                apply_runtime_relocation(session, plan, self.roots)

    def test_source_size_collision_is_not_accepted(self):
        self.source.write_bytes(b"different")
        with self.sessions() as session:
            plan = plan_runtime_relocation(session, self.roots)
            self.assertTrue(any("size conflicts" in item.reason for item in plan.unresolved))
            self.assertFalse(any(item.table == "recordings" for item in plan.changes))

    def test_known_allowlisted_missing_source_relocates_and_is_idempotent(self):
        self.source.unlink()
        allowance=MissingSourceAllowance(self.ids[0],"abc.ts",len(b"same-recording"),"physical_source_unavailable")
        with self.sessions() as session:
            before=semantic_snapshot(session);plan=plan_runtime_relocation(session,self.roots,missing_source_allowances={self.ids[0]:allowance})
            self.assertEqual(plan.missing_targets,[]);self.assertEqual(plan.allowed_missing_sources,[allowance])
            apply_runtime_relocation(session,plan,self.roots);self.assertEqual(semantic_snapshot(session),before);session.commit()
        with self.sessions() as session:
            second=plan_runtime_relocation(session,self.roots,missing_source_allowances={self.ids[0]:allowance})
            self.assertEqual(second.changes,[]);self.assertEqual(second.unresolved,[]);self.assertEqual(second.missing_targets,[])

    def test_unknown_missing_source_still_blocks_apply(self):
        self.source.unlink()
        with self.sessions() as session:
            plan=plan_runtime_relocation(session,self.roots)
            self.assertEqual(len([item for item in plan.missing_targets if item.table=="recordings"]),1)
            with self.assertRaisesRegex(ValueError,"missing recording sources"): apply_runtime_relocation(session,plan,self.roots)

    def test_generated_manifest_is_exact_and_loadable(self):
        self.source.unlink()
        manifest_path=self.root/"missing.json"
        with self.sessions() as session: document=build_missing_source_manifest(session,self.new_source)
        import json
        manifest_path.write_text(json.dumps(document),encoding="utf-8")
        loaded=load_missing_source_manifest(manifest_path)
        self.assertEqual(list(loaded),[self.ids[0]]);self.assertEqual(loaded[self.ids[0]].filename,"abc.ts")

    def test_authoritative_resolver_ignores_stale_stored_host_path(self):
        with self.sessions() as session:
            recording = session.get(Recording, self.ids[0])
            self.assertEqual(resolve_recording_source(self.settings, recording, verify_size=True), self.source.resolve())

    def test_discovery_refreshes_verified_path_but_rejects_conflict(self):
        with self.sessions() as session:
            recording = session.get(Recording, self.ids[0])
            stat = self.source.stat()
            recording.fingerprint = recording_fingerprint(self.source, stat)
            session.commit()
            result = discover_recordings(session, self.settings, datetime.now())
            self.assertEqual(result["conflicts"], 0)
            self.assertEqual(session.get(Recording, self.ids[0]).source_path, str(self.source.resolve()))
        with self.sessions() as session:
            recording = session.get(Recording, self.ids[0])
            recording.source_path = str(self.old_source / "abc.ts")
            recording.fingerprint = "not-the-current-file"
            recording.size_bytes += 1
            session.commit()
            result = discover_recordings(session, self.settings, datetime.now())
            self.assertEqual(result["conflicts"], 1)
            self.assertEqual(session.get(Recording, self.ids[0]).source_path, str(self.old_source / "abc.ts"))


if __name__ == "__main__":
    unittest.main()
