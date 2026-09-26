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

## Operational V2

The dashboard is race-day first. Closed recordings are grouped by parsed
broadcast date; expand a Race Day to inspect files, select visible or
unprocessed recordings, queue selected files, or cancel jobs that have not
started. Completed files remain complete and require an explicit reprocess
request rather than being silently requeued.

Operational settings are persisted in the service database at `/settings`.
Automatic Race Day mode, closed-file autoqueue, source deletion, and calendar
refresh are off by default. Processing pause is persistent: active jobs finish
but no worker claims another queued job until resumed. Controlled worker slots
support 1-4 independent jobs, each with its own legacy config and work folder.

People is the default review mode. It groups only manually resolved or trusted
identities within a Race Day; tentative OCR and unknowns remain individual
interviews. A group retains the independent interview evidence and may select
one preferred portrait. Candidate clicks select the image and seek the clip;
double-click opens the full-size candidate viewer.

Calendar and recorder integrations are provider boundaries. The included
calendar cache can hold meeting/race context but never determines identity.
The recorder adapter deliberately has no stream URL or credentials and stays
disabled until a real recorder is configured. The source lifecycle is disabled
by default and rechecks containment, stable/closed state, jobs, review/export,
retention, and evidence artifacts immediately before any delete.

## Bridge API

`/api/bridge/v1` is the future private service-to-service boundary for People
Intelligence. It exposes health, race days, groups, interviews and full-size
candidate/portrait media without Windows paths. Set `FRAME_PULL_BRIDGE_TOKEN`
outside source control to require `Authorization: Bearer <token>`. Use a LAN,
Tailscale, or private reverse tunnel as transport; do not share databases or
mount service storage between machines.
