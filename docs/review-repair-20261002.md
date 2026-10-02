# Independent review repairs, 2026-10-02

## Classification retry provenance

`classify()` publishes `manifest.json` with the current transcript digest and
ordered chunk plan before issuing requests. Each chunk receipt binds its response
hash to the transcript, chunk size/index range, rendered prompt, system prompt,
model, endpoint, token budget, episode/feed identity, and test mode. Only those
listed chunks can affect ad removal. An abandoned or obsolete folder is ignored;
a missing/invalid current chunk still fails processing. Matching completed chunks
are reusable after interruptions. Old classifications without receipts are
reclassified; they are not silently imported or deleted. An empty transcript has
an explicit empty manifest rather than relying on directory contents.

Receipts publish after responses using atomic writes. Interrupted writes or a
changed response cannot validate against a previous receipt. Multi-process
coordination remains outside this patch; processing locks are process-local.

## Stable audio identity

Each feed now has a persistent random UUID storage namespace, separate from its
SQLite primary key. The follow-up independent review demonstrated that SQLite
can reuse a deleted feed ID, so the previous numeric-ID/GUID filename policy
was insufficient. Deleting and recreating a feed generates a fresh namespace,
even if its RSS URL, episode GUID, enclosure URL, and title are all unchanged.
Renaming a feed/episode or updating its RSS address retains the existing namespace.

Version 2 download paths (`in/v2/episode-v2-<hash>/episode-v2-<hash>.mp3`) hash the
storage version, namespace, and episode GUID. The identity remains in the filename
so processed-output and classification paths inherit the separation. Database
Post objects without a persisted namespace are rejected; non-database URL-only
callers use a versioned enclosure-URL identity, never reusable numeric IDs.

A same-directory JSON receipt records versioned ownership and the audio SHA-256.
Disk-discovered downloads are reusable only when this receipt and file contents
match. Missing, malformed, stale-owner/version, empty, or modified files trigger
a fresh download. The audio publishes atomically before its receipt; interruption
between the two cannot bless mismatched bytes. Successful repeat downloads reuse
the verified cache. Verification reads the whole file and adds disk I/O.

Migration `f72b62c9d310` follows `ded4b70feadb`, backfills one random namespace per
existing feed, and enforces uniqueness/non-nullability. The normal Flask-Migrate
startup upgrade applies it; repeating startup does not rotate namespaces. Run the
migration before using the changed model against an existing database. The new
column has an ORM default for new feeds; raw SQL feed creators must supply their
own unique UUID namespace. No deployment database was touched in this repair.

Existing database audio paths, transcript records, and whitelist flags are
preserved. Explicit persisted paths remain usable for migration compatibility;
they are not retroactively proven by a new receipt. We do not scan, migrate,
delete, or adopt old title-only/numeric-ID files. An orphaned legacy file never
becomes a new download merely because a computed name matches. Historical wrong
bytes already associated with a database row still require manual inspection.
Cleanup uses the persisted source path rather than guessing from a title.
Migration restart behavior is protected by a real SQLAlchemy savepoint around
schema changes, namespace backfill, and constraint/table reconstruction. This
covers SQLite's deferred outer transaction, which previously let `ADD COLUMN`
survive a backfill failure. An interrupted attempt rolls these changes back.
For partial states left by the previous implementation, the migration checks
whether the column and constraints already exist and fills only NULL namespaces.
It retains every already-assigned namespace and all existing Feed/Post/Transcript
data. It can also finish a retry after the schema succeeded but Alembic's revision
update failed. No global database transaction settings were changed.

Regressions inject failures at the first/second backfill writes, table copy,
table rename, and revision update against actual on-disk SQLite/Alembic, then
perform a clean startup retry. Separate tests seed committed partial backfill,
completed backfill, and completed schema with the old revision to verify stable
namespace reuse. These are synthetic transient failures, not power-loss testing.

Downgrading removes the namespace column but retains media and Post paths;
re-upgrading assigns new namespaces and does not adopt orphaned version 2 files.

## HTTP recovery and whitelist behavior

The HTTP helper delegates to the same recovery function as scheduled processing.
Completed processed audio is returned before downloading the original, and a
valid persisted source is reused when processing must resume. Individual requests
and scheduled jobs still require the episode whitelist. The existing explicit
bulk-download action retains its documented bypass, without changing whitelist
flags. The helper reloads the post in its own application/database context.

## Feed URL destination concern: static review only

`routes.add_feed()` passes form input to `feeds.add_or_refresh_feed()`, which calls
`fetch_feed()` and passes the URL directly to `feedparser.parse()`. The downloader
separately uses syntax validation and `requests.get()` for enclosure URLs.
Neither path expresses an application destination policy. This is a static
code observation, not evidence of reachable deployment destinations.

Hardening options for a separately scoped change:

- Centralize feed and enclosure fetching behind an explicit HTTP(S) destination
  policy, with approved hosts/ports where the product permits an allowlist.
- Reject credentials and non-network input forms; validate normalized URLs before
  handing downloaded bytes to the parser.
- Apply destination policy at every redirect and at the actual connection's
  resolved address, including IPv4/IPv6 special-use ranges, so DNS changes cannot
  bypass an earlier decision. A vetted transport or egress proxy is preferable
  to a standalone DNS precheck.
- Bound redirects, response sizes, and connection/read budgets; preserve TLS
  verification. Define which private feeds, if any, are intentionally supported.
- Cover the policy with mocked transport/DNS tests and no network probes. An
  operator could later choose egress restrictions after deployment review.

No URL destination policy or live network setting was changed here. No network
probes, real feeds, real service calls, or exploit reproduction were performed.

## Validation

The repair adds offline regressions for changed classification inputs, interrupted
current chunks, response tampering, stable identity, legacy title-cache rejection,
and individual/bulk whitelist recovery. The original independent assertions are
unchanged. Existing cache tests now require provenance and verify a second retry
reuses a freshly validated response; the filename fixture uses a Post identity.
All providers and downloads are mocked and sockets are blocked. Exact final
commits, commands, counts, and environmental limits are in the sibling repair
report under `D:/Codex/adversarial-review-20261002/media-repair/`.
