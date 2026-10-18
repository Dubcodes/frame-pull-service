# Current State

## V2.10B local backlog canary checkpoint (stopped)

- The read-only production inventory found 74 physical source files: 62
  source-present held jobs, 11 source-present READY recordings with zero job
  history, and one source-present recording with a completed job plus a later
  held duplicate job. The 11 zero-history recordings were queued through the
  normal service as held jobs 95-105. The exact initial processable set was 73
  jobs; no generic release operation was used.
- A consistent pre-run SQLite/artifact export was created under the ignored
  runtime backup area before queue changes. The 18 historical held jobs whose
  sources are unavailable remained queued, unreleased, and unattempted; their
  deterministic state digest remained
  `aaa550d2baabb9ebaf06e3fde2ca6fb17afdc4e8750df0a655994afb2bde4247`.
- Job 94 / recording 1 / `trackside_20260816-1137_016.ts` was selected as the
  only canary release. It is a 3,600-second, 1,931,266,848-byte recording. The
  worker claimed only this job, but it failed in about 0.22 seconds before
  detector startup because `FRAME_PULL_LEGACY_PYTHON` had been set to the
  service virtual environment, which does not contain `cv2`. The existing
  frozen-detector environment at `J:\frame-pull\.venv\Scripts\python.exe`
  passes the required Python import check. The canary was not retried and the
  remaining backlog was not released.
- Processing was immediately paused and the API and worker were stopped with
  zero active jobs. Current queue state is 91 queued held, 0 released queued,
  0 active, 12 complete, and 2 failed. Of the 91 held jobs, 72 are the remaining
  source-present processable set, 18 are unavailable-source history, and job 93
  is the excluded held duplicate for an already-complete recording.
- Source deletion remained disabled. All original 74 `.ts` files retained
  identical filename/size/mtime metadata; the aggregate manifest digest stayed
  `783badcb93687811b3511108f26a8ae6acaeb02ced377173bbe1b32c1dff2b84`,
  and all five representative SHA-256 values remained unchanged. The canary
  produced zero interviews and zero candidates.
- Decision: **CANARY FAILED — BACKLOG NOT STARTED**. A future explicitly
  authorized retry must use the verified frozen-detector Python environment;
  no retry was attempted in this checkpoint.

## V2.10 runtime portability checkpoint (target blocked)

- Runtime source access now uses one containment-checked resolver based on the
  configured source root and recording filename. Worker invocation,
  normalization, manual frame capture, and source lifecycle checks no longer
  trust the absolute host path stored by an earlier deployment. Discovery may
  refresh a stored path only when fingerprint evidence, or the existing
  size-and-mtime identity, matches; conflicts remain untouched and reported.
- `scripts/relocate_runtime_state.py` performs an explicit-root, cross-platform
  dry-run or transactional apply over every known path-bearing column and the
  narrowly scoped JSON evidence fields. It rejects external paths, validates
  target recording identity, preserves queue/review relationships and state,
  and verifies post-apply idempotence. The production dry-run proposed 239
  bounded changes (104 source and 135 artifact paths), found no external paths,
  and made no database changes.
- A consistent SQLite-backup export was created under the ignored runtime
  backup area with 239 files totalling 130,694,617 bytes. All recorded SHA-256
  checksums verified, and all 135 referenced artifact paths exist both in the
  Windows runtime and export. The snapshot remains 104 recordings and 94 jobs:
  81 queued held, 0 released, 0 active, 12 complete, and 1 failed. The queued
  relationship digest remains
  `c7cd9cf57497aa02a4e42f5eb8d9b3dad94c6d2fe9d66c44b314715dccdfbd32`.
- The source corpus currently contains 74 `.ts` files. Thirty database source
  rows are already missing on Windows: 18 held queued, 11 complete, and 1
  failed. The known-positive
  `trackside_20260816-1237_016.ts` remains present with SHA-256
  `83FDD0A8ABC164029C0C29EB5F6C9AE1C1B6E53301B4ABEEB6EC265FD8630C5D`.
  Relocation apply deliberately blocks while recording targets are missing;
  missing history is reported rather than fabricated.
