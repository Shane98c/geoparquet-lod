# Standard geometry encoding ablation

The compact quantized coordinates are buying substantial storage and decode savings. On matched samples, standard native float64 multipolygon geometry uses **2.16× the compressed geometry bytes** and WKB **2.60×**, while each takes about **2.1× the geometry decoding CPU**. Actual MVT encoding costs are essentially unchanged. WKB gives a concrete interoperability benefit: installed DuckDB spatial automatically recognizes it as geometry and runs spatial SQL without our decoder.

This is an encoding experiment, not a whole-route/browser performance result or a projected national file size.

## Matched construction

Source: `/private/tmp/overview-japan-compact-1024/japan.compact.parquet`.

526 physical row groups, 542,728 rows and 3,026,383 coordinate pairs. Selection includes every group whose geometry chunk intersects a fetched range in run zero of the previous compact-reader browser route, plus up to 16 evenly spaced groups in each stored level. The route-touched set includes range-coalescing overfetch; it is not an exact list of rendered features. Groups retain original row ordering, grouping, selected features, level, source_row, height and class. All variants use Zstd level 9, no dictionary, standard statistics and the same page settings. No source or prior prototype was changed.

For every geometry, integer coordinates become longitude/latitude via the existing level's scale/offset. Standard variants retain those exact double values. They do **not** restore original source detail. Thus the experiment measures representation of already-quantized geometry, not additional simplification or precision loss.

The standard files carry GeoParquet 1.1 `geo` metadata with encoding `multipolygon` or `WKB`, geometry_types `["MultiPolygon"]` and the measured sample bbox. CRS is omitted, correctly invoking OGC:CRS84. Native coordinates are DOUBLE separated x/y, with the original three LIST levels and optional children; children contain no nulls. WKB uses binary BYTE_ARRAY. Quantized retains its private scale/offset metadata.

