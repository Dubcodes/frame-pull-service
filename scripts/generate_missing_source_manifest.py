from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from frame_pull_service.services.relocation import build_missing_source_manifest


def main() -> int:
    cli = argparse.ArgumentParser(description="Generate an exact-identity manifest for audited unavailable sources.")
    cli.add_argument("--database", required=True, type=Path)
    cli.add_argument("--source-root", required=True, type=Path)
    cli.add_argument("--output", required=True, type=Path)
    args = cli.parse_args()
    database = args.database.resolve()
    source_root = args.source_root.resolve()
    output = args.output.resolve()
    if not database.is_file():
        raise SystemExit(f"database not found: {database}")
    if not source_root.is_dir():
        raise SystemExit(f"source root not found: {source_root}")
    if output.exists():
        raise SystemExit(f"output already exists: {output}")
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    try:
        with Session(engine) as session:
            document = build_missing_source_manifest(session, source_root)
    finally:
        engine.dispose()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "recording_count": document["recording_count"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
