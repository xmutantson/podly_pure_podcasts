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

New download filenames use a SHA-256 identity derived from the feed database ID
and episode GUID (episode ID/URL fallback for incomplete post objects). Titles
are display metadata and can change without changing the source path. Identity
is in the filename so processed-output and classification paths inherit the
same separation, even when feed titles also collide.

Persisted database audio paths continue to support existing installations. We do
not scan, migrate, delete, or assume ownership of old title-only files. An
unreferenced legacy title file is not an automatic cache hit. Already persisted
historical collisions cannot be authenticated retroactively and need manual
inspection before clearing only the affected database paths and reprocessing.
Cleanup now uses the persisted source path rather than guessing from a title.

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