- The target Linux hostname/SSH destination and corpus recovery source have not
  been provided. No target precheck, transfer, restore, container build, schema
  migration, web/worker start, recorder probe, or isolated Linux detector run
  was attempted. Production remains paused with every queued job held;
  autoqueue, automatic Race Day mode, source deletion, and automatic recording
  remain off. No production job was released, claimed, or processed.
- Current decision: **STATE RELOCATION UNSAFE / INCOMPLETE** until the 30
  missing source files are recovered or their disposition is explicitly
  resolved and the actual Linux target completes qualification.
- **REAL i5 PERFORMANCE BENCHMARK NOT YET RUN.**

## V2.9 Linux/Docker production foundation

- Production now targets one Linux image used by `frame-pull-web` and
  `frame-pull-worker`. Compose gives both local `/data`, mounts `/legacy`
  read-only, mounts `/recordings` read/write for web/recorder and read-only for
  worker, and starts worker only after the web healthcheck succeeds.
- Application defaults and environment configuration are POSIX-safe. The
  legacy Python executable is explicit rather than derived from
  `.venv\\Scripts\\python.exe`. Windows development remains supported through
  local `.env` values and the existing PowerShell convenience scripts.
- The frozen detector was audited read-only at `J:\frame-pull`. Its requirements
  are OpenCV, NumPy, PyYAML, pytesseract, Pillow, FFmpeg/FFprobe, Tesseract, and
  ordinary font/runtime libraries. It does not import Torch. Its current config
  contains a Windows Tesseract executable, Windows examples path, and CUDA
  decode request; Docker replaces these only in each service-owned job config.
- SQLite remains in WAL mode with foreign keys and a five-second busy timeout.
  The supported topology is web plus one worker on the same Docker host and
  reliable local `/data`. Live SQLite on SMB/NFS/network storage is explicitly
  unsupported. Startup migration remains outside import/app construction and
  is performed explicitly by each service; Compose serializes first startup by
  waiting for healthy web before worker.
- Recorder sessions now persist a runtime owner ID alongside FFmpeg PID. A new
  runtime marks any prior-owner active session interrupted without probing or
  signalling its numeric PID, preserves all chunks, and never auto-restarts.
  Web SIGTERM requests a graceful `q` only from its own child and retains a
  final partial chunk if grace expires. Worker SIGTERM stops claims first and
  waits for active job threads within the external container stop grace.
- Docker was not installed on the V2.9 development machine. No image build,
  container smoke, Linux detector run, or target-volume permission test was
  claimed. These are mandatory V2.10 target-server checks before one zero-job
  production recording may be queued held, explicitly released, and processed.
- No production recording was queued, released, claimed, or processed in V2.9.
  The last observed reference remains 104 recordings and 94 jobs: 81 queued
  all held, 0 released, 0 active, 12 complete, and 1 failed, with processing
  paused and autoqueue, automatic Race Day mode, and source deletion off.
  A read-only plan reports only `recording_sessions.recorder_owner_id` pending;
  the Windows production DB was deliberately not migrated during this audit.

## V2.8.1 database startup safety checkpoint

- `frame_pull_service.main` import and `create_app()` construction no longer
  create runtime directories, connect to/migrate the configured schema, or
  change SQLite journal mode. Actual FastAPI lifespan startup explicitly runs
  directory creation, WAL/schema initialization, settings seeding, recorder
  recovery, and discovery before starting a watcher or optional worker.
- A read-only `pending_schema_additions()` inspection reports the controlled
  missing additive columns without modifying schema. Fresh-import,
  app-construction, legacy-schema lifespan migration, plan-before/after, and
  row-preservation tests use temporary SQLite databases. Non-web initialization
  utilities now call `init_db()` explicitly.
- The production additive plan is empty. A fresh import-only subprocess left
  the complete production semantic snapshot unchanged, including queued job
  relationship digest
  `c7cd9cf57497aa02a4e42f5eb8d9b3dad94c6d2fe9d66c44b314715dccdfbd32`.
  Normal no-worker API startup returned healthy with 81 held, 0 released, and
  0 active. Its normal discovery pass registered 11 new stable source files as
  `READY` (recordings 93 -> 104); autoqueue was off, so it created no jobs and
  claimed or processed nothing.
