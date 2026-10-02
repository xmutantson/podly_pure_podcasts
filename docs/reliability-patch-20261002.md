# Offline reliability patch, 2026-10-02

## Baseline and upstream choice

This local branch starts at the freshly fetched `origin/dl_periodic`,
`06a88e563e4378d695d0b7adcd73d8d3aaf9b342`. No deployed installation,
configuration, database, or other fork branch was changed.

The upstream comparison used
[`d58dd9575abdbdcb9ceb00789e80e3e3cd43d8e9`](https://github.com/podly-pure-podcasts/podly_pure_podcasts/commit/d58dd9575abdbdcb9ceb00789e80e3e3cd43d8e9).
Fork `main` is still 0 ahead / 462 behind; `dl_periodic` is 1 ahead / 497 behind.
The latter's image-metadata guard is already implemented in upstream
`src/app/feeds.py`. Upstream `frontend/src/components/FeedDetail.tsx` has
“Enable all episodes.” Upstream changes 372 files relative to `dl_periodic`,
including the frontend, configuration, processing architecture, and migrations.
This supports a small reliability patch now, rather than a blind upstream merge.

The existing bulk-download route, parallel scheduler, whitelist settings, and
fade configuration remain in this branch. Historical `dl_all` explicitly sets
every post's whitelist flag; `dl_periodic`'s bulk route downloads without changing
those flags. These are different product choices. This patch does not silently
import the historical branch's whitelist mutation or replace either branch.

## Changes

- Scheduled retries retain existing source audio and successful classification
  chunks. Missing, empty, and non-file paths are reconciled with database state.
- Downloads, processed audio, and new classification cache files are published
  with a same-directory atomic rename. A failed write leaves no completed-looking
  partial file. Existing completed audio can repair its missing database path.
- Empty/malformed classification responses fail processing. Invalid cached
  responses are retried; missing or invalid classification files cannot silently
  produce a successful “no ads” result. Explicit test-whisper behavior is retained.
- `job_timeout` is a **soft** budget reported after completion. The decorator runs
  in the caller and propagates exceptions; it never leaves an abandoned worker
  writing files behind the executor. Downloads now have 10-second connection and
  60-second inactivity timeouts. These are not whole-download deadlines.
- Correct seconds/milliseconds conversion for trailing ads and retain the full
  outer interval when merging nested ad intervals.

## Validation and limits

Run from the repository root with Python 3.11 and the project's dependencies:

```text
python -m pytest src/tests -q --disable-warnings
```

Result in the isolated Windows/Python 3.11.7 environment: **40 passed, 3 skipped**
(26 deprecation warnings). The skipped tests are the existing live LLM, remote
Whisper, and local Whisper probes. FFmpeg was 8.1; Pydantic 2.13.5, pytest 9.1.1,
Flask-SQLAlchemy 3.1.1, and LiteLLM 1.103.2 were installed into a task-local venv.
No repository dependency lockfile or global Python environment was changed.

Black/isort checks and Pylint pass for the changed Python files. Strict mypy
reports the same five errors in four application files on the original baseline
and this patch: absent Whisper, its now-unused ignore, missing bleach/waitress
stubs, and the inherited Jinja Template return type. It is not a clean full-CI
result. Whisper was deliberately not installed or allowed to fetch models.

Whisper is mocked, model calls and episode downloads use synthetic responses,
the database is in-memory, and sockets are blocked in automated tests. Existing
FFmpeg tests use the checked-in audio fixture without playback. Their output paths
are portable to Windows and duration checks allow 100 ms of codec padding;
chunk counts, timestamps, and upload size limits are also checked.

The new selected regression subset fails against the original source: 11 failed,
1 passed. It identifies deleted retry input, unusable file-state acceptance,
missing database repair, false classification success, and interval corruption.
The new file utility and test fixtures were supplied only to run those tests;
the baseline application functions remained unchanged.

Local/model processing can still block indefinitely: a real hard deadline needs
an isolated, cancellable process with a defined database/file handoff. Existing
nonempty files and old syntactically valid fallback classifications cannot be
distinguished from good results automatically; no historical data was purged.
Title-derived filenames and process-local locks remain inherited limitations,
especially for duplicate episode titles or multiple server processes.

Any later upstream migration should use disposable copies of the deployment's
database and media, disabled network/provider calls, and explicit checks for
whitelist, fade, scheduling, and bulk-download behavior. No deployment snapshot
was available or accessed in this lane.
