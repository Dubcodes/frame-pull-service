from __future__ import annotations

import json
import subprocess
from pathlib import Path


def run_checked(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True, capture_output=True)


def ffprobe_duration(ffprobe: str, source: Path) -> float:
    result = run_checked([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(source)])
    return float(json.loads(result.stdout)["format"]["duration"])


def clip_bounds(start: float, end: float, duration: float, padding: float) -> tuple[float, float]:
    return max(0.0, start - padding), min(duration, end + padding)


def create_clip(ffmpeg: str, source: Path, destination: Path, start: float, end: float) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg, "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(source), "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-c:a", "aac", "-shortest", str(destination)]
    run_checked(command)


def extract_frame(ffmpeg: str, source: Path, destination: Path, source_time: float) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    run_checked([ffmpeg, "-y", "-ss", f"{source_time:.3f}", "-i", str(source), "-frames:v", "1", "-q:v", "2", str(destination)])
