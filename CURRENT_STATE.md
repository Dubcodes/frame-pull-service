# Current State

## V1 foundation
- Standalone Git repository and Python package created.
- SQLite/WAL database with Recording, Job, ProcessingRun, Interview,
  InterviewRevision, Candidate and ReviewEvent models.
- First discovery establishes a historical baseline without queueing existing
  recordings when the default environment setting is used.
- Persistent, single-worker job queue and stale-job recovery are implemented.
- `LegacySubprocessEngine` uses the frozen legacy working directory and Python,
  but writes isolated config/output/work/log files beneath service `data/jobs`.
- Normalization creates reviewable clip/poster/candidate/manifest packages.
- FastAPI review API and server-rendered dashboard/queue/detail UI are present.

## Commands
- `scripts/setup.ps1`
- `scripts/initialize_baseline.ps1`
- `scripts/run_service.ps1`
- `scripts/run_worker.ps1`
- `scripts/run_all.ps1`

## Safety
No code in this project writes to legacy `output`, `work`, `config.yaml`, or
recordings. Do not use the legacy all-recordings launcher through this service.

## Validation completed
- New service environment installed with FastAPI, SQLAlchemy, Jinja2, Pillow
  and Uvicorn.
- First-run baseline discovered 93 recordings as historical with zero jobs.
- Isolated unit suite: 9/9 passing.
- One and only one real smoke job ran: `trackside_20260816-1237_016.ts`.
  The frozen detector reported 122 structure hits, 116 authenticated hits and
  six interview segments. The service created six clips, six posters, six
  manifests, and 22 automatic candidates across revisions.
- Ryan Foote was used for the manual review proof: native source-frame capture,
  candidate selection, metadata save, approval, approved portrait, pending
  export query and idempotent export acknowledgement all passed.
- Browser routes `/`, `/review`, and `/review/1` returned 200 through the
  application test client; clip range access returned HTTP 206.
- Legacy config SHA-256, legacy empty output/work state, and source file size
  and modification time remained unchanged before/after the smoke.

## Known issues
- The service intentionally inherits legacy detector recall/precision and OCR
  limitations. Those remain detector-engine work, not service work.
- PyTorch CUDA DLL 1114 remains a legacy-environment issue. Candidate decode
  used the existing FFmpeg CUDA path during the smoke.
- The service has a dry-run-only cleanup plan; V1 never auto-deletes artifacts.

## Next task
Implement the People Intelligence API consumer against the approved/export
contract. Do not directly access its database or filesystem.
