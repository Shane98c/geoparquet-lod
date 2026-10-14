# Independently paged nested overviews on all Japan

**This implementation does not retain production performance at national scale.** It preserves the source data and reduces dense overview storage, but sparse nested decoding and scattered source-attribute joins make coarse views much slower. Keep the production version as the preferred map implementation.

The [compact-row follow-up](overview-compact-rows.md) removes those two costs and recovers near-production national overview latency while keeping ordinary Parquet columns. It remains an experimental companion-file layout and is not yet overall parity.

This experiment scales the [independent-page prototype](overview-independent-pages.md) to all **53,742,978 Japan buildings**, comparing it with the preserved production reader and file. It does not replace the production format or public demo.

[Interactive national experiment](http://localhost:8105/?dataset=japan&width=765). Select Nested Japan or Production Japan, then Show map. The five matched zooms are **4, 8, 11, 14, and 16**. Between them, the nested experiment selects the next finer stored level; intermediate zooms are not equivalent comparisons.

## What was built

The converter extracts the production file's existing selected, quantized whole geometries at those five levels. It stores them as ordinary Parquet nested lists/structs of int32 coordinates, using DELTA_BINARY_PACKED and Zstd. Geometry page spans vary independently by level, with up to 2,048 selected features and 262,144 source rows per page. Original source height/class columns have 4,096-row pages and are joined by source row position after geometry filtering. They are stored once, not repeated in each overview.

All 52 analytical row groups and original source values are retained. The converter copies 2,132 compressed source chunks verbatim, reencodes height/class, and replaces the packed display column with five nested geometry columns. Full Arrow equality checks passed for all 1,300 source-column/row-group pairs, including original WKB, IDs, properties and spatial columns. These checks compare values; the experimental footer omits the old optional page-index pointers, so generic readers lose those page-level pruning indexes. Row-group statistics remain.

A 13,511-byte compressed root fits inside the fixed 16 KiB bootstrap read. It identifies 414 directory shards, loaded on demand. The complete compressed directory is 3,356,811 bytes; a city view does not fetch it all. Coordinate leaves remain contiguous within their Parquet column chunks. The national reader coalesces only directly adjacent requested ranges and bounds outstanding fetches to eight. Requests do not include unrelated original source columns.

This is standard Parquet storage, but the quantization transform and spatial discovery metadata are still experimental. Generic spatial tools do not automatically recognize these nested columns as GeoArrow geometry. Original GeoParquet WKB remains available for analysis.

The conversion from the already-built production file took 74.1 seconds: 23.6 seconds staging, 15.9 assembling, and 34.5 validating original source columns. This is a repack measurement, not the time to simplify/thin Japan from raw input.

## Content equivalence

Twenty sampled tile/style combinations have identical geometry and properties for every feature common to both readers. However, **tile membership is not exactly equal**. Production omits hosted copies of features whose original bounds are less than one pixel wide; the whole-feature nested reader includes their quantized geometries wherever they overlap the tile buffer.

Across the ten geometry-only tile checks, nested returns 3,121 additional neighboring features. Of these, 3,067 are buffer-only; 54 have tiny positive fill inside the tile after coordinate rounding. Dense z14 extras are all buffer-only. No production-only feature was found. These differences must remain disclosed rather than claiming byte-identical or exact visual parity. The browser audit separately compares visible ID sets over the route.

## Browser experiment

Both variants use the same MapLibre renderer and styling. Production uses its actual default coordinator and two CPU workers; nested uses one experimental reader worker. Each HTTP range receives 50 ms server delay, with no bandwidth cap. The map is fixed at 765×540. Three alternating trials cover Tokyo, Osaka, rural Japan, Sapporo, coarse views, style changes, revisits and separate cold z4 geometry/styled views.

The first national trial exposed an unbounded request fanout in the experimental reader when country-wide styling joined thousands of sparse source pages. A shared eight-fetch queue removes that browser resource-limit failure. The measured suite uses the bounded implementation and keeps large feature-ID/range arrays out of the live DOM.

Chrome 154 completed **84 samples with zero map errors**. All 14 run-zero visible feature-ID sets matched between variants, despite the disclosed tile-buffer differences. Captured nested ranges were checked against the Parquet footer and exclude all original source columns except requested height/class. Values below are medians of three alternating trials.

| Measurement                                         |                Production |                        Nested |
| --------------------------------------------------- | ------------------------: | ----------------------------: |
| Cold Tokyo z11 geometry                             |                    359 ms |                      1,095 ms |
| Add height/class at Tokyo z11                       |                    145 ms |                        862 ms |
| Tokyo z14 styled                                    |                    252 ms |                        428 ms |
| Tokyo east z14 pan                                  |                    136 ms |                        170 ms |
| Tokyo z16 styled                                    |                    186 ms |                        206 ms |
| Cached Tokyo z14 revisit                            |                     25 ms |                         24 ms |
| Tokyo z8 styled                                     |                    342 ms |                      5,153 ms |
| Cold Japan z4 geometry                              |                    218 ms |                      5,580 ms |
| Cold Japan z4 styled                                |                    226 ms |                     69,579 ms |
| Cold Japan z4 styled transfer                       |     94,470 B / 3 requests |  5,349,564 B / 7,503 requests |
| Warm Japan z4 style refresh                         |                     43 ms |                      6,701 ms |
| Route transfer, excluding independent cold z4 tests | 4,257,572 B / 25 requests | 14,977,160 B / 5,683 requests |

The ordinary route's summed phase medians are 2.17 seconds production versus 64.75 seconds nested. These are sums of settled view times, not a continuous gesture benchmark. Individual dense views can be close, but that does not rescue the national overview workload.

**Two costs are now isolated.** Cold z4 geometry downloads only 186 KB, yet nested decoding consumes about 4.29 seconds of worker CPU. The current general-purpose nested decoder materializes source-aligned rows across enormous null spans. A country-wide geometry view loads 52 directory shards and 206 geometry page entries. Compact storage on disk does not imply compact decoded work.

Adding height/class to cold z4 then fans out to thousands of 4,096-row source attribute pages for only about 7,000 overview features. The shared source attributes themselves occupy only 11.3 MB across all Japan; fetching selected pages individually creates excessive latency. Even a warm country-wide refresh decodes again because its working set exceeds the 512-entry decoded cache. That refresh transfers zero bytes but still takes 6.7 seconds. Cache and worker differences are part of this implementation comparison, not controlled codec-only effects.

The next experiments should address those measured costs: a standard-Parquet decoder that skips null runs without materializing source-length arrays, and compact ordinary overview attribute columns for sparse coarse levels. Compact per-level overview row sets are another way to address both, at the cost of abandoning source-row alignment. None requires hiding geometry inside a custom binary blob; each needs its own benchmark. This result rejects the present implementation as a replacement, not nested Parquet geometry in general.

## Limits

This is a five-level experiment, while the production file supports its full zoom ladder. Complete sizes of 6.448 GB nested and 7.667 GB production cannot be presented as an encoding-only saving. The nested decoded cache has a 512-entry limit, not a measured heap-byte bound. Settled navigation does not test continuous gesture cancellation. Local HTTP timing does not establish R2, bandwidth-constrained, PMTiles, COGP or Esri parity.

For the five matching levels, compressed display fields total 1.508 GB nested versus 2.106 GB packed, a 28.4% reduction. This comparison still mixes representation with removal of carried attributes and hosted copies. At coarse levels the nested columns are larger: z4 is 154 versus 73 KB, z8 is 6.77 versus 3.29 MB, and z11 is 38.65 versus 30.54 MB. Dense z14/z16 drive the overall reduction. Omitted production levels account for another 565 MB of the whole-file difference. Original source height/class totals change from 12.59 to 11.30 MB after repaging.

## Reproduction

The input is `/private/tmp/japan.tippecanoe-density.spatial.parquet`; generated files stay under `/private/tmp/overview-japan`. The prior production and small-fixture demos remain intact.

```bash
.venv/bin/python benchmarks/overview-alternatives/build-japan-grouped.py
js/node_modules/.bin/esbuild js/src/worker.ts --bundle --format=esm --outfile=/private/tmp/overview-japan/production/worker.js
js/node_modules/.bin/esbuild js/src/index.ts --bundle --format=esm --platform=node --outfile=/private/tmp/overview-japan/production.mjs
js/node_modules/.bin/esbuild js/experiments/overview-alternatives/worker.ts --bundle --format=esm --outfile=/private/tmp/overview-japan/nested-worker.js
js/node_modules/.bin/esbuild js/experiments/overview-alternatives/reader.ts --bundle --format=esm --platform=node --outfile=/private/tmp/overview-japan/reader.mjs
node benchmarks/overview-alternatives/check-japan.mjs
node benchmarks/overview-alternatives/browser-serve.mjs 8105 --japan
```

The tile checker deliberately fails its membership assertion while preserving detailed evidence: the known boundary difference is not silently waived. Run the benchmark in the linked browser page, then run `.venv/bin/python benchmarks/overview-alternatives/summarize-japan.py` to audit and preserve its results.

[Build, preservation, boundary and storage evidence](../benchmarks/overview-alternatives/japan-build-results.json), [browser samples and range audit](../benchmarks/overview-alternatives/japan-browser-results.json), and [sampled tile geometry/property comparisons](../benchmarks/overview-alternatives/japan-tile-validation.json).