- V2.8's historical audit limitation is unchanged and not reconstructed: the
  exact production pre-migration digest was not captured. Available evidence
  remains additive `DEFAULT 0`, isolated legacy-row preservation, the expected
  unchanged 81 queued relationships, and no worker/detector execution.
- Processing remains paused; autoqueue, automatic Race Day mode, and source
  deletion remain off. **REAL i5 PERFORMANCE BENCHMARK NOT YET RUN.**

## V2.8 backlog hold and selected release checkpoint

- Queue insertion and processing eligibility are now separate durable states.
  All newly queued manual, bulk, reprocess, retry, autoqueue, and closed-chunk
  jobs default held. A queued job must have
  `released_for_processing=true` **and** global processing must be resumed
  before the authoritative atomic claim path can select it.
- Release and hold are idempotent service operations with single-job and
  bounded explicit-ID bulk APIs. Only queued jobs may change release state;
  there is no unguarded release-all endpoint. The dashboard and Race Day rows
  distinguish Held, Released, Processing, Complete, and Failed, and “Resume
  released jobs” states exactly how many jobs can become eligible.
- The additive SQLite migration uses `BOOLEAN NOT NULL DEFAULT 0`. Production
  remains at 93 recordings and 94 jobs (81 queued, 12 complete, 1 failed), with
  all 81 queued jobs held, 0 released, and 0 active. Processing is paused;
  autoqueue, automatic Race Day mode, and source deletion remain off. No
  production job was claimed or processed.
- Isolated qualification covers old-schema migration, the pause/release claim
  matrix, hold-after-release, deterministic and simultaneous claims, explicit
  bulk IDs, invalid statuses, API counts and wording, autoqueue, reprocess, and
  retry-held defaults. **REAL i5 PERFORMANCE BENCHMARK NOT YET RUN.**

## V2.7 known-positive evidence checkpoint

- A preserved copy of the known-positive historical source
  `trackside_20260816-1237_016.ts` was processed through the existing V2.6
  isolated one-job tool. The original remained unchanged at 1,939,177,700
  bytes, mtime `2026-08-16T01:37:40.4270820Z`, and SHA-256
  `83FDD0A8ABC164029C0C29EB5F6C9AE1C1B6E53301B4ABEEB6EC265FD8630C5D`.
- The frozen detector exited 0 after 479.312 seconds and reproduced the
  historical positive result: six authenticated/normalized interviews, six
  evidence packages, and 22 candidates. All six manifests were internally
  consistent and path-contained; all clips were playable H.264/AAC with
  durations matching stored bounds; all posters and candidates decoded.
- Visual inspection of three positions from every clip showed the represented
  interview/lower-third event rather than unrelated race footage. Ryan Foote's
  isolated clip remained 329.25-334.00 seconds, and its selected candidate was
  interview evidence rather than the previously corrected race-frame drift.
  One package legitimately contained no candidate portrait.
- Review remained pending throughout. Four names met the existing trusted-OCR
  rule; the two single-timestamp OCR candidates remained tentative and did not
  populate final identity. Calendar context stayed unknown because the
  isolated database had no cached races; it did not establish identity.
- The isolated dashboard, People and Interviews modes, all six detail pages,
  clip playback, posters, candidates, and technical evidence rendered. HTTP
  Range returned 206 and API JSON exposed no local paths. No approval, reject,
  export, or review edit was performed.
- The legacy config/output/work snapshot and production state were unchanged;
  all 81 historical queued jobs remain paused. **REAL i5 PERFORMANCE BENCHMARK
  NOT YET RUN.**

## V2.6 controlled one-chunk processing checkpoint

- Exactly one preserved V2.5F recorder chunk,
  `trackside_20261004-0931_39.ts` (60.000 seconds, 32,049,676 bytes), was
  copied into a new ignored qualification root and processed through normal
  discovery, explicit queueing, isolated pause/claim handling, the service
  worker job path, the frozen legacy adapter, and normalization.
- The adapter invoked one explicit `--input` and no `--all` or `--rerun`, with
  its config, output, work, and log paths owned by the isolated service job.
  The detector exited 0 after 9.172 seconds; the job and recording completed.
