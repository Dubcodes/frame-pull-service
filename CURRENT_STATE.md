# Current State

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
