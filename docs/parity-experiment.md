# Spatial page parity experiment

Measured 2026-10-12 on the same 53,742,978-row Japan buildings file and
PMTiles archive used in the main benchmarks. This branch preserves every
analytical and packed overview value, row order, feature id, and row group.
It changes the physical pages and how the map finds them. No prebuilt MVT
payloads or additional geometry copies are added.

## What changed

The optimizer aligns LOD pages with their finest tile grid, places chunks of
each LOD together, and builds a spatial directory over those pages. Exact
row fences replace key-page reads. Empty LOD pages are omitted from the
spatial directory. A delta-coded root fits into the first 16 KiB; larger
directories can gain another level. Source-row inspection and full-detail
geometry still use the ordinary Parquet footer, loaded lazily. Compressed
pages and pending fetches are shared between neighbouring tile requests.

On Japan the file grows from 7.623 GB to 7.846 GB (2.9%). The compressed
spatial directory is 39.2 MB; the other growth is chiefly smaller compression
units and their standard page indexes. The root is about 12 KB, padded into
a 16 KiB bootstrap. The original LOD representation itself is preserved.

## Range reads

These are local file range reads instrumented at the byte-source boundary,
with the same coalescer used by the browser. Background index prefetch is
included in the original reader's counts. Opening is measured separately;
each cold tile starts with a fresh reader. KB and GB are decimal here.

| Reader        | Open bytes | Open requests |
| ------------- | ---------: | ------------: |
| Original LOD  |   470.9 KB |             6 |
| Spatial index |    16.4 KB |             1 |
| PMTiles       |    16.4 KB |             1 |

| Tile              | Original bytes / requests | Spatial bytes / requests | PMTiles bytes / requests | Spatial / PMTiles features |
| ----------------- | ------------------------- | ------------------------ | ------------------------ | -------------------------: |
| z8 Tokyo metro    | 729.6 KB / 11             | 659.0 KB / 3             | 499.4 KB / 2             |           115,775 / 95,774 |
| z11 Tokyo city    | 547.2 KB / 18             | 429.9 KB / 4             | 306.2 KB / 2             |            28,201 / 24,373 |
| z13 Shibuya       | 538.2 KB / 13             | 405.7 KB / 4             | 349.8 KB / 2             |            29,834 / 28,908 |
| z13 Chiba         | 239.8 KB / 13             | 111.5 KB / 4             | 60.3 KB / 2              |              4,955 / 4,929 |
| z15 Tokyo station | 236.2 KB / 14             | 61.9 KB / 4              | 32.4 KB / 2              |                229 / 1,441 |
| z15 Osaka         | 245.3 KB / 13             | 90.7 KB / 4              | 111.8 KB / 2             |              1,357 / 6,523 |

Counts matter: the overview thinning is unchanged, and PMTiles overzooms its
z14 parent at z15, so street comparisons do not contain the same features.
The spatial reader's exact row membership also removes some incidental
border features/duplicates admitted by the old reader's page overfetch.
Independent full-table membership tests verify this rather than treating
the old reader's extra features as the reference.

The next tile east, using the same reader (PMTiles advances one tile at its
native zoom, which is z14 for the two street cases):

| Tile          | Original bytes / requests | Spatial bytes / requests | PMTiles bytes / requests |
| ------------- | ------------------------- | ------------------------ | ------------------------ |
| Tokyo metro   | 77.9 KB / 8               | 15.4 KB / 2              | 15.6 KB / 1              |
| Tokyo city    | 243.0 KB / 4              | 266.7 KB / 4             | 166.2 KB / 1             |
| Shibuya       | 323.0 KB / 5              | 267.0 KB / 2             | 257.7 KB / 1             |
| Chiba         | 122.6 KB / 5              | 93.2 KB / 2              | 82.9 KB / 1              |
| Tokyo station | 57.6 KB / 4               | 25.7 KB / 2              | 105.7 KB / 1             |
| Osaka         | 119.0 KB / 4              | 44.4 KB / 2              | 125.8 KB / 1             |

## Latency experiment

Median of three fresh-reader runs with **50 ms added to each range read**,
without bandwidth throttling. These are a round-trip simulation, not measured
S3 or CDN timings. Time includes reading, decompression, and the LOD reader's
MVT generation. PMTiles already contains MVT. MapLibre rendering is excluded.
Open is excluded from the tile times in this table.

| Tile          | Original ms | Spatial ms | PMTiles ms |
| ------------- | ----------: | ---------: | ---------: |
| Tokyo metro   |         291 |        274 |        109 |
| Tokyo city    |         202 |        174 |        110 |
| Shibuya       |         211 |        163 |        110 |
| Chiba         |         174 |        117 |        106 |
| Tokyo station |         164 |        109 |        106 |
| Osaka         |         167 |        112 |        108 |

The two prefix/main data reads can run concurrently, so four requests can
still take two network round trips. Street latency approaches the archive's;
large tiles still pay for reconstructing geometry and encoding MVT. The z11
pan crosses a directory boundary and remains slower than the original pan
in this sample. This is substantial progress, not full PMTiles parity.

A smaller directory leaf (256 pages) was also tested. It reduced cold reads
to 369 KB in Shibuya, 73 KB in Chiba, and 25 KB at Tokyo station, but required
an extra directory level and 5–6 requests. The 1024-page default prefers fewer
dependent round trips. The benchmark should guide this tradeoff for other
datasets rather than assuming one setting is universally best.

## Validation

Every column of all 53,742,978 rows was compared between the original and
optimized Japan file with PyArrow, including all LOD bytes. They agree.
DuckDB and PyArrow also read the synthetic optimized fixtures as ordinary
GeoParquet with identical analytical values. The original JS reader can read
relocated/re-encoded pages through the standard footer and page indexes.

Tests independently reconstruct expected overview membership from full
columns, including boundary copies, attributes, strings, null/empty geometry,
points, lines, polygons, row-group crossings, empty tiles, exact-geometry
fallback, and source-row lookups. HTTP tests cover the one-request bootstrap,
CORS sizing fallback, truncation, cancellation, and legacy files.

The optimizer currently holds the spatial directory and standard indexes in
memory plus one row group's LOD values; keys are staged on disk. Very large
page counts can therefore make the build memory intensive. General analytical
rewrites discard the custom index and require re-optimization. Other Parquet
engines, actual remote network performance, and broader datasets still need
validation.

## Reproduce

```bash
uv run geoparquet-lod optimize japan_buildings.lod14.parquet japan.spatial.parquet
cd js
npm run build
RUNS=3 node test/parity.mjs ../japan_buildings.lod14.parquet \
  ../japan.spatial.parquet ../japan_buildings_z0.pmtiles 50
```

Use `--keep-pages` to isolate directory/clustering improvements,
`--leaf-pages 256` to try smaller directory reads, and a latency of `0` for
local I/O plus CPU. The benchmark requires local paths to the same Japan
fixtures; it never downloads data. Raw counts and all timing samples are in
[parity-benchmark.json](parity-benchmark.json).

These measurements describe the initial `b57e6f5` experiment. The
[single-order and rendering follow-up](parity-followup.md) tests removing the
prefix/main split, reducing MVT conversion cost, and complete viewport pans.
It reaches request-count parity in the six individual-tile samples, with
remaining coarse-byte and CPU tradeoffs.