- This sample produced zero authenticated interviews and zero candidates, so
  there were no evidence packages or clips to validate. The isolated dashboard
  represented the completed recording/job and the review queue remained empty,
  without bogus review cards. Calendar context was therefore not applicable.
- The frozen legacy config checksum and ordinary legacy `output`/`work`
  metadata were unchanged. Production remained at 93 recordings and 94 jobs
  (81 queued, 12 complete, 1 failed), with 12 interviews and 4 appearance
  groups; processing stayed paused and autoqueue, automatic mode, and source
  deletion stayed off. Both the original and isolated source copies remain.
- **ONE-CHUNK PIPELINE PASSED, POSITIVE INTERVIEW EVIDENCE STILL REQUIRES A
  CONTENT-POSITIVE SAMPLE.**
- **REAL i5 PERFORMANCE BENCHMARK NOT YET RUN.**

## V2.5A Trackside HLS profile checkpoint

- The recorder supports a local-only `trackside_hls` profile with structured
  pre-input resilience options, CRLF Origin/Referer headers, and mandatory
  configured program video/audio mapping. The endpoint and headers remain
  absent from source control, logs, and public status.
- The initial real-input shadow qualification remains controlled: processing,
  autoqueue, automatic race-day mode, and source deletion remain off. No
  shadow chunk is a production recording or job.
- On 2026-10-02 the direct HLS probe confirmed the configured program's H.264
  1280x720/25 video and AAC stereo/48 kHz audio. The isolated recorder also
  proved active-chunk discovery exclusion, but the source ended cleanly after
  9.36 seconds (5,120,932 bytes) before its first 60-second rollover. The service does
  not reproduce the Pi's unbounded shell restart loop, so real shadow
  qualification is deliberately stopped pending an explicit bounded recovery
  design and another controlled test.
- V2.5B adds bounded FFmpeg-level EOF recovery (`reconnect_at_eof`, a maximum
  of three retries, and a 30-second total reconnect-delay limit) while keeping
  one recorder process and no-clobber output. A subsequent isolated live probe
  found program entries but no usable stream records, so it was stopped before
  recording. Real continuity and rollover remain unqualified; no automatic
  application restart loop has been introduced.
- On 2026-10-04 the V2.5C single bounded live probe again found that configured
  program 1 did not expose both a usable video stream and a usable audio stream.
  The availability gate therefore stopped live work before capture. No mapping
  change, repeated probe, recorder process, worker, or detector run followed.
  Real continuity, rollover, stop behavior, and isolated queue handoff remain
  unqualified until a healthy program-1 probe succeeds.
- The V2.5D audit corrected two recorder-state semantics without changing the
  filename format: `active_chunk_sequence` is now a true per-session monotonic
  counter instead of the filename's clock-second suffix, and terminal recorder
  status remains operator-visible after `source_ended` while a deliberate new
  start remains allowed. At 2026-10-04 08:58 NZDT, the one permitted bounded
  probe still found program 1 missing at least one usable video/audio stream,
  so live shadow work stopped at the availability gate.
- V2.5F replaced only the ignored local input configuration with the endpoint
  advertised by the current Trackside website; the endpoint itself remains out
  of Git. Its live structure contains shared AAC stereo/48 kHz audio plus H.264
  25 fps video variants at 640x360 (program 0) and 1280x720 (program 1), so the
  existing explicit program-1 mapping remains correct. A direct stream-copy
  test captured 105.000 seconds (56,169,700 bytes) without the old 9.36-second
  process exit. The isolated recorder then produced two complete 60.000-second
  1280x720 H.264/AAC chunks on one PID, protected the active chunk, completed
  READY -> QUEUED handoff with paused claim blocked, and closed cleanly for both
  stop-after-current and normal graceful stop. The stop operations preserved
  valid 2.2-second and 1.0-second trailing chunks for review rather than deleting
  them. The older OnDemand endpoint behavior was not representative of the
  website's current live player.
- **REAL i5 PERFORMANCE BENCHMARK NOT YET RUN.**

## V2.4 dedicated recorder checkpoint

- `FFmpegRecorder` is implemented as a configurable, secret-safe rolling
  MPEG-TS recorder. It uses one FFmpeg segment process, parser-compatible
  timestamped chunks, durable `RecordingSession` state, disk-space gating,
  redacted logs/status, bounded stop behavior, and conservative PID recovery.