[GeoParquet 1.1 specification](https://geoparquet.org/releases/v1.1.0/) requires DOUBLE native coordinates, permits optional children when actual values are non-null, and recommends WKB for portability. Metadata was manually checked against its [official JSON schema](https://geoparquet.org/releases/v1.1.0/schema.json); an automated JSON-schema validator was unavailable locally. Actual geometry/schema/data checks below are separate from metadata validation.

## Bytes

Decimal MB, compressed geometry chunks only:

| Stored zoom          | Quantized int32 delta | Native float64 plain |           WKB plain |
| -------------------- | --------------------: | -------------------: | ------------------: |
| 4                    |                0.0313 |       0.0471 (1.50×) |      0.0958 (3.06×) |
| 8                    |                0.9795 |       1.5504 (1.58×) |      2.4148 (2.47×) |
| 11                   |                1.7660 |       3.5923 (2.03×) |      4.3470 (2.46×) |
| 14                   |                2.1328 |       5.1857 (2.43×) |      5.7367 (2.69×) |
| 16                   |                0.3504 |       1.0071 (2.87×) |      1.0985 (3.14×) |
| Combined sample      |            **5.2600** |  **11.3825 (2.16×)** | **13.6928 (2.60×)** |
| Route-touched subset |            **4.5814** |   **9.6782 (2.11×)** | **11.7502 (2.56×)** |

Whole sampled files, including identical auxiliary columns and each representation's footer/metadata: quantized 6.441 MB; native plain 12.575 MB; WKB plain 15.012 MB. These ratios differ from geometry-only ratios and must not be extrapolated to a national total without accounting for spatial/level weighting.

We also tried native BYTE_STREAM_SPLIT (24.220 MB geometry, 25.413 MB file) and WKB DELTA_LENGTH_BYTE_ARRAY (14.146 MB geometry, 15.465 MB file). Both were worse than PLAIN here. This does not establish their behavior on different datasets/coordinate distributions or compression configurations.

## CPU and rendering

Local Node v24.19.0, one CPU execution stream, compressed geometry chunks already loaded into memory. Seven alternating-order iterations; first discarded, median of six reported. Attributes excluded equally. Nested int32 and DOUBLE leaves decode directly into typed coordinate buffers using Parquet repetition levels. WKB bytes decode with a direct float64 scan. Standard geometry remains float64 through MVT encoding using identity scale/offset; no private grid metadata is required by that decoder.

| Variant                  | Geometry decode + assembly | MVT encoding |
| ------------------------ | -------------------------: | -----------: |
| Quantized                |                **73.6 ms** |     215.3 ms |
| Native float64 PLAIN     |                   150.9 ms |     213.6 ms |
| Native BYTE_STREAM_SPLIT |                   191.0 ms |     212.7 ms |
| WKB PLAIN                |                   156.3 ms |     214.5 ms |
| WKB delta length         |                   157.3 ms |     212.4 ms |

MVT work uses one tile centered on each group's bbox at its stored zoom. This is a synthetic decoder/render workload, not the compare-view route. File I/O is outside timing, network is absent, and no browser/MapLibre frame timing is measured. The DOUBLE decoder is an isolated extension of the direct coordinate path, not a claim that every generic native reader has this speed. Decode combines decompression, Parquet value decoding and geometry assembly; it does not separately time those substeps.

Validation outside measured timing compared **all transformed x/y coordinates, all geometry/polygon/ring offsets, and each individual MVT byte buffer** across all five representations. All matched exactly. Every written sampled table also passed a full Arrow readback equality check; WKB coordinates passed independent Shapely round trips.

## Actual user-facing interoperability

Installed DuckDB **1.5.6**, with `LOAD spatial`:

- WKB files automatically expose `geometry` as GEOMETRY. `ST_NPoints`, `ST_Area`, `ST_XMin` and `ST_XMax` run directly against `read_parquet`, without our private decoder or an explicit WKB conversion. Results match independent Shapely: 542,728 geometries, 3,026,383 points, summed area 0.9696488928904143 square degrees, x extent 122.9402835–153.9841198302161.
- Native multipolygon files expose nested DOUBLE structs in this DuckDB version; those same spatial calls fail on that type. The encoding is in GeoParquet 1.1, but that does not guarantee support by a particular reader. No second native spatial reader was installed; generic Arrow readability is not counted as successful spatial interoperability.
- Quantized geometry similarly requires custom conversion before spatial operations.

Area is only a reproducible planar correctness check, not a useful physical area estimate or a guarantee that all generalized geometries are valid. These files remain overview representations with level-dependent selection/simplification; standard encoding does not change that semantic limitation. Companion-file packaging and source-row link stability are unchanged.

## Interpretation and next choice

Special coordinates save roughly half the geometry transfer versus native float64 and more than half versus WKB in this sample. WKB's spatial interoperability is real, but replacing current display coordinates with WKB would likely widen the existing bandwidth gap unless access improvements compensate; actual route performance requires a separately built/indexed standard file and browser test.

A plausible design separates the interoperability contract from optional acceleration: standard geometry as the authoritative overview representation, with an optional compact display accelerator when the storage cost is acceptable. That would duplicate geometry and is not yet tested. Alternatively, retain standard source GeoParquet plus explicitly specialized display overviews and describe the latter honestly. This experiment does not decide the community's preferred semantics or single-file design.

## Reproduction and artifacts

- Builder: `benchmarks/overview-alternatives/standard-geometry-ablation.py`.
- Isolated direct decoder: `js/experiments/overview-alternatives/standard-coordinates.ts`.
- CPU/MVT checker: `js/experiments/overview-alternatives/standard-geometry-bench.ts`.
- Evidence: `benchmarks/overview-alternatives/standard-geometry-results.json` and `standard-geometry-cpu-results.json`.
- All five readable sampled Parquet files and compiled bench: `/private/tmp/overview-standard-geometry/`.

```sh
.venv/bin/python benchmarks/overview-alternatives/standard-geometry-ablation.py
js/node_modules/.bin/esbuild js/experiments/overview-alternatives/standard-geometry-bench.ts --bundle --platform=node --format=esm --outfile=/private/tmp/overview-standard-geometry/bench.mjs
node /private/tmp/overview-standard-geometry/bench.mjs
```
