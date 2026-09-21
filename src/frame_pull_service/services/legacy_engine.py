from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..config import Settings


@dataclass
class LegacyResult:
    job_dir: Path
    output_dir: Path
    work_dir: Path
    recording_work_dir: Path
    summary: dict
    segments: dict


class LegacySubprocessEngine:
    """Read-only adapter around the frozen reference detector."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def _job_config(self, source: Path, job_dir: Path) -> Path:
        job_dir.mkdir(parents=True, exist_ok=True)
        legacy_config = self.settings.legacy_engine_root / "config.yaml"
        text = legacy_config.read_text(encoding="utf-8")
        output_dir = job_dir / "legacy_output"
        work_dir = job_dir / "legacy_work"
        replacements = {
            "input_file": source.as_posix(),
            "output_root": output_dir.as_posix(),
            "work_root": work_dir.as_posix(),
        }
        for key, value in replacements.items():
            pattern = rf"(?m)^(\s*{re.escape(key)}\s*:\s*).*$"
            if re.search(pattern, text):
                text = re.sub(pattern, rf'\1"{value}"', text)
            else:
                text += f'\n{key}: "{value}"\n'
        config_path = job_dir / "legacy_job_config.yaml"
        config_path.write_text(text, encoding="utf-8")
        return config_path

    def run(self, source: Path, job_dir: Path, on_output: Callable[[str], None] | None = None) -> LegacyResult:
        job_dir.mkdir(parents=True, exist_ok=True)
        config = self._job_config(source, job_dir)
        log_path = job_dir / "legacy_engine.log"
        command = [str(self.settings.legacy_python), "-u", "profile_extractor.py", "--config", str(config), "--input", str(source)]
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(command, cwd=self.settings.legacy_engine_root, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, bufsize=1)
            assert process.stdout is not None
            for line in process.stdout:
                log.write(line); log.flush()
                if on_output:
                    on_output(line.rstrip())
            code = process.wait()
        if code:
            raise RuntimeError(f"Legacy detector exited with {code}; see {log_path.name}")
        stem = source.stem
        recording_work = job_dir / "legacy_work" / stem
        summary_path, segments_path = recording_work / "run_summary.json", recording_work / "segments.json"
        if not summary_path.exists() or not segments_path.exists():
            raise RuntimeError("Legacy detector completed without required run summary/segments evidence")
        return LegacyResult(job_dir, job_dir / "legacy_output", job_dir / "legacy_work", recording_work,
                            json.loads(summary_path.read_text(encoding="utf-8")),
                            json.loads(segments_path.read_text(encoding="utf-8")))