- Current/finishing chunks are excluded from normal discovery. Only closed,
  stable chunks become ordinary `Recording` rows and may be autoqueued. A
  paused worker continues to block claims independently from recorder activity.
- Synthetic local FFmpeg qualification stream-copied a temporary video/audio
  input into three-second test segments, proved active-chunk exclusion, rolled
  chunk readiness, final-stop handling, and FFprobe-readable transport stream
  output. No `J:\frame-pull\trackside_recordings` input was used.
- Manual/API and automatic Race Day orchestration controls are present, but
  remain inactive by default. The actual Trackside stream/input has not been
  configured, automatic race-day mode is off, autoqueue is off, processing is
  paused, and source deletion is off.
- **REAL TRACKSIDE INPUT NOT YET CONFIGURED.**
- **REAL i5 PERFORMANCE BENCHMARK NOT YET RUN.**

## V2.3 race-day intelligence checkpoint

- Broadcast wall-clock handling is explicit and timezone-aware with
  `Pacific/Auckland` as the configured default. Filename starts,
  FFprobe-derived recording ends, and interview source offsets are handled in
  the service layer rather than as UI arithmetic.
- `InterviewContext` persists refreshable resolver evidence: the applicable
  meeting/race, nearby races, runners, confidence label, resolver version, and
  local interview time. Calendar context never becomes identity proof and it
  never overwrites a human track correction.
- The Race Day and review UI expose calendar status, recording plans, readable
  interview/race context, unknown-person context, and grouped location
  conflicts while retaining technical evidence separately.
- Active recorder outputs are excluded from discovery before they can become
  ready or queued. Closed chunks still require normal stability evidence.
- A bounded `J:\projects` source/docs/manifests search found no reusable
  Trackside recording implementation. The adapter remains unconfigured; no
  stream URL, credentials, recorder, processing, or source deletion was added.
- V2.3 isolated verification has 79 passing Frame Pull tests. Native
  PostgreSQL staging is blocked: Docker is unavailable by requirement, no safe
  non-production native/remote PostgreSQL endpoint is configured, and the
  People production database was not used.

## V2.2 source/control checkpoint

- Public source repository: `https://github.com/Dubcodes/frame-pull-service`
  on `master`. The public source contains no runtime database, recordings,
  artifacts, bridge token, or People Intelligence deployment configuration.
- LoveRacing is now an independent, normalized calendar provider. It caches
  official meeting/race timing and runner context, preserves cached data on a
  provider outage, and labels any interview context as `high`, `medium`,
  `low`, `conflict`, or `unknown` rather than inferring identity.
- Race-day automation has a safety-first orchestration foundation: automatic
  mode remains off, no recording starts without a configured recorder, active
  `.ts` chunks never qualify for queueing, and planning reports an incomplete
  schedule rather than inventing a window.
- A real authenticated People-to-Frame-Pull staging qualification is required
  before deployment. It must use disposable PostgreSQL rather than the local
  production People database.

## Operational V2 checkpoint

## V2.1 bridge checkpoint

- Historical V2.1 checkpoint: source repository visibility later changed to
  public after a runtime/secret audit. `FRAME_PULL_BRIDGE_TOKEN` remains optional for localhost
  development and mandatory when configured.
- Group exports now acknowledge each approved member interview and refresh its
  manifest, rather than only changing a group-level flag. Repeated acknowledgements
  are idempotent. Approved, ungrouped interviews have the matching bridge route.
- Bridge records expose stable IDs, review/export state, approved portrait
  availability, role and track context, and interview evidence summaries. They
  never expose source or artifact filesystem paths.
- At this historical V2.1 checkpoint calendar and recorder were provider
  boundaries only. V2.2 later added the independent LoveRacing calendar
  provider; the recorder remains unconfigured.
- People Intelligence now has a server-to-server Frame Pull Inbox client. It
  imports only after explicit existing-person selection, records provenance,
  commits locally, then acknowledges this bridge idempotently.

- V1 qualification commit: `14c2de1 Qualify Frame Pull Service V1`.
- Before V2 migration, a SQLite-native backup was created at
  `data/backups/frame_pull-20260927-092024.sqlite`.
