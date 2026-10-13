# Reusing geometry across style changes

The packed file is unchanged. The reader now retains a bounded set of decoded
overview tiles, hydrates newly requested source attributes by row ID, and uses
the CPU pool for source-page projection and MVT encoding. This makes exploratory
styles substantially faster while keeping the packed map access path.

On the same 4,882,169-row Tokyo file, a new source attribute in the city viewport
fell from **797 to 402 ms**. Repeating that style fell from **245 to 81 ms**.
Every measured scenario retained the same range count, byte count, and rendered
MVT bytes as the preceding reader. This is a client-side change, with no file
rewrite, attribute restriction, geometry clipping, or new stored tile payload.

## Measurements

Baseline: commit `b0c8c44`. Five runs, alternating baseline/current order, two
actual Node CPU workers, 50 ms latency per range, identical packed file from the
[separation ablation](parity-separate.md). Metro is 2×2 tiles at z8; city and
neighborhood are 3×3 at z11 and z14. Cold viewport timings exclude the separately
measured file open. This does not simulate bandwidth limits or MapLibre painting,
and is not a new PMTiles comparison.

Median times, before → after:

| View         | Cold geometry | Cold carried style | First new source attribute | Repeat source style | Remove then restore source style |
| ------------ | ------------: | -----------------: | -------------------------: | ------------------: | -------------------------------: |
| Metro        |  175 → 188 ms |       189 → 197 ms |               712 → 373 ms |         128 → 71 ms |                      122 → 73 ms |
| City         |  202 → 212 ms |       216 → 220 ms |               797 → 402 ms |         245 → 81 ms |                      236 → 70 ms |
| Neighborhood |  162 → 168 ms |       170 → 171 ms |               328 → 277 ms |          92 → 29 ms |                       85 → 28 ms |

The common style requests `height` and `class`; the new attribute is source
`version`. The first new-attribute read costs 5/10/8 requests and
885,131/662,284/120,402 bytes respectively, unchanged from the baseline. Repeats,
property removal, and restoration use zero network requests. They also perform
zero additional geometry decodes. Cache instrumentation confirms this in every
run after the initial viewport and pan.

There is a real cold-load tradeoff: decoding all carried attributes and retaining
geometry adds about 6–13 ms to an unstyled cold viewport and 1–8 ms to a styled
one here. Warm geometry and style changes benefit. Keep frequently used display
attributes packed; a cold arbitrary-column change still costs hundreds of
milliseconds and has not reached the carried-attribute path's latency.

## What changed

1. A coordinator LRU keys tiles by source URL and z/x/y, independent of style and
   layer name. It retains decoded whole overview geometry, source row IDs,
   carried values, and subsequently requested scalar source values. Concurrent
   requests for the same geometry share one load.
2. A cold carried-style request decodes, encodes, and returns reusable geometry
   in one CPU job. Subsequent styles copy the retained coordinate buffers to
   an available worker; the cached originals are never detached or mutated by
   encoding. Point filtering happens once, before the tile enters the cache.
3. Source projection fetches ordinary Parquet pages in the coordinator, then
   sends serializable page runs, schema information, dictionaries, and selected
   row IDs to the pool. Decoding retains only the requested values, avoiding
   maps containing every unselected row of a source page. Direct `readColumns`
   calls use this selected-row decoder locally unless given an executor.
4. Hydrated scalar columns stay with the retained tile, so repeated styles skip
   both page decoding and the per-row source-cache lookup. Removing a property
   does not erase its cached values. Attribute names continue to distinguish
   carried display values from original source values as before.

One-worker mode uses the same cache and local decoding. The decoded-tile cache
currently applies to indexed **packed** overviews in the MapLibre adapter.
Direct `renderTile` calls, exact-primary fallback, and the experimental separated
layout retain their existing geometry-loading paths. Source projection improvements
also apply to their `readColumns` calls. Source files must remain immutable,
as required by the existing range/page caches.

Cancelling a tile stops that request's wait or queued CPU job. Shared geometry
loads may finish and populate the cache for another caller. A failed load is
removed and can be retried. Source hydration failures do not corrupt the retained
geometry or carried values.

## Memory and startup costs

The additional decoded cache is capped at **32 MiB of accounting and 64 tiles**,
shared across sources in one coordinator. It counts full typed-array backing
stores and estimates JS array/string storage. Columns added by hydration count
toward the same budget. Oversized tiles are rendered without being retained;
LRU eviction causes later requests to decode again.

Peak accounted cache storage in these sequences was 17.6 MiB metro, 26.9 MiB city,
and 14.0 MiB neighborhood, with at most 12 retained tiles. These figures are not
process-memory or heap bounds. Existing compressed-page/source-value caches,
queued and active jobs, buffer copies, decoder allocations, and worker heaps
consume additional memory. Large individual geometries and mobile memory
pressure still need dedicated production measurements.

The bundled worker grew by about **3.7 KB gzip** in the local comparison
(120,710 → 124,458 bytes; baseline bundled from emitted JS). It remains the same
worker URL used by the coordinator and CPU pool, with no additional JS asset.

## Validation and browser check

- All benchmark MVT outputs were SHA-256 matched against the prior reader for
  geometry, carried styles, source styles, repeats, removal, restoration, and pan.
  Every recorded range/byte count also matched across all five runs.
- Points, boundary-crossing lines and polygons, nulls, LOD overrides, and prefix
  layouts passed the existing Python/JS integration checks. Added cases cover
  cold source-attribute styling and repeated changes with one and two workers,
  source-URL isolation, unknown-column failures, and unchanged geometry-decode
  counts.
- Source projection is independently checked against PyArrow values, including
  duplicate/out-of-order row IDs, dictionary encoding, v2 pages, strings, nulls,
  unsigned integers, booleans, and half/double floats, locally and in real workers.
- Cache checks cover shared loads, independent cancellation, failed-load retry,
  LRU eviction, oversized entries, hydration growth, and immutable rendering.
  Existing pool cancellation, transport ownership, and failure checks still pass.
- A native browser-worker smoke check on localhost passed carried → source →
  repeated source styling, exact output equality, zero repeat transfer, source
  row inspection, and a whole-feature query. One run took 311 ms for the first
  source style and 63 ms for the repeat. The Long Tasks observer reported none
  on the main thread during this harness. It does not include MapLibre painting,
  and those single-run localhost times are not the controlled benchmark above.

23 Python tests, the JS suite, TypeScript build, and browser bundle pass.

The next useful work is reducing first-use attribute latency and measuring
memory under sustained navigation on large polygons/lines. The measured gain
here comes from retaining and reusing work, while the immutable file and its
GeoParquet analytical capabilities stay intact.

## Reproduce

Build the baseline in a temporary directory using the current installed dependencies:

```bash
style_baseline_root=$(mktemp -d /tmp/lod-style-baseline.XXXXXX)
git archive b0c8c44 js | tar -x -C "$style_baseline_root"
ln -s "$PWD/js/node_modules" "$style_baseline_root/js/node_modules"
(cd "$style_baseline_root/js" && npm run build)
npm --prefix js run build
RUNS=5 node js/test/style-benchmark.mjs /tmp/lod-separate/packed.parquet \
  "$style_baseline_root/js/test/worker-client.mjs" 50 \
  > docs/parity-style-benchmark.json
npm --prefix js test
.venv/bin/pytest -q
```

Use `benchmarks/build-separate.py` to recreate the unchanged Tokyo input as
explained in the previous report. Full samples, open timings, cache accounting,
and projection/decode counters are in
[parity-style-benchmark.json](parity-style-benchmark.json).
