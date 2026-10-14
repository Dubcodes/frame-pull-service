from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, event, inspect, text
from sqlalchemy.engine import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import Settings


class Base(DeclarativeBase):
    pass


# These are deliberately narrow, additive upgrades for tables that already
# exist. Missing tables are handled by Base.metadata.create_all at startup.
ADDITIVE_SCHEMA_COLUMNS = {
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
    # Existing queued jobs must remain inert after this migration. SQLite
    # applies the DEFAULT to every historical row when the column is added.
    "jobs": {"released_for_processing": "BOOLEAN NOT NULL DEFAULT 0"},
}


def create_db_engine(settings: Settings) -> Engine:
    connect_args = {"check_same_thread": False} if settings.resolved_database_url.startswith("sqlite") else {}
    engine = create_engine(settings.resolved_database_url, connect_args=connect_args)
    if settings.resolved_database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def pending_schema_additions(engine: Engine) -> dict[str, list[str]]:
    """Inspect known additive upgrades without changing schema or journal mode."""
    if engine.dialect.name == "sqlite":
        database = engine.url.database
        if database and database != ":memory:" and not Path(database).exists():
            return {}
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    pending: dict[str, list[str]] = {}
    for table, columns in ADDITIVE_SCHEMA_COLUMNS.items():
        if table not in existing_tables:
            continue
        existing_columns = {item["name"] for item in inspector.get_columns(table)}
        missing = [name for name in columns if name not in existing_columns]
        if missing:
            pending[table] = missing
    return pending


def init_db(engine: Engine) -> None:
    from . import models  # noqa: F401

    if engine.dialect.name == "sqlite":
        # WAL is a persisted database setting, so applying it belongs to the
        # explicit initialization boundary rather than ordinary connections.
        with engine.connect() as connection:
            connection.execute(text("PRAGMA journal_mode=WAL"))
            connection.commit()
    Base.metadata.create_all(engine)
    # SQLite create_all intentionally does not alter existing tables. Keep the
    # V2 additions additive so real operator data survives an in-place upgrade.
    pending = pending_schema_additions(engine)
    with engine.begin() as connection:
        for table, names in pending.items():
            for name in names:
                definition = ADDITIVE_SCHEMA_COLUMNS[table][name]
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))
