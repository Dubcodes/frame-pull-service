from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from frame_pull_service.services.relocation import (
    RelocationRoots, apply_runtime_relocation, plan_runtime_relocation, semantic_snapshot,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Relocate approved Frame Pull runtime roots without changing identity or queue state.")
    result.add_argument("--database", required=True, type=Path)
    result.add_argument("--old-source-root", required=True)
    result.add_argument("--new-source-root", required=True)
    result.add_argument("--old-data-root", required=True)
    result.add_argument("--new-data-root", required=True)
    mode = result.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    result.add_argument("--skip-file-check", action="store_true", help="Inventory paths without checking the target filesystem.")
    return result


def main() -> int:
    args = parser().parse_args()
    database = args.database.resolve()
    if not database.is_file():
        raise SystemExit(f"database not found: {database}")
    roots = RelocationRoots(args.old_source_root, args.new_source_root, args.old_data_root, args.new_data_root)
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    try:
        with Session(engine) as session:
            before = semantic_snapshot(session)
            plan = plan_runtime_relocation(session, roots, inspect_files=not args.skip_file_check)
            report = {"mode": "apply" if args.apply else "dry-run", "database": str(database), **plan.report(),
                      "semantic_before": _semantic_report(before)}
            if args.dry_run:
                session.rollback()
                report["database_changed"] = False
                print(json.dumps(report, indent=2, default=str))
                return 0
            try:
                apply_runtime_relocation(session, plan, roots)
                after = semantic_snapshot(session)
                if after != before:
                    raise ValueError("relocation changed queue/editorial semantics")
                session.commit()
            except Exception:
                session.rollback()
                raise
        with Session(engine) as verification:
            second = plan_runtime_relocation(verification, roots, inspect_files=not args.skip_file_check)
            if second.changes or second.unresolved:
                raise ValueError("post-apply relocation is not idempotent")
            report["semantic_after"] = _semantic_report(semantic_snapshot(verification))
            report["idempotent"] = True
            report["database_changed"] = bool(plan.changes)
        print(json.dumps(report, indent=2, default=str))
        return 0
    finally:
        engine.dispose()


def _semantic_report(snapshot: dict) -> dict:
    return {
        "recordings": snapshot["recordings"],
        "jobs": len(snapshot["jobs"]),
        "held": sum(status == "queued" and not released for _id, _recording, status, released in snapshot["jobs"]),
        "released": sum(status == "queued" and released for _id, _recording, status, released in snapshot["jobs"]),
        "active": sum(status == "processing" for _id, _recording, status, _released in snapshot["jobs"]),
        "reviews": len(snapshot["reviews"]),
        "queue_sha256": snapshot["queue_sha256"],
    }


if __name__ == "__main__":
    raise SystemExit(main())