- V2 added additive SQLite schema support for Race Days, persistent operation
  settings, recording sessions, calendar context, appearance groups, and
  source-deletion audit records. Existing runtime data was preserved.
- Current real state after migration: 93 recordings, 94 jobs (81 queued, 12
  complete, 1 failed), 12 interviews (10 pending, 1 approved, 1 rejected),
  and 54 candidates. Thirteen Race Days were derived from recording filenames.
- The service is deliberately persisted as `processing_paused=true`. No worker
  is running; opening the API cannot start the 81-job queue.
- Defaults remain safety-first: autoqueue off, automatic race-day mode off,
  source deletion off, review-before-delete on, export-before-delete on, and
  one concurrent recording slot.
- `/api/bridge/v1` is a versioned private API boundary. A configured
  `FRAME_PULL_BRIDGE_TOKEN` requires bearer authentication. No People
  Intelligence code, database, or filesystem was modified.
- The recorder and calendar implementations are safe provider boundaries;
  neither has been configured with a stream, credentials, or live calendar
  network call.
- Expanded isolated test suite: 64 passing tests before final V2 validation.
- Frozen legacy output/work currently contain four pre-existing artifacts dated
  24 September from an earlier direct detector run. V2 did not create, modify,
  or remove them.

## V1 qualification

- Standalone Frame Pull Service V1 with SQLite/WAL persistence, a first-run
  historical baseline, a single-worker persistent queue, a periodic discovery
  watcher, and a review-first FastAPI UI/API.
- The frozen detector at `J:\frame-pull` is invoked only through service-owned
  job config, output, work, and logs. It is never run with `--all` by this
  service and its config, output, work, and source recordings are not written.
- Existing recordings are registered as historical and do not autoqueue. Only
  stable recordings discovered after the baseline can autoqueue when enabled;
  historical files require an explicit manual queue action.
- Review packages contain a manifest, H.264 clip, poster, and candidates.
  When legacy shot-containment evidence is present, clips use the authenticated
  lower-third interval rather than broad pre/post-roll candidate windows.
- `tests/test_service.py` and `tests/test_qualification.py` contain 57
  isolated tests using temporary SQLite databases and synthetic FFmpeg media.

## Verified evidence

- Initial baseline registered 93 recordings as historical with zero jobs.
- The only real smoke run was `trackside_20260816-1237_016.ts`: 122 structural
  hits, 116 authenticated hits, six normalized interview records and six
  evidence packages.
- All six active packages have a valid clip, poster, manifest, and expected
  candidate count. The no-clean-shot interview remains reviewable with no
  candidate portrait.
- Ryan Foote remains approved and exported. Its service-owned evidence clip
  and approved portrait were corrected after visual review: the clip is bounded
  to 329.25-334.00 seconds and the selected/approved artifact is the preserved
  Ryan Foote candidate, not the earlier race-frame manual capture. The old
  capture remains unselected and auditable.
- UI/API at `http://127.0.0.1:8094` was visually checked for dashboard, review
  queue, and Ryan detail. HTTP Range media serving returned 206.

## Safety audit

- Legacy `config.yaml` SHA-256:
  `84BAE97469F307BA446A6DAF285509C35AF0F263440A3432CE222B1004B5BE56`.
- Historical note: legacy `output` and `work` counts were later corrected to
  four pre-existing artifacts dated 24 September. V2 did not create, modify,
  or remove them.
- The smoke source size and modification time remain unchanged.
- Cleanup remains dry-run only; it never proposes source recordings, active
  revision media, manifests, or approved portraits for deletion.

## Commands

- `scripts/setup.ps1`
- `scripts/initialize_baseline.ps1`
- `scripts/run_service.ps1`
- `scripts/run_worker.ps1`
- `scripts/run_all.ps1`

## Known limitations

- Detector recall, precision, and OCR quality remain inherited from the frozen
  legacy engine.
- PyTorch CUDA DLL 1114 remains a legacy-environment limitation.
- Historical note: V2.1 later introduced the explicit People Intelligence
  consumer through the authenticated bridge; it does not access this service's
  database or filesystem directly.
