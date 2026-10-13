# Single Hilbert order and rendering CPU experiment

Follow-up: [shared-cache tile workers and compression page sizes](parity-workers.md).

This follows the [spatial page index experiment](parity-experiment.md) on the
same 53,742,978 Japan buildings. It tests removing the coarse-prefix/main split
and reducing the cost of turning packed overviews into MVT. No stored MVT layer
or second geometry representation was added.

## Results

The single-order build with sparse page sizing reaches **two cold requests and
one neighbour request in all six individual-tile samples**, matching PMTiles.
Opening remains one 16 KiB request. Full performance parity is not reached:
coarse overviews still transfer more bytes, and dense views still pay for MVT
encoding.

| Tile          | Previous layout KB / requests | Single order KB / requests | PMTiles KB / requests | Single / PMTiles ms |
| ------------- | ----------------------------: | -------------------------: | --------------------: | ------------------: |
| Tokyo metro   |                     659.0 / 3 |                  778.9 / 2 |             499.4 / 2 |           184 / 114 |
| Tokyo city    |                     429.9 / 4 |                  369.2 / 2 |             306.2 / 2 |           145 / 110 |
| Shibuya       |                     405.7 / 4 |                  368.7 / 2 |             349.8 / 2 |           147 / 110 |
| Chiba         |                     111.5 / 4 |                   81.7 / 2 |              60.3 / 2 |           114 / 107 |
| Tokyo station |                      61.9 / 4 |                   35.1 / 2 |              32.4 / 2 |           106 / 107 |
| Osaka         |                      90.7 / 4 |                   63.2 / 2 |             111.8 / 2 |           111 / 107 |

Both Parquet columns above use the new renderer, isolating the access paths
from the CPU improvement. The old fixture stores `class` as strings; this
rebuild uses the current writer's existing value table for that attribute.
Selection and rendered geometry/properties match across the 14 compared tiles,
but byte differences therefore include the dictionary change as well as layout.
The original experiment's Tokyo metro time was 274 ms; the combined result here
is 184 ms. These are separate runs; the paired CPU comparison below is the
controlled measure of rendering improvement.

The new file is **7.727 GB**, compared with 7.846 GB for the previous indexed
fixture. Its unoptimized write is 7.579 GB. The directory shrinks from 39.2 MB
to 26.4 MB; the root is 9,028 bytes, within the existing 16 KiB bootstrap.

## Why sparse page sizing matters

Simply removing the prefix initially doubled Tokyo metro reads from 659 KB to
1,396 KB. A 2,048-source-row cap leaves sparse pages with very few actual values.
The optimizer now scales that row cap by each LOD chunk's density, targeting
2,048 present values, capped at 262,144 source rows. The 16 KiB page byte target
and finest-LOD tile boundaries remain in force. Tokyo metro then reads 779 KB,
and its neighbour falls from two requests to one. `optimize --fixed-pages`
reproduces the original cap; `--keep-pages` bypasses page re-encoding entirely.

This fixes most of the regression, but the metro tile still uses 18% more bytes
than the prefix fixture and 56% more than PMTiles. Its LOD also shows 115,775
features versus PMTiles' 95,774; comparisons are not equal-content encodings.
City and fine-zoom reads improve, so single order is useful for that workload;
the coarse-prefix option remains available and the writer default is unchanged.

## CPU, on the same existing file

The baseline is commit `b57e6f5`, using the same dependency versions and the same
prefix fixture as the new implementation. Values below are median milliseconds
for reading cached compressed pages, decoding geometry and generating identical
MVT bytes. No simulated network latency is included.

| Tile        | Previous, no properties | New, no properties | Previous, height/class | New, height/class |
| ----------- | ----------------------: | -----------------: | ---------------------: | ----------------: |
| Tokyo metro |                   139.7 |               57.1 |                  145.6 |              72.5 |
| Tokyo city  |                    46.0 |               20.9 |                   49.2 |              24.7 |
| Shibuya     |                    37.2 |               20.8 |                   40.2 |              25.1 |

The improvement is 1.6–2.4× across these cases. The worker grows by roughly 4 KB
gzipped (114 KB to 118 KB, unminified bundles compressed with gzip).

## Complete viewports and pans

These use 2×2 tiles at z6/z8 and 3×3 tiles at z11/z13/z15. Each cell below is
KB / requests / milliseconds. The LOD source uses its existing range coalescer;
PMTiles uses its default per-tile source behavior. Parallel requests do not add
a round trip each, so fewer requests need not mean lower elapsed time.

| Initial viewport |  Previous layout |     Single order |           PMTiles |
| ---------------- | ---------------: | ---------------: | ----------------: |
| Japan overview   |  609.0 / 3 / 184 |  559.0 / 3 / 171 |   408.0 / 4 / 111 |
| Tokyo metro      |  942.0 / 8 / 218 | 1126.7 / 4 / 215 |   723.1 / 5 / 114 |
| Tokyo city       | 2592.5 / 8 / 275 | 2208.5 / 4 / 258 | 1876.1 / 10 / 116 |
| Chiba            |  468.9 / 8 / 145 |  389.1 / 5 / 144 |   314.4 / 8 / 108 |
| Osaka            |  405.0 / 6 / 138 |  394.6 / 4 / 139 | 1180.3 / 10 / 113 |

| Newly exposed pan column | Previous layout |   Single order |        PMTiles |
| ------------------------ | --------------: | -------------: | -------------: |
| Japan overview           |   47.9 / 2 / 59 |  44.7 / 2 / 57 |  38.8 / 2 / 54 |
| Tokyo metro              |   40.8 / 4 / 63 |  49.5 / 2 / 58 |  36.4 / 2 / 54 |
| Tokyo city               |  262.0 / 4 / 80 | 233.0 / 2 / 69 | 193.2 / 3 / 54 |
| Chiba                    |  165.4 / 4 / 69 | 165.5 / 2 / 69 | 148.7 / 3 / 54 |
| Osaka                    |   97.2 / 2 / 60 |  94.7 / 1 / 60 | 612.7 / 3 / 56 |

