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
media endpoints, approval/rejection, export acknowledgement, and race-day
calendar routes are available under `/api`. People Intelligence uses the
separate authenticated `/api/bridge/v1` boundary; neither service shares a
database or storage directory.

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

The LoveRacing calendar provider discovers official meeting overview pages and
normalizes only supported meeting/race timing context into the local cache.
Calendar context can resolve a likely track/race with an explicit confidence
label; it never establishes identity. Refresh is an explicit operator action
until automatic mode is enabled. The recorder adapter deliberately has no
stream URL or credentials and stays disabled until a real recorder is
configured. Race-day planning derives a window from cached race times and
configured lead/tail minutes; it reports an incomplete schedule rather than
guessing. The source lifecycle is disabled by default and rechecks containment,
stable/closed state, jobs, review/export, retention, and evidence artifacts
immediately before any delete.

## Race-day intelligence (V2.3)

Broadcast time is explicit: `Pacific/Auckland` is the configurable default.
Trackside filenames supply a timezone-aware recording start when they match the
known naming convention, and an interview's source timestamp is added to that
start in the service layer. FFprobe duration is cached only when a recording is
already being normalized, allowing the stored recording end to be shown without
probing media during dashboard reads.

The conservative `InterviewContextResolver` associates an interview with
cached calendar evidence using configured pre-race and post-race windows. It
returns `high`, `medium`, `low`, `conflict`, or `unknown`, retains nearby races,
and stores refreshable evidence in `InterviewContext`. Calendar runners,
scratches, and explicitly labelled jockey/trainer fields remain contextual
evidence only: they never establish an interview subject's identity. Human
track values are not overwritten.

Race Day cards show actual cached meetings, race counts, first/last times,
recording ranges, and a lead/tail recording plan. The detail view leads with
human-readable interview context and keeps technical provenance available
below it. Grouped people display one resolved track only when member evidence
agrees; otherwise the UI reports a location conflict. Unknown interviews remain
separate and retain their own context.

The recorder remains unconfigured. `RecorderAdapter` is only a future contract
for status, start, stop, current output, and health. A recorder's active `.ts`
path is excluded from discovery entirely until it closes; a closed chunk still
needs its ordinary stability check before it becomes ready. Automatic race-day
mode, autoqueue, processing, and source deletion all remain off by default.

## Dedicated recorder (V2.4)

`FFmpegRecorder` is the concrete recorder implementation behind the adapter.
It starts one long-running FFmpeg segment process with an argument array (never
`shell=True`), stream-copies where the configured input permits it, and writes
normal source chunks using the compatible `trackside_YYYYMMDD-HHMM_SS.ts`
format. The date/time in every filename belongs to that chunk's start, rather
than the start of the overall session. Production defaults to 60-minute chunks.

The actual Trackside input is **not configured in source control**. A future
operator supplies `FRAME_PULL_RECORDER_INPUT` through a local secret
environment. Status responses and logs retain only a redacted configuration
indicator, not a URL, token, password, or query string. Recording additionally
requires the persisted recorder-enabled setting, a manual start or enabled
automatic plan, a usable FFmpeg executable, output storage, and sufficient free
space. The output path belongs to configuration; browser requests cannot choose
an arbitrary path or executable.

Every recorder session persists its race-day/manual origin, planned end, PID,
active chunk, stop request, log path, exit code, and error state. On an API
restart the service never kills a remembered PID: an unproven live process is
marked `uncertain`, while a missing PID is marked failed. FFmpeg crashes are
recorded and do not auto-retry indefinitely.

The current chunk is excluded from normal discovery while recording,
finalizing, or gracefully stopping. After a roll or final stop, the closed
chunk re-enters the ordinary source path: existence/readability/stability
checks, then `READY`, then optional `QUEUED`. `autoqueue_closed_recordings`
only controls queue insertion; `processing_paused` still blocks worker claims.
Therefore recording can safely continue while processing is paused.

The dashboard exposes a concise redacted recorder state and manual start,
stop-after-current-chunk, and stop controls. The Race Day scheduler remains off
by default. When enabled with a configured recorder and a valid calendar plan,
it starts once at the plan window and requests a graceful stop at the planned
end. No real recording starts merely by opening the service.

## Bridge API

`/api/bridge/v1` is the future private service-to-service boundary for People
Intelligence. It exposes health, race days, groups, interviews and full-size
candidate/portrait media without Windows paths. Set `FRAME_PULL_BRIDGE_TOKEN`
outside source control to require `Authorization: Bearer <token>`. Use a LAN,
Tailscale, or private reverse tunnel as transport; do not share databases or
mount service storage between machines.
