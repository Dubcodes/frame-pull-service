from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class RecorderState:
    active_path: Path | None = None
    running: bool = False


class RecorderAdapter:
    """Boundary for a future recorder. V2 never invents stream URLs or credentials."""
    name = "external"

    def status(self) -> RecorderState:
        return RecorderState()

    def start(self) -> RecorderState:
        raise RuntimeError("No recorder input is configured")

    def stop(self) -> RecorderState:
        return RecorderState()
