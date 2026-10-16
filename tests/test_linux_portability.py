from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from frame_pull_service.config import Settings
from frame_pull_service.services.legacy_engine import LegacySubprocessEngine


class LinuxPortabilityTests(unittest.TestCase):
    def test_compose_keeps_worker_sources_read_only_and_secrets_external(self):
        root = Path(__file__).resolve().parents[1]
        compose = (root / "compose.yaml").read_text(encoding="utf-8")
        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("frame-pull-web:", compose)
        self.assertIn("frame-pull-worker:", compose)
        self.assertIn("/recordings:ro", compose)
        self.assertIn("/legacy:ro", compose)
        self.assertIn("service_healthy", compose)
        self.assertNotIn("FRAME_PULL_RECORDER_INPUT:", compose)
        self.assertIn("FROM python:3.12-slim", dockerfile)
        self.assertIn("USER 10001:10001", dockerfile)

    def test_container_paths_and_legacy_python_are_environment_configurable(self):
        environment = {
            "FRAME_PULL_SOURCE_DIR": "/recordings",
            "FRAME_PULL_DATA_DIR": "/data",
            "FRAME_PULL_LEGACY_ENGINE_ROOT": "/legacy",
            "FRAME_PULL_LEGACY_PYTHON": "/usr/local/bin/python",
            "FRAME_PULL_HOST": "0.0.0.0",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings(_env_file=None)
        self.assertEqual(settings.source_dir, Path("/recordings"))
        self.assertEqual(settings.data_dir, Path("/data"))
        self.assertEqual(settings.legacy_engine_root, Path("/legacy"))
        self.assertEqual(settings.legacy_python, Path("/usr/local/bin/python"))
        self.assertEqual(settings.host, "0.0.0.0")
        self.assertNotIn("Scripts", str(settings.legacy_python))
        self.assertNotEqual(settings.legacy_python.name.lower(), "python.exe")

    def test_default_python_is_current_runtime_not_a_windows_legacy_venv(self):
        settings = Settings(_env_file=None)
        self.assertEqual(settings.legacy_python, Path(sys.executable))

    def test_linux_adapter_overrides_are_written_only_to_job_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            legacy = root / "legacy"
            legacy.mkdir()
            frozen = legacy / "config.yaml"
            frozen.write_text(
                'input_file: "J:/old.ts"\noutput_root: "J:/output"\nwork_root: "J:/work"\n'
                'tesseract_cmd: "C:/Program Files/Tesseract-OCR/tesseract.exe"\n'
                'ffmpeg_hwaccel: "cuda"\nexamples_dir: "J:/examples"\n',
                encoding="utf-8",
            )
            settings = Settings(
                source_dir=Path("/recordings"), data_dir=root / "data", legacy_engine_root=legacy,
                legacy_python=Path("/usr/local/bin/python"), legacy_tesseract_cmd="/usr/bin/tesseract",
                legacy_ffmpeg_hwaccel="", legacy_examples_dir=Path("/legacy/examples/manual_profile_screenshots"),
            )
            job_config = LegacySubprocessEngine(settings)._job_config(Path("/recordings/one.ts"), root / "job")
            generated = job_config.read_text(encoding="utf-8")
            self.assertIn('input_file: "/recordings/one.ts"', generated)
            self.assertIn('tesseract_cmd: "/usr/bin/tesseract"', generated)
            self.assertIn('ffmpeg_hwaccel: ""', generated)
            self.assertIn('examples_dir: "/legacy/examples/manual_profile_screenshots"', generated)
            self.assertIn("C:/Program Files/Tesseract-OCR/tesseract.exe", frozen.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
