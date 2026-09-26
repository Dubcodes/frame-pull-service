# Frame Pull Service

Review-first local service for Trackside interview evidence packages. The frozen
legacy engine at `J:\frame-pull` remains a read-only detector dependency; this
service owns its own database, job work, clips, portraits, logs, and review API.

## Install and run

```powershell
cd J:\projects\frame-pull-service
.\scripts\setup.ps1
.\scripts\initialize_baseline.ps1
.\scripts\run_all.ps1
```

Open `http://127.0.0.1:8094`. Use `run_service.ps1` and `run_worker.ps1` in
separate terminals when preferred.

## First-run safety

The initial discovery registers existing recordings as `historical` and does
not queue them. `FRAME_PULL_QUEUE_EXISTING_ON_FIRST_RUN=false` is the default.
Historical recordings can only be processed through an explicit queue action.
New `.ts` files are polled, must exceed the configured minimum size, remain
unchanged for the stability window, and are then eligible for automatic queueing.

## Data and safety

`data/` is generated runtime state and is gitignored. It contains the SQLite
WAL database, service-owned job work, evidence packages, clips and approved
portraits. Source recordings are never copied, changed or deleted. No API
accepts arbitrary filesystem paths.

Every authenticated interview creates an evidence package with a manifest,
seekable H.264 clip, poster, and preserved candidate portraits. OCR trust and
tentative OCR remain distinct. Approval requires a selected portrait and a
final name; rejected false positives remain auditable.

When the frozen engine supplies shot-containment evidence, the review clip is
bounded to the authenticated lower-third interval. This prevents the viewer or
manual capture from drifting into unrelated footage before or after the
interview; wider candidate evidence remains preserved separately.

## API

`/api/health`, recording discovery/queue routes, interview review routes,
media endpoints, approval/rejection, and export acknowledgement are available
under `/api`. `GET /api/interviews?status=approved&export_state=pending` is the
future People Intelligence handoff boundary. No People Intelligence client is
included here.

## Cleanup

V1 does not automatically delete service artifacts. The legacy source corpus,
its fixtures, and its configuration remain untouched.
