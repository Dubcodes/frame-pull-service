from __future__ import annotations

from sqlalchemy import Engine, event, inspect, text
from sqlalchemy.engine import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import Settings


class Base(DeclarativeBase):
    pass


def create_db_engine(settings: Settings) -> Engine:
    connect_args = {"check_same_thread": False} if settings.resolved_database_url.startswith("sqlite") else {}
    engine = create_engine(settings.resolved_database_url, connect_args=connect_args)
    if settings.resolved_database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()
    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    from . import models  # noqa: F401

    Base.metadata.create_all(engine)
    # SQLite create_all intentionally does not alter existing tables. Keep the
    # V2 additions additive so real operator data survives an in-place upgrade.
    additions = {
        "recordings": {
            "race_day_id": "INTEGER", "recording_started_at": "DATETIME", "recording_stopped_at": "DATETIME",
            "duration_seconds": "FLOAT", "metadata_probed_at": "DATETIME", "is_closed": "BOOLEAN DEFAULT 0",
            "recorder_session_id": "INTEGER",
        },
        "interviews": {"appearance_group_id": "INTEGER", "context_confidence": "FLOAT"},
        "calendar_meetings": {"source_url": "TEXT", "last_attempted_at": "DATETIME", "provider_status": "VARCHAR(32) DEFAULT 'unknown'", "last_error": "TEXT"},
        "recording_sessions": {
            "planned_start_at": "DATETIME", "planned_end_at": "DATETIME", "manual": "BOOLEAN DEFAULT 1",
            "ffmpeg_pid": "INTEGER", "active_chunk_sequence": "INTEGER", "active_chunk_started_at": "DATETIME",
            "stop_after_chunk": "BOOLEAN DEFAULT 0", "error_summary": "TEXT", "log_path": "TEXT",
            "exit_code": "INTEGER", "recovery_checked_at": "DATETIME",
        },
    }
    inspector = inspect(engine)
    with engine.begin() as connection:
        for table, columns in additions.items():
            existing = {item["name"] for item in inspector.get_columns(table)}
            for name, definition in columns.items():
                if name not in existing:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))