These windows surround each reference location and pan east into populated
areas. The z15 Osaka PMTiles view covers its z14 parents, hence its larger
feature set and byte count. Requests fall substantially, but the dense Tokyo
city view is still 258 ms against PMTiles' 116 ms. Building MVT for an entire
viewport remains a material cost. Empty tiles can also require host-page reads
where PMTiles can answer absence from its directory.

The rendering changes are useful independently of row order. Keep them; use
`--no-prefix` followed by `optimize` for the next city/street map experiments,
and retain the prefix option for coarse-heavy workloads. The remaining gaps
are sparse coarse-page bytes, empty-tile discovery, and MVT construction cost.

Raw samples: [individual tiles](parity-layout-benchmark.json),
[CPU stages](parity-cpu-benchmark.json), and
[viewports and pans](parity-viewport-benchmark.json).

## Rendering changes

The indexed reader visits present values directly in v1 PLAIN Parquet pages,
scanning definition-level runs without expanding null rows into arrays. Other
page encodings retain the general hyparquet decoder. Selected packed geometries
decode directly into typed coordinate buffers; unrequested attributes and
copies are skipped without constructing geometry trees.

The MVT encoder streams features into one layer buffer, reuses command/tag
buffers, caches repeated latitude projections within each tile, and projects
interior paths into reusable typed buffers. Boundary paths retain the general
clipping algorithm. This implementation is adapted from the MIT-licensed
`@cogp/reader` 2.1.4 encoder, with its notice included in the source and bundle.
The exact-primary-geometry fallback continues using the dependency's encoder.

Byte-for-byte comparisons against that encoder cover clipping, winding, holes,
duplicate and degenerate paths, multi-part shapes, dateline wrapping, negative
scales, large IDs, properties and cancellation. The indexed-reader fixtures
compare both decoding paths with a full-column reference, including v2 pages,
nullable geometry and attributes, boundary copies and analytical row reads.

## Row order and identity

`write(..., prefix_zoom=None)` and CLI `write --no-prefix` use one global
Hilbert sequence. This is a new write: row positions change, and boundary-copy
IDs must refer to the new positions. The subsequent optimizer preserves every
value and row position of that new file. Source geometry and source attributes
remain normal GeoParquet columns.

The experiment exposed a pre-existing tie bug: equal Hilbert keys were sorted
without a source-ID tie-breaker, while copy IDs were calculated assuming that
tie-breaker. The writer now uses the same order in both places. A regression
with repeated keys verifies the copied feature's ID against its analytical
source and compares selection/copy content across prefix and single-order files.

## Validation

All 20 Python tests pass, including the JS reader and worker integration
fixtures. The standalone JS tests, TypeScript build, demo bundle and formatting
checks pass. PyArrow compared every column of all 53,742,978 rows between the
new raw file and its final indexed copy; they are identical. Whole-row source
checksums agree, and 14 rendered tile/neighbor comparisons preserve geometry
and carried properties across row layouts.

The local final fixture is `/private/tmp/japan.single-sparse.spatial.parquet`;
its raw writer output is `/private/tmp/japan.single-fixed.parquet`.

## Reproduction

`benchmarks/write-japan-single.py` rebuilds the Japan fixture with the original
thinning settings, disables the prefix, and optimizes the output. It takes
source, raw output, and indexed output paths. `benchmarks/check-table.py`
compares every value between the raw output and its indexed copy.
`benchmarks/check-analysis.py` compares order-independent whole-row checksums
including every source analytical column and source ID; the full source and
rebuilt file agree on count, XOR and the exact integer sum of row hashes.
This checksum is not an exact equality proof. The raw-to-optimized comparison
is exact and covers every column of all 53,742,978 rows.

The CPU benchmark can import the previous commit for an independent baseline:

```sh
mkdir -p /tmp/geoparquet-lod-baseline
git archive b57e6f5 js/src js/tsconfig.json js/package.json | \
  tar -x -C /tmp/geoparquet-lod-baseline
ln -s "$PWD/js/node_modules" /tmp/geoparquet-lod-baseline/js/node_modules
js/node_modules/.bin/tsc -p /tmp/geoparquet-lod-baseline/js/tsconfig.json
BASELINE_MODULE=/tmp/geoparquet-lod-baseline/js/dist/index.js \
  node js/test/profile-tile.mjs japan.prefix.spatial.parquet
LAYOUTS=1 RUNS=3 node js/test/parity.mjs japan.prefix.spatial.parquet \
  japan.single.spatial.parquet japan.pmtiles 50
RUNS=3 node js/test/viewport.mjs japan.prefix.spatial.parquet \
  japan.single.spatial.parquet japan.pmtiles 50
node js/test/compare-layouts.mjs japan.prefix.spatial.parquet \
  japan.single.spatial.parquet
```

CPU measurements alternate implementation order, discard three warmup rounds,
and report medians of twelve warm-cache runs, with and without carried
properties. Layout measurements use the same new renderer for both files and
three fresh readers per scenario. The viewport test requests visible tiles
concurrently, then only a newly exposed column during an eastward pan.

The network experiment adds 50 ms to each local range read; it does not model
bandwidth or measure a real CDN. Opening is separate. PMTiles has different
thinning and stops at z14, so its z15 entries read z14 parent tiles. MVT output
is included in LOD timings; MapLibre rendering is excluded for both formats.
