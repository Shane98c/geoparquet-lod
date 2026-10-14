# Compact ordinary overview rows: national test

The [standard geometry follow-up](overview-standard-geometry.md) isolates the storage, CPU and interoperability tradeoffs of these integer coordinates versus native float64 and WKB, with identical selected geometry.

**Update: the reader-only follow-up below reduces the smaller-group route from 3.09 s to 2.50 s with identical transfer.** [Try the improved reader](http://localhost:8108/?dataset=japan&layout=compact&width=765); the original 8106/8107 experiments remain available.

**Compact rows remove the national-scale failure of the source-aligned nested prototype.** The 4,096-row-group version loads cold styled Japan in 244 ms, compared with 69.6 seconds for the previous nested implementation and 219 ms for the production control measured in this run. It is not yet production parity across the route.

[4,096-row groups](http://localhost:8106/?dataset=japan&layout=compact&width=765) and [1,024-row groups](http://localhost:8107/?dataset=japan&layout=compact&width=765) are separate local experiments. Select Compact Japan or Production Japan, then Show map. Existing production and source-aligned nested demos remain intact.

## Shape and compatibility

The original Japan file is untouched. This experiment writes an **ordinary companion Parquet table** containing `level`, `source_row`, `geometry`, `height` and `class`. Each selected feature occupies one actual row at that level, rather than a sparse position in a source-length geometry column. `source_row` refers to the zero-based row position in the immutable original dataset. Reordering or replacing that dataset invalidates the link.

The five levels contain 7,182 / 495,590 / 2,313,488 / 53,742,978 / 53,742,978 rows at z4/8/11/14/16, respectively. Total overview rows are 110,302,216. Quantized whole-feature geometry, selection and attributes are taken from the existing production build; hosted boundary copies are omitted. Geometry remains lists/structs of int32 coordinates encoded with standard Parquet delta encoding and Zstd. Source IDs use ordinary int64 delta encoding. Height/class are ordinary scalar columns repeated for the selected overview rows, allowing one local read rather than a sparse source join.

All leaves for one small spatial row group are physically adjacent. The browser eagerly loads height/class with geometry, just as the production packed payload carries those fields, so changing those styles can reuse decoded data. It coalesces nearby requested ranges with an 8 KiB / 25% gap limit and a 4 MiB cap, and bounds concurrent fetches to eight. A compressed bootstrap and spatial directory locate ordinary Parquet column chunks without downloading the full footer.

This is **not yet a single-file source-plus-overview format**. Generic Parquet tools can inspect all overview coordinates and attributes, but the nested integer geometry requires experimental scale/offset metadata and is not automatically recognized as standard GeoParquet or GeoArrow geometry. Original GeoParquet analysis remains available in the unchanged source file. Only the five stored zooms are matched; intermediate zooms use the next finer level.

DuckDB successfully queried per-level counts and source-row bounds, and read `geometry[1][1][1]`, height and class directly without a custom geometry decoder. That establishes ordinary column access, not automatic spatial-function compatibility.

## Validation and controls

Both compact files were built from the full 53,742,978-row source. Native decoding verifies original height/class for every selected feature. Every compact source-group/level table passed a complete Arrow round trip before final assembly. Relocated compressed payload bytes are identical by SHA-256; sampled final-file row groups read identically through PyArrow. Twenty tile/style cases in each variant have exactly the same source IDs, geometry and properties as the previous source-aligned nested prototype.

As in that prototype, production and whole-feature nested rendering differ slightly at boundaries: production suppresses copies of subpixel source features, while nested whole geometry can retain tiny slivers. This was not silently filtered away to force parity. The browser separately verifies visible feature ID sets.

The browser uses the actual production coordinator/two CPU workers and one experimental compact reader worker. Both have the same MapLibre renderer, a 765×540 map, and 50 ms service delay per range without a bandwidth cap. Each suite has three alternating trials and 14 settled navigation/style phases. Timings end at map idle plus two animation frames. Cache implementations differ; these are implementation comparisons, not codec-only measurements.

## 4,096-row group results

All 84 samples completed with zero map errors, and all 14 run-zero visible ID sets matched production. Medians:

| Measurement                   |                Production |                   Compact |
| ----------------------------- | ------------------------: | ------------------------: |
| Cold Tokyo z11 geometry       |                    335 ms |                    760 ms |
| Add height/class at Tokyo z11 |                    143 ms |                    147 ms |
| Tokyo z14 styled              |                    250 ms |                    445 ms |
| Tokyo east z14 pan            |                    138 ms |                    146 ms |
| Tokyo z16 styled              |                    184 ms |                    188 ms |
| Cached Tokyo z14 revisit      |                     25 ms |                     25 ms |
| Tokyo z8 styled               |                    331 ms |                    699 ms |
| Cold Japan z4 geometry        |                    219 ms |                    235 ms |
| Cold Japan z4 styled          |                    219 ms |                    244 ms |
| Cold Japan z4 styled transfer |     94,470 B / 3 requests |     64,974 B / 3 requests |
| Warm Japan z4 style refresh   |                     35 ms |                     43 ms |
| Route transfer                | 4,257,572 B / 25 requests | 7,976,101 B / 60 requests |

Route totals exclude the two independent cold z4 tests and sum phase medians. Summed visible-ready time is 2.11 seconds production versus 3.44 seconds compact. Cold national decoding is now about 25 ms instead of 5.84 seconds. The entire z4 overview fits into one 7,182-row spatial group.

Dense views expose overfetch: Tokyo z16 takes almost the same time on this uncapped connection, but compact transfers 421 KB versus 38 KB. This motivated the additional 1,024-row-group test. Similar localhost latency does not imply equal bandwidth use.

The 4,096-row version is 1,533,927,968 bytes, with 26,929 row groups, a 17.67 MB standard footer, a 6,957-byte compressed bootstrap root and 146 directory shards totaling 2.27 MB. Building and validating it took 107 seconds from the already-built production file; this is repacking time, not raw geometry preparation time.

## Smaller groups

The 1,024-row version keeps the national z4 overview in one group, while reducing the other groups. It is 1,647,551,165 bytes, with 107,713 row groups and a 68.49 MB footer. Its 572 directory shards total 8.72 MB, and a descriptor-manifest tier adds one bootstrap request. Build/validation took 151 seconds. These costs are measured tradeoffs of finer access, not free optimizations.

Again, 84 samples completed with zero map errors and all 14 run-zero visible ID sets matching. Fresh production controls were measured in this suite as well:

| Measurement                   |                Production |       Compact, 1,024 rows |
| ----------------------------- | ------------------------: | ------------------------: |
| Cold Tokyo z11 geometry       |                    336 ms |                    662 ms |
| Add height/class at Tokyo z11 |                    143 ms |                    258 ms |
| Tokyo z14 styled              |                    246 ms |                    304 ms |
| Tokyo east z14 pan            |                    137 ms |                    162 ms |
| Tokyo z16 styled              |                    180 ms |                    149 ms |
| Osaka z14 styled              |                    186 ms |                    287 ms |
| Rural z14 styled              |                    130 ms |                    147 ms |
| Tokyo z8 styled               |                    333 ms |                    638 ms |
| Cold Japan z4 styled          |                    228 ms |                    294 ms |
| Cold Japan z4 styled transfer |     94,470 B / 3 requests |     84,166 B / 4 requests |
| Route transfer                | 4,257,572 B / 25 requests | 5,810,501 B / 98 requests |
| Summed route view times       |                    2.09 s |                    3.09 s |

Relative to 4,096-row groups, smaller groups reduce route bytes by 27% and summed view times by 10%, but increase requests by 63% and complete-file storage by 7.4%. Tokyo z16 drops from 421 to 188 KB, still well above production's 38 KB. Some extra directory transfer is included in this phase, so the phase total is not purely geometry overfetch. The source-aligned predecessor transferred 14.98 MB in 5,683 requests over this route and took 64.75 seconds.

One cache limitation is exposed by the smaller groups: changing style at z11 transfers zero bytes but still incurs 144 ms of decode CPU. The experimental 512-entry cache mixes assembled geometry with decoded leaves, so a view with many small groups can evict reusable geometry. This is a reader issue, not a requirement of ordinary Parquet columns. Larger footers and the additional manifest request are layout costs that also remain to be tuned.

**Recommendation:** retain the compact-row design as the next experimental direction, with the 1,024-row version as the more bandwidth-efficient interactive candidate. Keep production as the default. The experiment has removed the catastrophic coarse-view costs while retaining ordinary column access; it has not established overall PMTiles/production parity or a ready single-file standard. The next focused work would be compact-reader cache/typed-array decoding and spatial grouping that reduces overfetch without multiplying requests. Packaging and standard geometry metadata remain separate unresolved design decisions.

## Reader-only follow-up: separate caches and direct coordinates

The same 1,024-row companion file is reused byte-for-byte through a local symlink. No geometry selection, grouping, attributes, compression or index changes were made. The preserved production bundles are also unchanged. The new reader is isolated at [localhost:8108](http://localhost:8108/?dataset=japan&layout=compact&width=765).

Assembled geometry now has a separate 32 MiB estimated retained-data LRU budget; intermediate leaves have 8 MiB. Cache accounting includes full typed-array backing allocations and conservative estimates for JS arrays/objects/strings. In-flight work is deduplicated separately, never evicted mid-build, and removed after success or failure. These are retained-value budgets, **not total worker/browser heap limits**: active decoding, rendering, directories and pending work consume additional memory. The compressed cache remains 32 MiB, with eight active fetches.

For the compact builder's V1 delta-encoded int32 coordinates, the reader takes standard Parquet definition/repetition levels and fills typed coordinate and offset buffers directly. It avoids constructing nested JS arrays and per-point objects. The current specialization requires the known three-LIST schema and fully defined coordinates; dictionary/V2 pages and null/empty nested structures fall back to ordinary assembly. No on-disk custom coordinate encoding is introduced. Ordinary scalar decoding is unchanged.

All 84 browser samples completed without map errors; all 14 run-zero visible feature ID sets still match production. Medians, with the prior compact run retained for comparison:

| Measurement                   |          Previous compact |          Improved compact |  Fresh production control |
| ----------------------------- | ------------------------: | ------------------------: | ------------------------: |
| Cold Tokyo z11                |                    662 ms |                    535 ms |                    349 ms |
| Add height/class at Tokyo z11 |                    258 ms |                    141 ms |                    148 ms |
| Tokyo z14 styled              |                    304 ms |                    302 ms |                    247 ms |
| Tokyo z16 styled              |                    149 ms |                    138 ms |                    180 ms |
| Osaka z14 styled              |                    287 ms |                    253 ms |                    203 ms |
| Tokyo z8 styled               |                    638 ms |                    394 ms |                    347 ms |
| Cold Japan z4 styled          |                    294 ms |                    285 ms |                    219 ms |
| Summed route view times       |                    3.09 s |                    2.50 s |                    2.17 s |
| Route transfer                | 5,810,501 B / 98 requests | 5,810,501 B / 98 requests | 4,257,572 B / 25 requests |

Summed route decode CPU drops from 1,025 ms to 180 ms (82%); z11 style refresh decoding drops from 144 ms to zero. Route visible-ready time improves 19% against the previous compact run and is now 15% above the fresh production control. Previous-versus-new numbers are separate benchmark sessions, with three alternating production/compact trials per session; they are not simultaneous codec microbenchmarks. Every phase's byte/request total is exactly unchanged. This isolates a reader improvement, but does not isolate the cache and direct-decoding contributions from each other.

Peak retained-cache accounting sampled at phase boundaries is 33,541,210 bytes geometry and 8,388,384 bytes leaves, within the configured budgets. This is not a peak heap measurement. Twenty national tile/style checks remain exactly equal to the previous source-aligned nested reader, including all IDs, geometry and properties; the previously documented production boundary differences remain. Small fixtures additionally compare direct and ordinary decoding across multipolygons, holes, long rings crossing page boundaries, V2/dictionary fallback, empty/null geometry and nullable scalar attributes. Cache tests cover backing-buffer accounting, in-flight reuse during eviction, oversize values and retry after failure.

**Next bottleneck:** request count and spatial overfetch. Coarse and some warm views are close, but cold Tokyo still costs 20 requests versus four; the route still transfers 36% more bytes in nearly four times as many requests. Fine spatial grouping plus better physical ordering/coalescing is the next layout experiment. Keep this reader and these immutable data files as its control. Single-file packaging and standard geometry metadata remain unresolved.

Evidence: [browser measurements](../benchmarks/overview-alternatives/japan-compact-reader-browser-results.json), [tile validation](../benchmarks/overview-alternatives/japan-compact-reader-tile-validation.json).

## Reproduction and evidence

```bash
.venv/bin/python benchmarks/overview-alternatives/build-japan-compact.py
# Separate optional finer-group build; neither command overwrites a completed file.
.venv/bin/python benchmarks/overview-alternatives/build-japan-compact.py --group-rows 1024 --directory /private/tmp/overview-japan-compact-1024
js/node_modules/.bin/esbuild js/experiments/overview-alternatives/worker.ts --bundle --format=esm --outfile=/private/tmp/overview-japan-compact/nested-worker.js
js/node_modules/.bin/esbuild js/experiments/overview-alternatives/reader.ts --bundle --format=esm --platform=node --outfile=/private/tmp/overview-japan-compact/reader.mjs
node benchmarks/overview-alternatives/check-japan.mjs --compact
node benchmarks/overview-alternatives/browser-serve.mjs 8106 --japan --compact
# Run the linked browser benchmark, then preserve and audit results:
.venv/bin/python benchmarks/overview-alternatives/summarize-japan.py --compact
```

For the smaller build, use its directory for bundles and pass `--directory /private/tmp/overview-japan-compact-1024` to the checker/server/summarizer. Pass a distinct `--output` to the summarizer. The production worker bundle remains the preserved `/private/tmp/overview-japan/production/worker.js` from the [preceding national test](overview-japan-pages.md).

Evidence: [4,096-row build](../benchmarks/overview-alternatives/japan-compact-build-results.json), [browser samples](../benchmarks/overview-alternatives/japan-compact-browser-results.json), [tile validation](../benchmarks/overview-alternatives/japan-compact-tile-validation.json).

Smaller-group evidence: [build](../benchmarks/overview-alternatives/japan-compact-1024-build-results.json), [browser samples](../benchmarks/overview-alternatives/japan-compact-1024-browser-results.json), [tile validation](../benchmarks/overview-alternatives/japan-compact-1024-tile-validation.json). [Ordinary DuckDB queries](../benchmarks/overview-alternatives/japan-compact-duckdb-validation.json).

Reader follow-up reproduction (use a new scratch directory to preserve prior bundles/results):

```bash
mkdir -p /private/tmp/overview-japan-compact-reader
ln -s /private/tmp/overview-japan-compact-1024/japan.compact.parquet /private/tmp/overview-japan-compact-reader/japan.compact.parquet
js/node_modules/.bin/esbuild js/experiments/overview-alternatives/reader.ts js/experiments/overview-alternatives/byte-cache.ts js/experiments/overview-alternatives/direct-coordinates.ts --bundle --format=esm --platform=node --out-extension:.js=.mjs --outdir=/private/tmp/overview-japan-compact-reader
js/node_modules/.bin/esbuild js/experiments/overview-alternatives/worker.ts --bundle --format=esm --outfile=/private/tmp/overview-japan-compact-reader/nested-worker.js
.venv/bin/python benchmarks/overview-alternatives/check-compact-reader.py /private/tmp/overview-japan-compact-reader/fixtures
node benchmarks/overview-alternatives/check-compact-reader.mjs /private/tmp/overview-japan-compact-reader
node benchmarks/overview-alternatives/check-japan.mjs --compact --directory /private/tmp/overview-japan-compact-reader
node benchmarks/overview-alternatives/browser-serve.mjs 8108 --japan --compact --directory /private/tmp/overview-japan-compact-reader
# Run browser benchmark, then audit:
.venv/bin/python benchmarks/overview-alternatives/summarize-japan.py --compact --directory /private/tmp/overview-japan-compact-reader --output benchmarks/overview-alternatives/japan-compact-reader-browser-results.json
```
