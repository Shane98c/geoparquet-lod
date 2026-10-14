# Browser prototype: nested quantized overviews with compact spatial discovery

Follow-up: [independent overview pages](overview-independent-pages.md) substantially reduces requests using standard contiguous column streams. This report preserves the earlier shared-page layout results.

The isolated prototype renders nested int32/delta Parquet overviews through our existing MVT encoder and ordinary MapLibre styling. **The dense encoding is promising, but this prototype does not match packed access across the complete navigation/style workload.** Keep the current production baseline. Sparse page grouping and physical source-attribute locality remain the next design issues.

The interactive prototype is at [localhost:8103](http://localhost:8103/). Existing Japan demos on 8097/8098 and their datasets are unchanged. No production reader/writer code was modified.

## What is implemented

The same 91,067-row Tokyo fixture from the [per-level experiment](overview-alternatives.md) now has two complete, standard Parquet files. Each contains original source ID, height, class, exact primary WKB, and four whole-geometry overview columns. The **matched experimental packed baseline** and nested variant have identical source order, selected rows, quantized geometry, source values, 236 spatial row-group/page spans, and buffered-bbox query semantics. Neither stores tile-boundary copies.

Nested overviews use `list<list<list<struct<x:int32,y:int32>>>>` with standard Parquet `DELTA_BINARY_PACKED` and Zstd. Scale/offset and encoding metadata are discoverable through the normal Parquet footer (`overview_experiment`) and the compact bootstrap. **This is custom transform metadata, not standard CRS-valued GeoArrow geometry compatibility.** Original primary WKB remains ordinary GeoParquet.

An embedded 16 KiB bootstrap points to a gzip directory of page bbox envelopes and compressed leaf locations. The browser fetches these and selected pages, without opening the full footer or fetching original WKB/source IDs. The initial read is exactly offset 4, length 16,384; the leading `PAR1` is skipped. This experimental index is embedded in each file, not a sidecar. It uses one fully loaded directory; it does not demonstrate country-scale sharding.

The worker uses hyparquet's standard nested definition/repetition and integer delta decoders, converts coordinates into `GeometryBuilder` arrays, filters whole feature bboxes, and calls existing `toMvt`. The packed control uses the existing direct packed decoder and the same builder/filter/MVT path. Both expose normal MapLibre vector sources and height-based fill styling. Source height/class remain ordinary scalar columns, stored once; packed also carries its display values inside overview blobs.

Stored levels are z8/11/14/16. Intermediate browser zooms use the next finer stored level; only the four measured levels are compared with the reference. This is an access/encoding fixture using `drop_rate(2)`, not Japan's tuned production display policy.

| Complete file component  | Matched packed | Nested delta |
| ------------------------ | -------------: | -----------: |
| File bytes               |      9,502,957 |    9,734,388 |
| Exact primary WKB        |      5,509,852 |    5,509,852 |
| Source IDs               |      1,912,759 |    1,912,759 |
| Scalar height + class    |         56,471 |       56,471 |
| z8 overview              |         14,636 |       35,059 |
| z11 overview             |         41,394 |       85,627 |
| z14 overview             |        316,635 |      442,073 |
| z16 overview             |      1,488,791 |    1,373,563 |
| Bootstrap fetched        |         16,384 |       16,384 |
| Directory fetched        |         36,243 |       45,292 |
| Full footer, not fetched |        109,788 |      257,304 |

Smaller row groups make X/Y chunks adjacent and keep the source/overview tables aligned, but make coarse overviews substantially more fragmented than in the earlier independent per-level layout. This is a prototype limitation, not the production layout.

## Real browser measurements

Chrome 154 on macOS, 765×540 map viewport, one reader worker, three runs with alternating variant order. Each variant starts with a fresh worker/cache. The nine phases then share caches. The local HTTP server adds 50 ms service delay to every data range, with `no-store` responses; no bandwidth cap is modeled. Browser HTTP connection scheduling applies.

Visible-ready includes reader opening in the cold phase, HTTP, worker processing, MapLibre `idle`, and two animation frames. It records a paint opportunity, not instrumented GPU completion. Style transitions and source fade duration are zero. The worker separately records decode and MVT CPU. All requests/bytes include bootstrap, directory, gap fills, and bounded incidental prefetch.

Latest medians; bytes are decimal KB. Every visible feature count matched between variants in every run, and all 54 samples completed without map errors.

| Phase                     | Packed visible ms / KB / requests | Nested visible ms / KB / requests |
| ------------------------- | --------------------------------: | --------------------------------: |
| Cold city z14, geometry   |                  891 / 148.3 / 84 |                  904 / 190.1 / 84 |
| Pan east z14, geometry    |                      90 / 0.5 / 3 |                     102 / 0.7 / 3 |
| Zoom city z16, geometry   |                   210 / 85.6 / 16 |                   210 / 76.0 / 16 |
| Add height/class z16      |                        66 / 0 / 0 |                    226 / 3.9 / 16 |
| Revisit city z14, styled  |                        65 / 0 / 0 |                   625 / 12.3 / 64 |
| Repeat city z14, styled   |                        63 / 0 / 0 |                        65 / 0 / 0 |
| Pan periphery z14, styled |                  944 / 200.1 / 98 |               1,623 / 302.8 / 176 |
| Zoom city z11, styled     |                   873 / 13.4 / 96 |                1,206 / 35.2 / 130 |
| Zoom city z8, styled      |                 1,039 / 8.5 / 110 |                1,212 / 26.0 / 130 |

The nine-phase sums of median transfer are **456,391 bytes / 407 requests packed** versus **647,042 bytes / 619 requests nested**: nested adds 42% bytes and 52% requests. This is a settled sequence, not a canceled continuous zoom route.

Nested's dense geometry transfer is 11% smaller at the z16 step, with the same requests and visible-ready time. Actual browser nested decode is slower here: z16 worker decode medians are 15.3 ms nested versus 3.9 ms packed; cold z14 is 35.6 versus 15.1 ms. These decoder timers include decompression and geometry/list conversion, but exclude bbox precomputation and filtered coordinate copying. MVT CPU is separately measured, around 2–3 ms at z16. These CPU sums are not the visible-ready wall time.

Ordinary scalar styling still needs source pages that packed already carries. A styled z14 revisit adds only 12.3 KB but needs 64 requests, creating a substantial latency penalty. The fully cached repeated styled view transfers zero bytes for both variants. The caches are a 32 MiB compressed-leaf cache and a 512-entry decoded cache; the latter is an entry bound, not a measured heap-byte bound. This small fixture does not establish country-scale memory behavior.

## Locality ablation and its limits

The first browser pass used adjacent-only batching with physical order `ID,height,class,primary-WKB,overviews`. The source WKB separated scalar and overview chunks. A narrow follow-up moved primary WKB before the scalar/overview corridor in **both** files and applied the same policy to both readers:

- Gaps at most 8 KiB, at most 25% of requested payload, with runs capped at 4 MiB.
- Never fetch primary WKB or source IDs as gaps.
- Only within spatially selected pages, cache a cheap scalar/earlier-overview corridor under the same 8 KiB/25% bound.

This is bounded incidental prefetch, not whole-file attribute prefetch. All captured ranges were audited against source WKB/ID chunk locations; none overlap. No attributes are duplicated in the nested file.

| Representative phase | First packed → latest packed       | First nested → latest nested         |
| -------------------- | ---------------------------------- | ------------------------------------ |
| Cold city z14        | 144.0 → 148.3 KB; 84 → 84 requests | 188.4 → 190.1 KB; 84 → 84 requests   |
| Styled z14 revisit   | 0 → 0 KB; 0 → 0 requests           | 12.6 → 12.3 KB; 66 → 64 requests     |
| Styled periphery pan | 183.8 → 200.1 KB; 98 → 98 requests | 287.6 → 302.8 KB; 196 → 176 requests |

The follow-up removes some fragmentation, but does not solve attribute locality. It also increases transfer through gap fills/prefetch. Prefetching scalar columns is redundant for packed because the attributes are already carried: **the increased packed bytes are not a production improvement and must not be sold as parity.** Against the original packed peripheral byte baseline, latest nested is still 65% larger (303 versus 184 KB). The pass also changes style transition duration, so before/after visible times are not a pure transport ablation. No further tuning sweep was performed.

## Correctness, artifacts, and reproduction

Full Arrow readback confirmed every original source value and overview column equals the assembled input. Sixteen city/periphery, zoom, and style comparisons have identical selected source IDs and byte-identical MVT output across decoders. Independent Python scans of the packed integer geometry confirm every sampled buffered-bbox membership, including 390 tile-boundary-crossing instances counted across scenarios. 33,520 decoded MVT height/class property checks match original source scalar values. Source transforms/coordinates were already fully validated in the preceding experiment. This validates the prototype's whole-feature bbox semantics; it does not establish equivalence with production tile-copy membership or PMTiles.

Evidence: [latest browser results](../benchmarks/overview-alternatives/browser-results.json), [first pass](../benchmarks/overview-alternatives/browser-results-unclustered.json), and [independent validation](../benchmarks/overview-alternatives/browser-validation.json). Range traces appear once per phase/variant, with timings retained for all runs. A screenshot remains at `/private/tmp/overview-alternatives/browser-preview.jpg`.

Run from the repository root, after generating the per-level fixture with the preceding report's commands:

```bash
.venv/bin/python benchmarks/overview-alternatives/build-browser.py
js/node_modules/.bin/esbuild js/experiments/overview-alternatives/worker.ts \
  --bundle --format=esm --outfile=/private/tmp/overview-alternatives/worker.js
js/node_modules/.bin/esbuild js/experiments/overview-alternatives/reader.ts \
  --bundle --format=esm --platform=node \
  --outfile=/private/tmp/overview-alternatives/reader.mjs
node benchmarks/overview-alternatives/browser-check.mjs \
  > /private/tmp/overview-alternatives/browser-check.json
.venv/bin/python benchmarks/overview-alternatives/validate-browser.py
node benchmarks/overview-alternatives/browser-serve.mjs
```

Open localhost:8103 and click **Run browser benchmark**. The page displays the completed JSON and saves it to the local scratch server, then:

```bash
.venv/bin/python benchmarks/overview-alternatives/summarize-browser.py
.venv/bin/ruff check benchmarks/overview-alternatives
js/node_modules/.bin/tsc --noEmit --target es2022 --module nodenext \
  --moduleResolution nodenext --skipLibCheck --allowJs false \
  js/experiments/overview-alternatives/reader.ts \
  js/experiments/overview-alternatives/worker.ts
```

Generated files and bundles stay in scratch; shared `demo/dist` is not rebuilt. The page uses the same MapLibre CDN version as existing demos. The bootstrap/index and worker are experimental, lack production versioning/resource-hardening, and cover two small windows with a fully loaded directory. No national rebuild, HTTP bandwidth simulation, production deployment, or PMTiles parity claim is included.
