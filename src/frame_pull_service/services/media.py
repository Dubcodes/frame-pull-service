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


def contained_clip_bounds(segment: dict, duration: float, padding: float) -> tuple[float, float, str]:
    """Build a review clip without leaking outside authenticated shot evidence."""
    start = float(segment.get("start", segment["detected_start"]))
    end = float(segment.get("end", segment["detected_end"]))
    detected_start = float(segment["detected_start"])
    detected_end = float(segment["detected_end"])
    window_start = float(segment.get("candidate_window_start", start))
    window_end = float(segment.get("candidate_window_end", end))
    if segment.get("shot_boundaries") and window_end > window_start:
        # The expanded candidate window is useful for portrait scoring, but can
        # predate the on-screen interview. A review clip should start/end where
        # the authenticated strap itself was observed, while remaining inside
        # the legacy continuity window.
        clip_start = max(window_start, detected_start)
        clip_end = min(window_end, detected_end)
        if clip_end > clip_start:
            return max(0.0, clip_start), min(duration, clip_end), "authenticated_interval_containment"
        return max(0.0, window_start), min(duration, window_end), "candidate_window_containment"
    clip_start, clip_end = clip_bounds(start, end, duration, padding)
    return clip_start, clip_end, "segment_padding"


def create_clip(ffmpeg: str, source: Path, destination: Path, start: float, end: float) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg, "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(source), "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-c:a", "aac", "-shortest", str(destination)]
    run_checked(command)


def extract_frame(ffmpeg: str, source: Path, destination: Path, source_time: float) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    run_checked([ffmpeg, "-y", "-ss", f"{source_time:.3f}", "-i", str(source), "-frames:v", "1", "-q:v", "2", str(destination)])
