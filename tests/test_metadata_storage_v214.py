from __future__ import annotations

import tempfile
import unittest
from collections import namedtuple
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from fastapi.testclient import TestClient

from frame_pull_service.config import Settings
from frame_pull_service.db import create_db_engine, init_db, make_session_factory
from frame_pull_service.main import create_app
from frame_pull_service.models import (
    AppearanceGroup, CalendarMeeting, Candidate, Interview, InterviewMetadataAnalysis,
    InterviewRevision, ProcessingRun, RaceDay, Recording, RecordingStatus,
    ReviewStatus,
)
from frame_pull_service.services.groups import sync_groups
from frame_pull_service.services.interview_metadata import (
    analyze_interview, bulk_candidates, name_consensus, normalize_code,
)
from frame_pull_service.services.operations import seed_settings, update_settings
from frame_pull_service.services.recorder import FFmpegRecorder
from frame_pull_service.services.storage import optional_work_allowed, relieve_storage_pressure, storage_metrics


class MetadataStorageV214Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "recordings"; self.source.mkdir()
        self.data = self.root / "data"; self.data.mkdir()
        self.settings = Settings(source_dir=self.source, data_dir=self.data,
            database_url=f"sqlite:///{(self.root/'frame_pull.db').as_posix()}", min_input_bytes=1)
        self.settings.ensure_data_dirs()
        self.engine = create_db_engine(self.settings); init_db(self.engine)
        self.sessions = make_session_factory(self.engine)
        with self.sessions() as session: seed_settings(session, self.settings)

    def tearDown(self):
        self.engine.dispose(); self.temp.cleanup()

    def make_interview(self, *, status=ReviewStatus.PENDING, export="pending", name=None,
                       identity_source="unknown", code="unknown") -> int:
        media = self.source / f"trackside_20261115-12{len(list(self.source.glob('*.ts'))):02d}_00.ts"
        media.write_bytes(b"not-modified-source")
        with self.sessions() as session:
            day = session.query(RaceDay).filter_by(race_date=date(2026, 11, 15)).one_or_none()
            if day is None:
                day = RaceDay(race_date=date(2026, 11, 15), label="15 November"); session.add(day); session.flush()
                session.add(CalendarMeeting(race_day_id=day.id, track="Riccarton", provider_status="ok")); session.flush()
            recording = Recording(filename=media.name, source_path=str(media), fingerprint=media.name,
                size_bytes=media.stat().st_size, mtime_epoch=media.stat().st_mtime,
                status=RecordingStatus.COMPLETE, stable_at=datetime.utcnow(), is_closed=True,
                race_day_id=day.id, recording_started_at=datetime(2026, 11, 15, 12, 0), duration_seconds=60)
            session.add(recording); session.flush()
            run = ProcessingRun(recording_id=recording.id, run_key=f"run-{recording.id}", job_dir=f"jobs/{recording.id}", status="complete")
            session.add(run); session.flush()
            item = Interview(interview_key=f"interview-{recording.id}", recording_id=recording.id,
                source_start=5, source_end=15, bounds_basis="fixture", review_status=status,
                export_state=export, final_name=name, identity_source=identity_source, racing_code=code)
            session.add(item); session.flush()
            revision = InterviewRevision(interview_id=item.id, processing_run_id=run.id, revision_number=1,
                clip_start=5, clip_end=15, clip_path=f"interviews/{item.id}/clip.mp4",
                poster_path=f"interviews/{item.id}/poster.jpg", manifest_path=f"interviews/{item.id}/manifest.json")
            session.add(revision); session.flush(); item.active_revision_id = revision.id
            session.commit(); return item.id

    @staticmethod
    def extractor(_command, _source, target, _time):
        Image.new("RGB", (640, 360), "white").save(target)

    @staticmethod
    def clear_ocr(_command, image):
        if image.name.startswith("name_"):
            return {"lines": [{"text": "LISA LATTA", "confidence": 91},
                               {"text": "TRAINER", "confidence": 88}],
                    "text": "LISA LATTA | TRAINER"}
        return {"lines": [{"text": "RICCARTON THOROUGHBRED", "confidence": 86}],
                "text": "RICCARTON THOROUGHBRED"}

    def test_multi_frame_consensus_trusts_clear_agreement(self):
        result = name_consensus([{"lines": [{"text": "LISA LATTA", "confidence": 91}]},
                                 {"lines": [{"text": "LISA LATTA", "confidence": 87}]}])
        self.assertEqual(result["trusted"], "Lisa Latta"); self.assertEqual(result["support"], 2)

    def test_conflicting_single_reads_remain_untrusted(self):
        result = name_consensus([{"lines": [{"text": "LISA LATTA", "confidence": 90}]},
                                 {"lines": [{"text": "NIALL QUINN", "confidence": 90}]}])
        self.assertIsNone(result["trusted"]); self.assertEqual(result["reason"], "insufficient_consensus")

    def test_two_lines_from_one_frame_do_not_fake_multi_frame_support(self):
        result = name_consensus([{"lines": [{"text": "LISA LATTA", "confidence": 91},
                                              {"text": "LISA LATTA", "confidence": 88}]}])
        self.assertIsNone(result["trusted"]); self.assertEqual(result["support"], 1)

    def test_analysis_recovers_name_role_track_code_and_persists_evidence(self):
        interview_id = self.make_interview()
        with self.sessions() as session:
            result = analyze_interview(session, self.settings, session.get(Interview, interview_id),
                analyzer=self.clear_ocr, frame_extractor=self.extractor)
            item = session.get(Interview, interview_id)
            self.assertEqual((item.final_name, item.final_role, item.final_track), ("Lisa Latta", "TRAINER", "Riccarton"))
            self.assertEqual(item.racing_code, "thoroughbred")
            self.assertEqual(result["analysis_basis"], "canonical_source")
            self.assertEqual(session.query(InterviewMetadataAnalysis).count(), 1)
            self.assertEqual(len(result["source_timestamps"]), 4)

    def test_visual_track_ocr_alias_normalizes_only_from_close_text(self):
        interview_id = self.make_interview()
        def misspelled(_command, image):
            if image.name.startswith("name_"):
                return self.clear_ocr(_command, image)
            return {"lines": [{"text": "RICCART0N", "confidence": 82}], "text": "RICCART0N"}
        with self.sessions() as session:
            result = analyze_interview(session, self.settings, session.get(Interview, interview_id),
                analyzer=misspelled, frame_extractor=self.extractor)
            self.assertEqual(result["proposed_track"], "Riccarton")

    def test_manual_identity_is_never_overwritten(self):
        interview_id = self.make_interview(name="Human Confirmed", identity_source="manual")
        with self.sessions() as session:
            analyze_interview(session, self.settings, session.get(Interview, interview_id),
                analyzer=self.clear_ocr, frame_extractor=self.extractor)
            self.assertEqual(session.get(Interview, interview_id).final_name, "Human Confirmed")

    def test_approved_exported_fields_are_not_silently_applied(self):
        interview_id = self.make_interview(status=ReviewStatus.APPROVED, export="exported")
        with self.sessions() as session:
            result = analyze_interview(session, self.settings, session.get(Interview, interview_id),
                analyzer=self.clear_ocr, frame_extractor=self.extractor)
            item = session.get(Interview, interview_id)
            self.assertIsNone(item.final_name); self.assertIsNone(item.final_track)
            self.assertEqual(result["applied_fields"], []); self.assertEqual(result["proposed_name"], "Lisa Latta")

    def test_bulk_selection_prioritizes_unresolved_only(self):
        unresolved = self.make_interview(); self.make_interview(name="Resolved")
        with self.sessions() as session:
            ids = [item.id for item in bulk_candidates(session, 10)]
            self.assertIn(unresolved, ids)

    def test_racing_code_validation(self):
        self.assertEqual(normalize_code(" Harness "), "harness")
        with self.assertRaises(ValueError): normalize_code("dogs")

    def test_known_cross_code_groups_are_separate_and_unknown_does_not_merge(self):
        thoroughbred = self.make_interview(name="Same Person", code="thoroughbred")
        harness = self.make_interview(name="Same Person", code="harness")
        unknown = self.make_interview(name="Same Person", code="unknown")
        with self.sessions() as session:
            sync_groups(session)
            group_ids = {session.get(Interview, item).appearance_group_id for item in (thoroughbred, harness, unknown)}
            self.assertEqual(len(group_ids), 3)

    def test_group_upgrade_preserves_preferred_portrait_reference(self):
        interview_id = self.make_interview(name="Legacy Person", code="harness")
        with self.sessions() as session:
            item = session.get(Interview, interview_id); recording = session.get(Recording, item.recording_id)
            candidate = Candidate(revision_id=item.active_revision_id, rank=1, source_timestamp=6, image_path="interviews/test.jpg")
            session.add(candidate); session.flush()
            legacy = AppearanceGroup(race_day_id=recording.race_day_id, identity_key="legacyperson", display_name="Legacy Person", preferred_candidate_id=candidate.id)
            session.add(legacy); session.flush(); item.appearance_group_id = legacy.id; session.commit()
            sync_groups(session); upgraded = session.get(AppearanceGroup, legacy.id)
            self.assertEqual(upgraded.identity_key, "legacyperson|harness"); self.assertEqual(upgraded.preferred_candidate_id, candidate.id)

    def test_shared_legacy_unknown_group_splits_before_known_code_upgrade(self):
        unknown = self.make_interview(name="Legacy Shared", code="unknown")
        harness = self.make_interview(name="Legacy Shared", code="harness")
        with self.sessions() as session:
            first=session.get(Interview,unknown); second=session.get(Interview,harness)
            recording=session.get(Recording,first.recording_id)
            legacy=AppearanceGroup(race_day_id=recording.race_day_id,identity_key="legacyshared",display_name="Legacy Shared")
            session.add(legacy);session.flush();first.appearance_group_id=legacy.id;second.appearance_group_id=legacy.id;session.commit()
            sync_groups(session)
            self.assertNotEqual(session.get(Interview,unknown).appearance_group_id,session.get(Interview,harness).appearance_group_id)

    def test_storage_metrics_thresholds_and_no_host_paths(self):
        (self.source / "sample.ts").write_bytes(b"x" * 10)
        Usage = namedtuple("Usage", "total used free")
        with self.sessions() as session, patch("frame_pull_service.services.storage.shutil.disk_usage", return_value=Usage(100, 75, 25)):
            update_settings(session, {"storage_warning_free_gb": 1, "storage_critical_free_gb": 0, "max_managed_storage_gb": 1})
            result = storage_metrics(session, self.settings, refresh=True)
            self.assertEqual(result["filesystem"]["free"], 25); self.assertNotIn(str(self.root), str(result))

    def test_critical_storage_blocks_optional_work(self):
        Usage = namedtuple("Usage", "total used free")
        with self.sessions() as session, patch("frame_pull_service.services.storage.shutil.disk_usage", return_value=Usage(100, 99, 1)):
            update_settings(session, {"storage_critical_free_gb": 1})
            allowed, reason = optional_work_allowed(session, self.settings)
            self.assertFalse(allowed); self.assertIn("critical", reason)

    def test_automatic_recorder_predictive_guard_uses_critical_reserve(self):
        Usage = namedtuple("Usage", "total used free")
        recorder = FFmpegRecorder(self.settings, disk_usage=lambda _path: Usage(100*1024**3, 59*1024**3, 41*1024**3))
        with self.sessions() as session:
            allowed, reason = recorder._automatic_storage_safe(session)
            self.assertFalse(allowed); self.assertIn("estimated next chunk", reason)

    def test_storage_api_and_bridge_are_additive_and_path_free(self):
        interview_id = self.make_interview(name="Bridge Person", code="harness")
        with TestClient(create_app(self.settings)) as client:
            storage = client.get("/api/operations/storage?refresh=true")
            self.assertEqual(storage.status_code, 200); self.assertIn("filesystem", storage.json())
            self.assertNotIn(str(self.root), storage.text)
            bridge = client.get(f"/api/bridge/v1/interviews/{interview_id}")
            self.assertEqual(bridge.status_code, 200); self.assertEqual(bridge.json()["racing_code"], "harness")

    def test_cache_eviction_precedes_only_normal_lifecycle_sweep(self):
        order = []
        before = {"managed_cap_exceeded": True}; after_cache = {"managed_cap_exceeded": True}; after = {"managed_cap_exceeded": True}
        with self.sessions() as session, \
             patch("frame_pull_service.services.storage.storage_metrics", side_effect=[before, after_cache, after]), \
             patch("frame_pull_service.services.storage.cleanup_playback_cache", side_effect=lambda *_a, **_k: order.append("playback") or {}), \
             patch("frame_pull_service.services.storage.cleanup_candidate_preview_cache", side_effect=lambda *_a, **_k: order.append("preview") or {}), \
             patch("frame_pull_service.services.storage.sweep_source_lifecycle", side_effect=lambda *_a, **_k: order.append("lifecycle") or {"deleted": 0}), \
             patch("frame_pull_service.services.storage.source_lifecycle_status", return_value={"counts": {"protected": 1}}):
            result = relieve_storage_pressure(session, self.settings)
        self.assertEqual(order, ["playback", "preview", "lifecycle"])
        self.assertTrue(result["cleanup_blocked"]); self.assertEqual(result["source_lifecycle"]["deleted"], 0)


if __name__ == "__main__":
    unittest.main()
