# COGP checkout: packed overview access experiment

The local experiment establishes a substantial speedup while preserving COGP's
cumulative feature semantics and original analytical data. It does **not** yet
establish parity with the current geoparquet-lod implementation. Keep the current
demo and use this as evidence for an optional COGP performance extension.

The experiment is based on an actual checkout of
[Kanahiro/cloud-optimized-geoparquet](https://github.com/Kanahiro/cloud-optimized-geoparquet)
at commit `44f29be34d303fbb1febb154413a5aa7aa568a91` (the merge of the recent page-size
work, writer 2.2.2 / reader 2.1.4). The checkout is at
`/private/tmp/cogp-local-parity`, branch `codex/local-parity`, experiment commit `1f44d789ed197ec3f4da58719555d132e9d68d40`. The working demo's
preserved commit is `6c3a4f9`, on `codex/pmtiles-parity`. This report and a portable
patch are on `codex/cogp-checkout-parity`; no geoparquet-lod production reader, writer, compare
page, demo bundle, or served dataset was changed.

## What changed in the checkout

The Rust writer was built unchanged and used to generate a fresh Tokyo baseline.
The TypeScript reader was modified, and an experimental Python repacker replaces
its `quantized_geoarrow` overviews with an optional `packed_bbox_delta_v2` encoding:

1. One BINARY value per eligible source row/LoD contains exact source bbox values
   and losslessly delta-encoded quantized polygon coordinates. Geometry remains
   whole features. There is no tile clipping, feature dropping, or resimplification.
2. Bboxes exactly representable at seven decimal places use signed coordinate
   minima and unsigned extents. Other values retain the original four doubles.
   This is lossless encoding, not rounding or an approximate bbox predicate.
3. A compact directory directly indexes overview page envelopes across row groups.
   A 16 KiB bootstrap and spatial shards avoid loading the full footer, bbox
   columns, and separate X/Y columns before drawing. Country data uses a second
   directory tier. The directory does not alter source row order or level prefixes.
4. Real reader APIs accept source-bound metadata/page indexes and project sparse
   absolute row IDs. Original WKB and arbitrary attributes use COGP's existing
   Parquet decoder, without monkey-patching private reader methods.
5. The reader batches requests for 1 ms and can fill small gaps, bounded to 8 KiB,
   a 25% gap/data ratio, and 4 MiB runs. WKB intervals cannot be fetched as gaps.

This **requires a new overview decoder**. Existing COGP renderers cannot render
that encoding. Standard GeoParquet analytical readers still see the original
primary geometry and attributes. The current prototype supports MultiPolygon
and finite bbox values; line/point encoding support has not been implemented.
The existing COGP reader's other geometry support is unchanged.

## Country-scale measurements

53,742,978 Japan buildings, 210 row groups, original eight-level COGP file.
That country baseline was produced by COGP 2.2.1; the separate Tokyo control below
uses the freshly built 2.2.2 writer. All COGP variants retain identical source row
IDs, overview coordinates/topology, transforms, selected attributes, and MVT bytes.

Three runs, alternating variant order, six concurrent requests, 50 ms service
delay per request. Opening is included in cold numbers. Data and directory use
the same scheduler; directory traffic is counted. Units are decimal MB.

| Cold 3×3 viewport | Native COGP: ms / MB / requests | Modified COGP: ms / MB / requests | Current geoparquet-lod: ms / MB / requests |
| ----------------- | ------------------------------: | --------------------------------: | -----------------------------------------: |
| z8                |                341 / 1.720 / 10 |                   234 / 0.135 / 4 |                            286 / 1.340 / 6 |
| z11               |                472 / 2.124 / 31 |                   255 / 0.389 / 7 |                            303 / 2.130 / 5 |
| z14               |             1,649 / 4.650 / 167 |                  415 / 1.725 / 28 |                            228 / 1.170 / 6 |
| z16               |                982 / 2.720 / 91 |                  325 / 0.569 / 19 |                            162 / 0.060 / 5 |

At z14 this is approximately **4× faster, 63% fewer bytes, and 83% fewer
requests** than native COGP. At z16 it is 3× faster than native, but still
2× slower and transfers roughly 9.5× as much as our current implementation.
It is not network parity.

The current implementation is a contextual reference, **not matched content**.
Its display policy is substantially denser at coarse/city zooms. For example,
COGP selects 1,885 rows across the z8 tiles, while our MVTs contain 209,230 feature
instances. At z14 those counts are 31,361 and 69,156 respectively. COGP counts are
bbox-selected rows before clipping; reference counts are encoded MVT features;
both sums include tile-boundary duplication. They are not unique source counts.
The faster coarse COGP numbers must not be interpreted as equivalent visual work.

### Panning, styles, and the zoom route

| Japan workload                   | Native: ms / MB / requests | Modified: ms / MB / requests | Current: ms / MB / requests |
| -------------------------------- | -------------------------: | ---------------------------: | --------------------------: |
| z14 pan one tile east            |           666 / 0.867 / 46 |              205 / 0.287 / 8 |             111 / 0.476 / 2 |
| z16 pan one tile east            |           297 / 0.115 / 10 |              105 / 0.080 / 2 |              56 / 0.015 / 2 |
| Add height/subtype after z14 pan |           809 / 0.048 / 44 |             550 / 0.197 / 33 |             334 / 0.318 / 7 |
| Repeat styled z14 view           |                458 / 0 / 0 |                  190 / 0 / 0 |                  91 / 0 / 0 |
| Settled zoom route               |        2,472 / 4.463 / 185 |           1,476 / 2.582 / 77 |          1,038 / 2.316 / 15 |

The route is one Tokyo tile at zooms `8,10,11,12,13,14,15,16,14,11`, each allowed
to finish. Excluding opening, bytes are 2.940 MB native, 2.566 MB modified, and
2.299 MB current. Thus the sustained byte saving over native is about **13%**;
the larger total saving includes eliminating the 1.523 MB native footer.
Requests remain roughly five times our current route count.

Arbitrary attributes remain available, but directory leaves fetch indexes for
all columns in relevant row groups. That increases style-fetch bytes. Repeated
geometry/attribute decoding and per-query source metadata setup also leave CPU
costs that the current implementation's caches avoid. This is not a style-fetch
or warm-render parity result.

### Is the gain merely the directory?

No. An additional variant uses the directory/public APIs and 1 ms adjacent-only
batching while retaining native GeoArrow geometry. At Japan z14 it takes
1,578 ms / 3.140 MB / 152 requests; its route takes 2,419 ms / 2.925 MB / 150 requests.
The physical geometry/bbox access change is responsible for most of the new
latency improvement. This indexed control differs from the earlier indexed
experiment's broader bounded-gap transport, so its exact timing is not a repeat
of that earlier comparison.

## Fresh-writer control

4,882,169 Tokyo buildings from the same source used for our existing Tokyo
comparison file. The checked-out Rust writer used z8–17, 262,144-row groups,
1,024-row pages, 64 KiB page-size limit, and ZSTD level 9. The repacker retains the
resulting 23 row groups, level boundaries, row order, geometry, and attributes.
Its pages use the same row/byte limits; encoding/layout details can differ.

| Cold viewport | Native: ms / MB / requests | Indexed native: ms / MB / requests | Packed: ms / MB / requests |
| ------------- | -------------------------: | ---------------------------------: | -------------------------: |
| z8            |            279 / 0.347 / 9 |                    232 / 0.180 / 4 |            165 / 0.094 / 3 |
| z11           |            296 / 0.498 / 7 |                    251 / 0.330 / 4 |            187 / 0.215 / 3 |
| z14           |        1,108 / 2.338 / 104 |                   991 / 2.192 / 87 |           275 / 1.374 / 16 |
| z16           |           762 / 0.849 / 69 |                   737 / 0.695 / 54 |           248 / 0.503 / 16 |

The substantial speedup survives the current upstream writer improvements.
Our Tokyo reference at z14 takes 216 ms / 0.890 MB / 6 requests, with different
content. Its overviews end at z14, so its z16 measurements use exact primary WKB;
do not compare those as equivalent overview workloads. The full Japan reference
has z16 overviews and is the better street-scale reference.

## Storage and compatibility costs

| Dataset |     Native file |      Packed file | Packed directory |
| ------- | --------------: | ---------------: | ---------------: |
| Tokyo   |   832,613,467 B |  1,018,861,515 B |      1,289,059 B |
| Japan   | 9,694,703,869 B | 11,283,101,032 B |     13,988,366 B |

The packed country file is **16.4% larger**, plus its small directory. Repeating
bbox values across overview levels and loss of some native column compression
are real costs. The initial four-double bbox representation was worse: Tokyo
was 1,221,100,158 B, and its first z14 run fetched 2.200 MB instead of v2's 1.374 MB.
That initial one-run ablation used an earlier timing harness and is retained
only to explain the representation/byte tradeoff.

The directory remains a **sidecar prototype**, whereas our current format embeds
its bootstrap/index. Embedding without removing the standard footer is plausible
but not demonstrated by this experiment. The directory stores a footer fingerprint;
opening checks sizes only. Production would require binding it to the exact
immutable source, validation/resource limits, and an encoding/versioning decision.

## Validation and measurement limits

- The checkout's reader suite passes **136 tests** (130 upstream plus six added),
  including source metadata injection, sparse row projection, bounded gap/WKB
  protection, shared-LoD prefixes, multipart polygons/holes, renamed columns,
  and exact non-decimal bbox fallback.
- Whole-file Arrow comparison confirmed every original WKB value, source attribute
  (including nested columns), bbox value, row order, row count, group boundary,
  and level/quantization metadata matches for both datasets. Only the overview
  encoding declaration changes in `geo` metadata.
- Country integration verification passed 68 query comparisons across all levels,
  randomized Tokyo views, Osaka, Fukuoka, Sapporo, Hiroshima, Nagoya, empty areas,
  primary WKB, attribute-only/nested projections, limits, shared cancellation,
  retry, and wrong-size directory rejection. Native/modified geometry and MVT
  hashes also match throughout the benchmark.
- These are local Node measurements with simulated latency, not browser painting
  or HTTP benchmarks. File sizes are known equally; no HEAD charge, bandwidth
  cap, partial-body transfer, or CPU-worker pool is modeled. Correctness hashes
  and reference MVT feature counting run outside measured intervals.
- Native COGP uses 32 MiB compressed-range and 64 MiB parsed-index caches. Directory
  variants use 32 MiB compressed-range and 64 MiB estimated directory caches.
  The reference retains its existing implementation/caches; exact heap budgets
  and decoded-cache policies are not equalized. Three medians establish direction,
  not tail-latency confidence. No PMTiles benchmark was rerun for this task.

## Recommendation

There is a credible COGP extension path: preserve cumulative source semantics,
add compact spatial discovery, and allow a packed overview representation.
The observed performance gain does not require abandoning analytical columns
or whole-feature geometry. It does require reader/encoding changes and additional
storage, and it has not reached our request/byte efficiency.

For an upstream discussion, bring the reproducible experiment and propose small
metadata/index-provider and sparse-row-projection APIs first. Discuss the packed
encoding and embedded discovery as optional extensions backed by these ablations.
Do not present the experiment as drop-in `quantized_geoarrow` compatibility or
claim PMTiles/current-map parity. The next performance targets are street-scale
page overfetch and projection-specific directory/decoded caching.

## Reproduce and artifacts

The portable checkout patch is `benchmarks/cogp-checkout/cogp-local-parity.patch.gz`.
Apply it to upstream commit `44f29be34d303fbb1febb154413a5aa7aa568a91` with
`gzip -dc /path/cogp-local-parity.patch.gz | git apply --binary`; it includes source changes, small fixtures, tests, the
repacker, directory builder, benchmark, and a README with commands. The dataset
files remain local under `/private/tmp`; they are not checked into this repo.

Raw results: `benchmarks/cogp-checkout/japan.json` and `tokyo.json`. Earlier ablation:
`initial-inline-bbox.json`. Exact data paths, latency, concurrency, and gap budget
are recorded in each final result's `config`. The implementation reference is
geoparquet-lod commit `6c3a4f9`.
