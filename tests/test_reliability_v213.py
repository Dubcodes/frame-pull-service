from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from frame_pull_service.config import Settings
from frame_pull_service.db import create_db_engine, init_db, make_session_factory
from frame_pull_service.main import create_app
from frame_pull_service.models import (
    Candidate, Interview, InterviewRevision, Job, JobStatus, PlaybackProxy, ProcessingRun,
    RaceDay, Recording, RecordingMediaAudit, RecordingStatus, ReviewEvent, SourceDeletionAudit,
    SourceProtectionAudit,
)
from frame_pull_service.services.manual_recovery import create_manual_interview, validate_manual_interval
from frame_pull_service.services.media_audit import audit_recording
from frame_pull_service.services.operations import seed_settings, update_settings
from frame_pull_service.services.playback import cleanup_playback_cache, create_playback_proxy
from frame_pull_service.services.queue import claim_next_job, queue_recording
from frame_pull_service.services.race_days import race_day_archive
from frame_pull_service.services.source_lifecycle import delete_if_eligible, deletion_eligibility, set_source_protection


class ReliabilityV213Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.source = self.root / "recordings"; self.source.mkdir()
        self.data = self.root / "data"; self.data.mkdir()
        self.settings = Settings(
            source_dir=self.source, data_dir=self.data,
            database_url=f"sqlite:///{(self.root / 'frame_pull.db').as_posix()}",
            min_input_bytes=1, playback_cache_min_free_gb=0, playback_cache_max_bytes=10_000,
            manual_interview_max_seconds=120,
        )
        self.settings.ensure_data_dirs()
        self.engine = create_db_engine(self.settings); init_db(self.engine); self.sessions = make_session_factory(self.engine)
        with self.sessions() as session: seed_settings(session, self.settings)

    def tearDown(self):
        self.engine.dispose(); self.temp.cleanup()

    def recording(self, name="trackside_20261115-1200_00.ts", *, started=None, duration=30, closed=True):
        path = self.source / name; path.write_bytes(b"source-media-" * 100)
        with self.sessions() as session:
            day = session.query(RaceDay).filter_by(race_date=date(2026, 11, 15)).one_or_none()
            if day is None:
                day = RaceDay(race_date=date(2026, 11, 15), label="Sunday 15 November 2026"); session.add(day); session.flush()
            row = Recording(
                filename=name, source_path=str(path), fingerprint=name, size_bytes=path.stat().st_size,
                mtime_epoch=path.stat().st_mtime, status=RecordingStatus.READY,
                stable_at=datetime.utcnow() if closed else None, is_closed=closed, race_day_id=day.id,
                recording_started_at=started or datetime(2026, 11, 15, 12, 0), duration_seconds=duration,
                recording_stopped_at=(started or datetime(2026, 11, 15, 12, 0)) + timedelta(seconds=duration),
            )
            session.add(row); session.commit(); return row.id

    def runner(self, *, warning="", scan_exit=0, audio=True, probe_ok=True):
        def run(command, **_kwargs):
            if "-version" in command:
                return subprocess.CompletedProcess(command, 0, stdout=f"{command[0]} version fixture\n", stderr="")
            if command[0] == "ffprobe":
                streams = [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "avg_frame_rate": "25/1"}]
                if audio: streams.append({"codec_type": "audio", "codec_name": "aac"})
                payload = {"format": {"duration": "30.0", "format_name": "mpegts", "start_time": "1.4"}, "streams": streams}
                return subprocess.CompletedProcess(command, 0 if probe_ok else 1, stdout=json.dumps(payload) if probe_ok else "", stderr="probe failed" if not probe_ok else "")
            return subprocess.CompletedProcess(command, scan_exit, stdout="out_time_us=30000000\nprogress=end\n", stderr=warning)
        return run

    def healthy_row(self, session, recording_id):
        row = RecordingMediaAudit(
            recording_id=recording_id, tool_version="fixture", probe_ok=True, full_scan_ok=True,
            classification="healthy", duration_seconds=30, traversed_seconds=30,
            has_video=True, has_audio=True, error_count=0, summary="healthy", evidence={},
        )
        session.add(row); return row

    def test_media_audit_classifies_healthy_warning_and_damaged_sources(self):
        healthy = self.recording()
        warning = self.recording("trackside_20261115-1300_00.ts", started=datetime(2026, 11, 15, 13, 0))
        damaged = self.recording("trackside_20261115-1400_00.ts", started=datetime(2026, 11, 15, 14, 0))
        with self.sessions() as session:
            self.assertEqual(audit_recording(session, self.settings, session.get(Recording, healthy), runner=self.runner()).classification, "healthy")
            warned = audit_recording(session, self.settings, session.get(Recording, warning), runner=self.runner(warning="packet corrupt at dts 42"))
            self.assertEqual(warned.classification, "warning"); self.assertTrue(session.get(Recording, warning).source_protected)
            failed = audit_recording(session, self.settings, session.get(Recording, damaged), runner=self.runner(warning="Invalid data found", scan_exit=1))
            self.assertEqual(failed.classification, "failed"); self.assertTrue(session.get(Recording, damaged).source_protected)
            self.assertNotIn(str(self.source), json.dumps(failed.evidence))

    def test_active_source_cannot_be_integrity_scanned(self):
        recording_id = self.recording(closed=False)
        with self.sessions() as session:
            with self.assertRaisesRegex(ValueError, "active or not stable"):
                audit_recording(session, self.settings, session.get(Recording, recording_id), runner=self.runner())

    def test_integrity_and_manual_protection_are_deletion_gates(self):
        recording_id = self.recording()
        with self.sessions() as session:
            session.add(Job(recording_id=recording_id, status=JobStatus.COMPLETE))
            session.add(RecordingMediaAudit(recording_id=recording_id, tool_version="fixture", probe_ok=True, full_scan_ok=False,
                classification="failed", has_video=True, has_audio=True, error_count=1, summary="damaged", evidence={}))
            update_settings(session, {"source_deletion_enabled": True, "source_retention_hours": 0})
            result = deletion_eligibility(session, self.settings, session.get(Recording, recording_id))
            self.assertFalse(result["checks"]["integrity_healthy"]); self.assertFalse(result["safe_to_delete"])
            set_source_protection(session, session.get(Recording, recording_id), True, reason="investigation")
            self.assertFalse(deletion_eligibility(session, self.settings, session.get(Recording, recording_id))["checks"]["not_manually_protected"])
            set_source_protection(session, session.get(Recording, recording_id), False, reason="operator cleared")
            self.assertEqual(session.query(SourceProtectionAudit).count(), 2)

    def test_enabled_integrity_gate_prevents_detector_claim_until_audit_passes(self):
        recording_id = self.recording()
        with self.sessions() as session:
            job = queue_recording(session, session.get(Recording, recording_id)); job.released_for_processing = True
            update_settings(session, {"integrity_audit_enabled": True})
            self.assertIsNone(claim_next_job(session, paused=False))
            session.add(RecordingMediaAudit(recording_id=recording_id, tool_version="fixture", probe_ok=True, full_scan_ok=True,
                classification="warning", has_video=True, has_audio=True, error_count=1, summary="recoverable", evidence={})); session.commit()
            self.assertEqual(claim_next_job(session, paused=False).id, job.id)

    def test_manual_safe_delete_uses_canonical_lifecycle_and_audit(self):
        recording_id = self.recording()
        with self.sessions() as session:
            session.add(Job(recording_id=recording_id, status=JobStatus.COMPLETE)); self.healthy_row(session, recording_id)
            update_settings(session, {"source_deletion_enabled": True, "source_retention_hours": 0})
            result = delete_if_eligible(session, self.settings, session.get(Recording, recording_id))
            self.assertTrue(result["deleted"]); self.assertFalse((self.source / "trackside_20261115-1200_00.ts").exists())
            self.assertEqual(session.query(SourceDeletionAudit).count(), 1)

    def test_race_day_archive_orders_chunks_maps_interviews_and_counts_manual(self):
        later = self.recording("trackside_20261115-1300_00.ts", started=datetime(2026, 11, 15, 13, 0))
        earlier = self.recording("trackside_20261115-1200_00.ts", started=datetime(2026, 11, 15, 12, 0))
        with self.sessions() as session:
            run = ProcessingRun(recording_id=earlier, run_key="auto", job_dir="x", status="complete"); session.add(run); session.flush()
            auto = Interview(interview_key="auto-int", recording_id=earlier, source_start=3, source_end=8, bounds_basis="authenticated_interval_containment")
            manual = Interview(interview_key="manual-int", recording_id=later, source_start=4, source_end=9, bounds_basis="manual_operator")
            session.add_all([auto, manual]); session.flush()
            for item in (auto, manual):
                revision = InterviewRevision(interview_id=item.id, processing_run_id=run.id, revision_number=1, clip_start=item.source_start,
                    clip_end=item.source_end, clip_path="missing", poster_path="missing", manifest_path="missing")
                session.add(revision); session.flush(); item.active_revision_id = revision.id
            self.healthy_row(session, earlier); self.healthy_row(session, later); session.commit()
            archive = race_day_archive(session, self.settings, session.query(RaceDay).one())
            self.assertEqual([row["id"] for row in archive["recordings"]], [earlier, later])
            self.assertEqual(archive["automated_interviews"], 1); self.assertEqual(archive["manually_recovered_interviews"], 1)
            self.assertEqual({row["recording_id"] for row in archive["interview_rows"]}, {earlier, later})

    def test_playback_proxy_is_contained_does_not_mutate_source_and_cache_is_lru(self):
        first = self.recording(); second = self.recording("trackside_20261115-1300_00.ts", started=datetime(2026, 11, 15, 13, 0)); before = hashlib.sha256((self.source / "trackside_20261115-1200_00.ts").read_bytes()).hexdigest()
        def runner(command, **_kwargs):
            if command[0] == "ffprobe":
                return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"streams":[{"codec_type":"video","codec_name":"h264"},{"codec_type":"audio","codec_name":"aac"}]}), stderr="")
            Path(command[-1]).write_bytes(b"proxy-one"); return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        with self.sessions() as session:
            proxy = create_playback_proxy(session, self.settings, session.get(Recording, first), runner=runner)
            self.assertEqual(proxy.mode, "remux"); self.assertTrue((self.data / proxy.relative_path).is_file())
            self.assertEqual(before, hashlib.sha256((self.source / "trackside_20261115-1200_00.ts").read_bytes()).hexdigest())
            second_file = self.data / "playback_cache" / f"recording_{second}.mp4"; second_file.write_bytes(b"x" * 20)
            session.add(PlaybackProxy(recording_id=second, relative_path=str(second_file.relative_to(self.data)), size_bytes=20,
                mode="remux", last_accessed_at=datetime.utcnow()-timedelta(days=1))); session.commit()
            update_settings(session, {"playback_cache_max_bytes": 10})
            status = cleanup_playback_cache(session, self.settings, preserve_recording_id=first)
            self.assertGreaterEqual(status["deleted"], 1); self.assertTrue((self.source / "trackside_20261115-1200_00.ts").exists())

    def test_missing_source_playback_is_safe_404(self):
        recording_id = self.recording(); (self.source / "trackside_20261115-1200_00.ts").unlink()
        app = create_app(self.settings)
        with TestClient(app) as client:
            self.assertEqual(client.get(f"/api/recordings/{recording_id}/playback").status_code, 404)
            self.assertEqual(client.get(f"/api/recordings/{recording_id}/download").status_code, 404)

    def test_archive_api_and_page_hide_internal_paths(self):
        recording_id = self.recording(); app = create_app(self.settings)
        with TestClient(app) as client:
            with self.sessions() as session: day_id = session.get(Recording, recording_id).race_day_id
            self.assertEqual(client.get(f"/race-days/{day_id}").status_code, 200)
            response = client.get(f"/api/race-days/{day_id}/archive")
            self.assertEqual(response.status_code, 200); self.assertNotIn(str(self.root), response.text)
            self.assertEqual(response.json()["recordings"][0]["id"], recording_id)

    def test_manual_interval_validation_and_evidence_creation_preserve_detector_history(self):
        recording_id = self.recording(duration=60)
        with self.sessions() as session:
            recording = session.get(Recording, recording_id)
            with self.assertRaises(ValueError): validate_manual_interval(self.settings, recording, 10, 5)
            with self.assertRaises(ValueError): validate_manual_interval(self.settings, recording, 10, 200)
            original = ProcessingRun(recording_id=recording_id, run_key="detector-original", job_dir="detector", status="complete", summary={"interviews": 0})
            session.add(original); session.commit(); original_id = original.id
            def clip(_ffmpeg, _source, destination, _start, _end): destination.parent.mkdir(parents=True, exist_ok=True); destination.write_bytes(b"clip")
            def frame(_ffmpeg, _source, destination, _time): destination.parent.mkdir(parents=True, exist_ok=True); destination.write_bytes(b"jpg")
            with patch("frame_pull_service.services.manual_recovery.create_clip", clip), patch("frame_pull_service.services.manual_recovery.extract_frame", frame):
                interview = create_manual_interview(session, self.settings, recording, start=10, end=20)
            self.assertEqual(interview.bounds_basis, "manual_operator"); self.assertIsNone(interview.final_name)
            self.assertEqual(session.query(Candidate).filter(Candidate.revision_id == interview.active_revision_id).count(), 3)
            self.assertEqual(session.query(ReviewEvent).filter_by(interview_id=interview.id, event_type="manual_recovery_created").count(), 1)
            self.assertEqual(session.get(ProcessingRun, original_id).summary, {"interviews": 0})
            revision = session.get(InterviewRevision, interview.active_revision_id)
            self.assertTrue(Path(revision.clip_path).is_file()); self.assertTrue(Path(revision.poster_path).is_file()); self.assertTrue(Path(revision.manifest_path).is_file())
            self.assertTrue(json.loads(Path(revision.manifest_path).read_text())["recovery"]["manual"])

    def test_archive_frontend_has_chunk_advance_timeline_filters_and_safe_actions(self):
        js = (Path(__file__).resolve().parents[1] / "src" / "frame_pull_service" / "web" / "static" / "race_day.js").read_text()
        html = (Path(__file__).resolve().parents[1] / "src" / "frame_pull_service" / "web" / "templates" / "race_day.html").read_text()
        self.assertIn("player.addEventListener('ended'", js); self.assertIn("timeline-marker", js)
        self.assertIn("manual-interviews/validate", js); self.assertIn("delete-source", js); self.assertIn("Download original .ts", html)
        self.assertIn("Protect all stored sources", html); self.assertIn("option value=\"unresolved\"", html)


if __name__ == "__main__":
    unittest.main()
