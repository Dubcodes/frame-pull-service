from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="FRAME_PULL_")

    source_dir: Path = Path(r"J:\frame-pull\trackside_recordings")
    legacy_engine_root: Path = Path(r"J:\frame-pull")
    data_dir: Path = Path(r"J:\projects\frame-pull-service\data")
    host: str = "127.0.0.1"
    port: int = 8094
    discovery_interval_seconds: int = 30
    stable_for_seconds: int = 120
    min_input_bytes: int = 10 * 1024 * 1024
    auto_queue: bool = False
    queue_existing_on_first_run: bool = False
    max_concurrent_jobs: int = 1
    clip_padding_seconds: float = 5.0
    broadcast_timezone: str = "Pacific/Auckland"
    bridge_token: str = ""
    database_url: str | None = None

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'frame_pull.db').as_posix()}"

    @property
    def legacy_python(self) -> Path:
        return self.legacy_engine_root / ".venv" / "Scripts" / "python.exe"

    def ensure_data_dirs(self) -> None:
        for path in (self.data_dir, self.data_dir / "jobs", self.data_dir / "interviews", self.data_dir / "logs", self.data_dir / "backups"):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
