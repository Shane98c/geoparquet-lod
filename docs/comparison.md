# Comparison with other formats

Four readers against the archives behind the [hosted demo](hosted-comparison.md),
all derived from the same 53,742,978 Japan building footprints:

- **GeoParquet LOD**: this layout, read through its spatial index.
- **COGP 2.2.2**: a fresh conversion with the released writer
  (`--webmerc-minzoom 10`, otherwise defaults), read with `@cogp/reader` 2.1.4
  the way its MapLibre adapter does.
- **PMTiles**: tippecanoe 2.82 with
  `-zg -Z 0 --drop-smallest-as-needed --extend-zooms-if-still-dropping`.
- **Esri SOP**: Esri's hosted Spatially Optimized Parquet file, read with the
  reference MapLibre reader from its repository, which queries the tile extent
  at the tile's resolution and returns GeoJSON.

Each reader opens once; every tile is then read on a fresh reader, geometry
only, over the public internet from the same R2 bucket. Bytes are response
bodies in decimal megabytes; "next tile" is the tile one column east on the
same reader. Elapsed times are not reported because one run from one laptop
is not a CDN benchmark.

| Reader         | Open           |
| -------------- | -------------- |
| GeoParquet LOD | 0.02 MB, 1 req |
| COGP           | 1.50 MB, 3 req |
| PMTiles        | 0.02 MB, 1 req |
| Esri SOP       | 2.62 MB, 3 req |

| Tile                  | GeoParquet LOD                                 | COGP                            | PMTiles                                       | Esri SOP                             |
| --------------------- | ---------------------------------------------- | ------------------------------- | --------------------------------------------- | ------------------------------------ |
| z8 metro, Tokyo       | 0.73 MB, 2 req, 115,775 feat.; next 0.02 MB, 1 | 0.12 MB, 8 req, level 0, 1,152  | 0.50 MB, 2 req, 95,774 feat.; next 0.02 MB, 1 | 26.65 MB, 1,048 req, level 8, 28,000 |
| z11 city, Tokyo       | 0.36 MB, 2 req, 28,201; next 0.19 MB, 1        | 0.25 MB, 17 req, level 1, 860   | 0.31 MB, 2 req, 24,373; next 0.17 MB, 1       | 5.39 MB, 143 req, level 12, 177,679  |
| z13 downtown, Shibuya | 0.36 MB, 2 req, 29,834; next 0.26 MB, 1        | 0.68 MB, 42 req, level 3, 2,739 | 0.35 MB, 2 req, 28,908; next 0.26 MB, 1       | 2.61 MB, 92 req, level 14, 51,996    |
| z13 suburbs, Chiba    | 0.08 MB, 2 req, 4,955; next 0.09 MB, 1         | 0.44 MB, 34 req, level 3, 454   | 0.06 MB, 2 req, 4,929; next 0.08 MB, 1        | 1.30 MB, 55 req, level 14, 6,841     |
| z15 street, Tokyo     | 0.04 MB, 2 req, 229; next 0.02 MB, 1           | 0.86 MB, 82 req, level 5, 213   | 0.03 MB, 2 req, 1,441; next 0.11 MB, 1        | 1.30 MB, 62 req, level 16, 229       |
| z15 street, Osaka     | 0.07 MB, 2 req, 1,357; next 0.04 MB, 1         | 0.80 MB, 80 req, level 5, 1,287 | 0.11 MB, 2 req, 6,523; next 0.13 MB, 1        | 1.01 MB, 57 req, level 16, 1,362     |

## Reading the table

The four files do not agree on what a tile should contain, so bytes only mean
something next to the feature counts.

- COGP keeps very few features at coarse levels in this conversion: its writer
  assigns each feature to the coarsest level where it is visible and defers
  features until their simplified geometry survives. Reading a fine level means
  reading every coarser level's row groups that touch the view, so requests
  grow with the level count.
- PMTiles stops at z14 and serves the overzoomed parent at z15, which is why
  its street tiles hold more features than the others.
- Esri's reader returns every feature in the extent at the chosen level's
  resolution and does not drop features. It reads many pages per view and has
  a feature limit at coarse zooms.
- This layout's hosted file shows about 20% more features than PMTiles at z8
  because its thinning budgets were not calibrated. The
  [Japan evaluation](japan-evaluation.md) calibrates them to within 3% of
  PMTiles counts; that build is reproducible from the repository but is not
  the hosted file.

Counts and visual coverage both matter: different thinning, tile buffers, and
overzoom rules can make smaller responses do less work. These single-tile reads
do not establish whole-session or cross-device parity.

## What each format stores

| Format         | Row layout                                      | Which rows at coarse zooms                                         | Overview geometry                                                                           | How a tile is found                                           |
| -------------- | ----------------------------------------------- | ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------- | ------------------------------------------------------------- |
| GeoParquet LOD | One Hilbert order; every feature once           | Per-zoom thinning, need not nest; recorded in nullable LOD columns | Packed varint rows on a quantized grid per LOD, with carried attributes and boundary copies | Spatial directory in the first 16 KiB over tile-aligned pages |
| COGP           | Coarse-to-fine row-group prefixes; feature once | Cumulative: a level adds row groups                                | Optional `quantized_geoarrow` nested int32 column                                           | Row-group prefix, then bbox statistics and page index         |
| Esri SOP       | XZ-code clustering; feature once                | All features at every level                                        | Per-level protobuf columns, Douglas-Peucker simplified                                      | Page index predicate skipping on the cluster code             |
| tylertoo       | `level` column with simplified WKB rows         | Per-level selection, written once per level in duplicating mode    | WKB                                                                                         | Level column and bbox                                         |
| PMTiles        | Pre-cut MVT tiles                               | tippecanoe's per-zoom dropping                                     | MVT                                                                                         | Directory lookup                                              |

The [specification](../spec/SPEC.md#relation-to-other-proposals) discusses the
design differences and where this layout could fold into those proposals.

## Reproducing

Bundle the Esri reference reader for Node once, then point the harness at the
hosted release:

```sh
cd js && npm ci && npm run build
npx esbuild vendor/esri-sop/index.ts --bundle --platform=node --format=esm --outfile=test/esri-node.mjs
R=https://pub-a9c45d804ff74bd19e8f9b7a52d9a90b.r2.dev/geoparquet-lod/releases/6c3a4f9/
BASE=$R LOD=${R}japan_buildings.parquet COGP=${R}japan_buildings.cogp-2.2.2.parquet \
  PMTILES=japan_buildings.pmtiles node test/compare4.mjs
```

`SKIP=cogp,esri` leaves readers out. The archives, their sizes and checksums are
listed in the [hosted comparison notes](hosted-comparison.md#data-provenance).
