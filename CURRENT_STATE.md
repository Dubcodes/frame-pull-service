# Current State

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
- Legacy `output` and `work` contain zero files.
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
- No People Intelligence consumer is included. The next task is the explicit
  API consumer integration; do not directly access its database or filesystem.
