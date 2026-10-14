# Ordinary overview geometry: bounded encoding/access experiment

**Quantized nested int32 coordinates with standard Parquet delta encoding merit a browser experiment.** They approach packed transfer at the dense level in this fixture, while WKB provides ordinary spatial SQL access at a larger byte cost. Neither result establishes production parity or PMTiles parity. The [browser follow-up](overview-browser-prototype.md) now tests nested decoding, an embedded compact index, and complete source/overview files; the measurements below remain the earlier independent per-level ablation.

This is an encoding/access ablation, not a replay of Japan's tuned display profile. It uses 91,067 buildings from two Tokyo windows: city `[139.72,35.64,139.77,35.69]` and periphery `[139.47,35.68,139.52,35.73]`. The existing Tokyo source subset supplies exact source WKB, IDs, height, and class. A fresh reference uses contiguous z8–16, `drop_rate(2)`, complete z16, and unchanged height/class. No full Japan rebuild or production reader/writer change was made.

## Controls and semantics

All alternatives have identical selected source rows, quantized geometry, Hilbert row order, and source spans. Each span ends at a tile boundary or 2,048 source rows. Each column/span is one independently compressed Parquet page in one row group; Zstd level 9, dictionaries disabled. These are experimental per-level tables, not complete replacement production files.

- **Packed own:** original packed own geometry and carried height/class, with hosted boundary copies removed. Original scalar height/class remain independently accessible in the table, as they do in the reference.
- **WKB:** one nullable, CRS-valued whole MultiPolygon WKB per source row, with ordinary scalar source height/class. GeoParquet metadata advertises this geometry, so DuckDB recognizes it directly.
- **Nested:** nullable `list<list<list<struct<x:int32,y:int32>>>>`, retaining quantized coordinates and an explicit scale/offset. Plain integer pages provide a control; **nested delta** changes just X/Y to standard Parquet `DELTA_BINARY_PACKED` before Zstd. This is COGP-like physical storage with experiment transform metadata, **not standard CRS-valued GeoArrow compatibility**.

Scalar attributes align with original source rows, including rows absent from a coarse overview. No nullable attribute LIST/copy-slot scheme is used. Height/class equality is established for this thinning policy; source scalar columns would not represent arbitrary thinning overrides. The previous [attribute separation experiment](parity-separate.md) tested different semantics and storage.

Spatial discovery is a gzip directory of every page's quantized-geometry bbox envelope. Querying reads every overlapping page and filters decoded feature bboxes. Geometry remains whole and unclipped. This removes tile-specific boundary copies, while retaining spatial page discovery. It changes access semantics/layout relative to the production tile-copy path; encoding comparisons are **within this shared no-copy layout**. Source row IDs are inferred from directory spans; their physical column is not fetched during viewport queries.

## Compressed storage

Overview bytes only; packed includes carried attributes, while WKB/nested geometry excludes them. All values are decimal KB.

| Zoom | Selected rows | Packed own |     WKB | Nested plain | Nested delta | Source scalar height/class | Directory |
| ---: | ------------: | ---------: | ------: | -----------: | -----------: | -------------------------: | --------: |
|    8 |           343 |        5.8 |    13.6 |         13.5 |         12.5 |                       33.9 |       1.4 |
|   11 |         2,894 |       28.2 |    84.9 |         64.1 |         57.1 |                       34.0 |       1.6 |
|   14 |        22,855 |      297.5 |   818.3 |        509.5 |        394.2 |                       35.4 |       2.0 |
|   16 |        91,067 |    1,488.8 | 3,983.5 |      1,994.2 |      1,373.6 |                       56.5 |       8.5 |

Nested delta is 8% smaller than packed's geometry-plus-attributes component at z16, but 33% larger at z14 and over twice as large at sparse z8. WKB geometry is 2.37–3.01× packed overview storage. Changing integer encoding materially changes the answer; comparing packed only against WKB would overstate the necessity of a custom geometry codec.

Every per-level file also stores a source-row-ID column (96–109 KB) and a full footer. Footer bytes for packed/WKB/nested delta are respectively 11.6/11.9/19.5 KB at z8 and 57.7/58.9/96.0 KB at z16. At z16 the complete experimental tables are 1.712/4.208/1.635 MB; these sizes exclude primary source WKB and other source columns. See `sizes` in the JSON for every component and plain-nested result. They cannot be compared directly with the 11.273 MB reference file, which contains original WKB and all nine overview levels.

With the same page spans, retaining the reference's hosted copies increases packed overview bytes from 297,476 to 326,824 at z14 and 1,488,791 to 1,712,047 at z16. Removing copies saves 9% and 13% respectively in this fixture. These savings require bbox discovery/filtering; they are not an encoding-only gain.

## Actual compressed-range transfer

Cold totals include the exact full footer, its 8-byte trailer, the whole bbox directory, and selected compressed chunks. The harness performs real local byte-range reads, retaining their offsets and lengths. Adjacent requested chunks are merged without filling gaps. Payloads are decoded/filtered for packed/WKB; nested controls fetch the same selected compressed ranges and validate full-file roundtrips separately, without a decoder timer.

Views are one tile at z8/z11 and 3×3 tiles at z14/z16, centered on each window. The two z8 views happen to cover the same source subset; they are not independent coarse-content samples. Values are decimal KB, geometry-only → styled height/class. Request counts include footer and directory.

