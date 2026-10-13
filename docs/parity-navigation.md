# Stop downloading abandoned views

The reader now cancels shared work when its last consumer leaves. The immutable
Japan file, all geometry detail, source columns, and cache budgets are unchanged.
This addresses transient navigation waste, not the intrinsic byte cost of a
fully loaded viewport or the different detail policies of this PMTiles archive.

## Measurements

Three alternating before/after runs on the 53,742,978-row Japan file, two actual
CPU workers, 50 ms per range. Each route requests nine tiles at zooms
11, 12, 13, 14, 15, 16, 17, 16, 14, 12, 11, moves slightly east on the way out,
and finishes at the original Tokyo city viewport. Intermediate views either
finish or are cancelled after 40/100 ms. The final view always finishes.
Totals include opening and a drain period for abandoned baseline work.

| Navigation                | Completed payload before → after | Reduction | Request starts before → after | New aborted requests |
| ------------------------- | -------------------------------: | --------: | ----------------------------: | -------------------: |
| Every view finishes       |                   9.04 → 9.04 MB |        0% |                       58 → 58 |                    0 |
| 40 ms intermediate views  |                   8.80 → 2.13 MB |       76% |                       39 → 15 |                   10 |
| 100 ms intermediate views |                   8.80 → 6.20 MB |       30% |                       39 → 42 |                   16 |

Payload totals and counts agree across all three runs. Every completed viewport
in the settled route, and the final viewport in each cancelled route, has the
same MVT SHA-256 before and after. The surviving requests do not receive an
AbortError from departed neighbours. The harness records started, completed,
and aborted ranges, unique completed bytes, and refetched completed bytes.
Refetched completed bytes are zero in these routes; this is not an eviction
stress test and does not reproduce the user's entire navigation session.

The transport simulation respects AbortSignal during its delay, then delivers a
whole range at once. These are completed payload bytes, not measured internet
wire bytes. Partial response bodies, HTTP headers, bandwidth contention, browser
painting, and CDN behaviour are not simulated. Actual savings depend on how much
of an abandoned response has already arrived. This is a comparison with the
preceding GeoParquet reader, not a fresh PMTiles parity claim.

## Implementation and tradeoffs

- A shared load gives each caller an independently cancellable subscription.
  It starts lazily and aborts its controller only when the final subscriber
  leaves. Completed results remain reusable. An abandoned entry is replaced
  immediately on a new lookup; late failures cannot evict its replacement.
- Decoded tiles, spatial directories, compressed page runs, and exact-geometry
  bbox pages carry those subscriptions through to the range source. Queued
  CPU decoding also receives the shared signal. Running synchronous worker
  computation may still finish; its cancelled result is discarded.
- The analytical footer opens through the same ownership mechanism. Leaving
  high-detail view can stop an unused footer read, while a surviving query or
  row inspection keeps it alive. Completed readers continue to use independent
  per-operation signals.
- Coalesced requests discard cancelled members before dispatch, reject cancelled
  members promptly, and remove listeners when finished. A merged range stays
  alive while any member needs it. The MapLibre coordinator batches for 1 ms to
  collect neighbouring tile requests across microtasks; direct API callers keep
  the existing microtask default unless they set `windowMs`.

The 100 ms sequence still starts three more requests than the baseline, although
it completes fewer and transfers less. Immediate cancellation can also remove
work that a later return would have reused. A 16 ms grace-period experiment
reduced the byte savings substantially, so it was not retained. The 1 ms batching
window reduced the 100 ms sequence's starts from 68 to 42 in a one-run ablation
without changing its completed payload. Cache eviction, page overfetch, and
high-zoom exact reads remain separate sources of session bandwidth.

The comparison HUD also ignores expected cancellation events, including
MapLibre 5's plain `Error('AbortError')` during interrupted `setTiles` updates.
Other transport and decode errors remain visible. This is separate from the
shared-bbox cancellation bug, which is covered by the reader regression tests.

## Validation and reproduction

The JS suite and 23 Python tests pass, including real Parquet fixtures with
polygons, points, lines, prefix layouts, and v2 pages. Cancellation tests cover
one surviving neighbour, both neighbours leaving, abandoned analytical opening,
retry after cancellation, late completion versus replacement, pre-dispatch
cancellation, shared range ownership, and cross-microtask batching.

The baseline was copied from the working tree before this change: the preceding
style-cache reader plus the shared-bbox correctness fix. Keep its built `js/dist`
and `js/test/worker-client.mjs`; copy the current abort-aware
`js/test/worker-harness.mjs` beside that baseline client so both variants use
identical transport simulation. Then:

```bash
npm --prefix js run build
RUNS=3 node js/test/navigation-benchmark.mjs /tmp/japan.single-64k.spatial.parquet \
  /tmp/lod-navigation-before/js/test/worker-client.mjs 50 \
  > docs/parity-navigation-benchmark.json
```

Full samples are in [parity-navigation-benchmark.json](parity-navigation-benchmark.json).
