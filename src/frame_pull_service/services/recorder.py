from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class RecorderState:
    active_path: Path | None = None
    running: bool = False
    health: str = "unconfigured"


class RecorderAdapter:
    """Boundary for a future recorder. V2 never invents stream URLs or credentials."""
    name = "external"

    def status(self) -> RecorderState:
        return RecorderState(health="unconfigured")

    def start(self, race_day=None) -> RecorderState:
        raise RuntimeError("No recorder input is configured")

    def stop(self) -> RecorderState:
        return RecorderState(health="unconfigured")

    def current_output(self) -> Path | None:
        return self.status().active_path

    def health(self) -> str:
        return self.status().health