| Zoom / view    | Features |        Packed |           WKB |  Nested delta | Requests, all variants |
| -------------- | -------: | ------------: | ------------: | ------------: | ---------------------: |
| 8 / both       |      343 |   18.7 → 18.7 |   27.0 → 60.8 |   33.5 → 67.3 |                     48 |
| 11 / city      |      645 |   20.3 → 20.3 |   34.4 → 40.6 |   35.3 → 41.5 |                     14 |
| 11 / periphery |    1,990 |   32.3 → 32.3 |   69.6 → 93.9 |   60.4 → 84.7 |                     34 |
| 14 / city      |    7,281 | 119.8 → 119.8 | 313.1 → 323.6 | 160.3 → 170.8 |                     24 |
| 14 / periphery |   14,881 | 210.5 → 210.5 | 539.0 → 563.9 | 286.3 → 311.3 |                     38 |
| 16 / city      |    2,339 | 215.7 → 215.7 | 480.6 → 486.3 | 238.6 → 244.2 |                     28 |
| 16 / periphery |    4,618 | 314.0 → 314.0 | 709.9 → 719.7 | 328.0 → 337.9 |                     27 |

Styled nested delta adds 13%/8% at z16, 43%/48% at z14, roughly 2.0–2.6× at z11, and 3.6× at z8. Sparse source-aligned scalar columns pay for values of non-displayed rows within fetched spans. Packed carries only displayed values.

The dense z16 city breakdown is packed **149.5 geometry/carried + 57.7 footer + 8.5 directory KB**, versus nested delta **134.1 geometry + 5.7 source scalars + 96.0 footer + 8.5 directory KB**. Its larger full footer causes most of the remaining dense-level penalty. The index/metadata strategy matters as much as geometry bytes here.

Without adjacent batching, styled WKB takes 138 requests at z8 and 78/75 at the two z16 views; nested geometry also has separate X/Y requests. Adjacent batching brings all variants to the same counts. Ordinary columns do not inherently require more requests when their requested chunks are contiguous. This layout uses small row groups and a full per-level footer, so these counts are not the production bootstrap/page-directory counts.

## Validation and analytical access

All 91,067 source-row memberships and quantized geometries were decoded. Every hosted boundary copy at the four measured levels matches its own geometry and original height/class: 0, 14, 1,850, and 10,545 copies. Nested Arrow roundtrips match every null and integer coordinate, plus both scalar source columns. Every sampled packed/WKB bbox query matches an independent exhaustive scan of canonical quantized geometry bboxes, with matching source properties. Nested has full-file coordinate equality and identical bbox range selection; it has no per-query geometry decoder in this harness. The z14/z16 views include 49–81 geometries crossing central tile boundaries. This validates bbox-overlap membership, not exact geometry intersection or a production MVT comparison.

Ordinary DuckDB spatial SQL on WKB works without a custom geometry decoder:

```sql
SELECT count(overview), sum(ST_NPoints(overview)), sum(ST_Area(overview)),
       avg(height) FILTER (WHERE overview IS NOT NULL)
FROM read_parquet('/private/tmp/overview-alternatives/f16.wkb.parquet');
```

GeoParquet metadata makes `overview` a geometry automatically; `ST_GeomFromWKB` is unnecessary. The four aggregate queries took roughly 2–4 ms locally and matched selected counts/vertex counts. Areas are in squared longitude/latitude degrees, not physical square meters. Nested columns are ordinary SQL-accessible lists/structs, but need the recorded transform and a geometry constructor for spatial SQL.

Packed/WKB payload query timing uses three runs, with footer/index setup excluded from the timer but included in transfer totals. At z16 styled city/periphery, medians were 77/138 ms packed and 7/13 ms WKB. **These measure the CPython validation decoder and individual Shapely construction versus Shapely's vectorized C WKB decoder, not production JS codec speed.** They establish analytical usability, not a rendering CPU winner. No network delay/bandwidth or drawing is modeled. Warm repeats transferred zero bytes using an unbounded compressed cache in this small fixture; decoded geometry is rebuilt, so this is a reuse check, not a country-scale memory claim. Nested has no measured decode timing.

## Recommendation and reproduction

Keep spatial ordering, bounded spatial discovery, page locality, and adjacent request batching as independent mechanisms. Packed geometry plus carried attributes remains compelling for sparse overviews. Source-aligned WKB gives the strongest ordinary geometry compatibility here at materially higher transfer. Quantized nested int32 with standard delta pages is the next useful browser test, especially for dense levels; this experiment does not establish that custom geometry packing is required there. Removing boundary copies appears feasible with bbox page envelopes, but requires a production bbox traversal and explicit display-override semantics.

The current production file uses an embedded compact bootstrap/directory and larger analytical row groups. This fixture uses a fully loaded per-level bbox directory and standard footer, and resets scalar storage per level. It does not test country-scale directory traversal, nested browser decoding, production caching, HTTP behavior, MVT parity, or current Japan/PMTiles parity.

```bash
.venv/bin/python benchmarks/overview-alternatives/build.py \
  --source /private/tmp/lod-separate/source.parquet \
  --out /private/tmp/overview-alternatives
.venv/bin/python benchmarks/overview-alternatives/measure.py
.venv/bin/python benchmarks/overview-alternatives/nested.py
.venv/bin/python benchmarks/overview-alternatives/summarize.py
.venv/bin/ruff check benchmarks/overview-alternatives
```

Run from the repository root. DuckDB's spatial extension must already be available. `build.py` accepts source/output overrides; `nested.py` currently uses the default scratch/output paths. Generated Parquet files remain in scratch. Compact [results.json](../benchmarks/overview-alternatives/results.json) retains component sizes, validations, aggregate results, medians, and one actual range trace per scenario; no threefold duplicate traces are retained.
