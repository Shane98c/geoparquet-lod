# geoparquet-lod

One GeoParquet file that is the dataset for analysis and also serves a web
map at tile cost. Every feature is stored once with its exact geometry; a
small set of extra columns carry what a renderer needs at each zoom; a plain
reader ignores them.

- `python/`: writer, validator and CLI (`geoparquet-lod write | validate | inspect`)
- `js/`: a browser reader and a MapLibre tile protocol
- `spec/`: the layout and metadata contract
- `examples/`: thinning functions for common datasets

## How it works

Rows are sorted once along a Hilbert curve in large row groups, so a city
tile touches one or two of them. The rows shown at the coarsest zooms come
first in their own order, so a view of the whole dataset reads one row group.
A float32 bbox covering with page statistics lets a reader pick pages without
reading data. One nullable struct per LOD, a zoom or a run of zooms, holds for
exactly the rows shown there the geometry on that LOD's grid and the carried
attributes as ordinary typed columns.

Which rows appear at which zoom is a _thinning function_, one shape:

```python
def thin(rows: pa.Table, zoom: int, ctx: Context) -> pa.Table
```

The writer calls it once per zoom from fine to coarse, starting from the
complete zoom where every feature is present, each time with the rows kept
at the zoom just finer, and it returns the subset to keep. Nesting is
guaranteed by construction. Shapes stay shapes at every zoom on a
quarter-pixel grid; one too small for it is stored as a half-pixel square,
so zoomed out a thinned building set reads as a faint density texture in the
same fill layer, as tippecanoe draws it. `rows`
carries `id`, centroid, bbox diagonal, area, a per-row uniform `u` hashed
from the geometry, and the carried attributes; returning a carried column
overrides its value in that zoom's LOD, which is how a cluster passes its
count along. Built-ins are a few lines each and meant to be copied:

| thinning                        | rule                                                                                                                         | good for                                           |
| ------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------- |
| `drop_rate(2.5)`                | each zoom coarser keeps 1/rate, uniformly, so relative density survives (tippecanoe `-r`); the default                       | points, small polygons                             |
| `size_threshold(4)`             | keep a feature while it still spans 4 pixels                                                                                 | large polygons and lines first                     |
| `tiny_polygons(0.5)`            | inspired by tippecanoe's tiny-polygon reduction: small areas accumulate and one half-pixel square is kept per square of area | area-true coverage; nearly empty at national scale |
| `importance('population', 300)` | keep the top N per tile by a column                                                                                          | named places, ranked facilities                    |
| `cluster(8, count='count')`     | one representative per 8 px cell carrying how many it stands for                                                             | dense points                                       |
| `density_budget(60_000)`        | cap the busiest tile; flattens dense places                                                                                  | hard byte ceilings                                 |
| `only('risk > 0', inner)`       | restrict eligibility before `inner`                                                                                          | partial datasets                                   |
| `combine(a, b)`                 | keep what any part keeps                                                                                                     | size plus sample, etc.                             |

There are no other knobs. Anything the built-ins cannot express is a short
function of a table.

Zooms are derived from the data unless given: the coarsest zoom is where the
dataset's extent fits a tile, every feature is present from the zoom where
the median feature spans a pixel, and the finest zoom is where the median
vertex spacing spans four pixels. The writer logs the guess; pass `zooms=` or
`complete_from=` to override. On Overture buildings for a city that is zooms
2 to 16, complete from 13; on US counties, complete at every zoom; on census
tracts, complete from 4.

## Geometry

Points, lines and polygons, one family per file, and a family keeps its
geometry type at every zoom, as tippecanoe does. Lines and polygons carry a
quantized copy per LOD: one per zoom below the complete zoom, where the
thinning changes the row set, and one per two zooms above it. Coordinates are
interleaved x, y pairs, so a geometry is one Parquet column a tile reads in
one range. LOD geometry is COGP's
`quantized_geoarrow`: nested lists of interleaved int32 `[x, y]` pairs on the
LOD's pixel grid, decoded as `offset + scale * integer`. Nothing in the file is a private byte format. When the full-detail shapes of
the whole dataset are small (16 MB by default) a single LOD serves every zoom
instead of copies, which is what happens for things like counties. Finer
than the finest zoom the reader draws the exact primary geometry, so full
detail is always reachable and costs nothing to store.

## Quick start

```bash
uv pip install -e .
geoparquet-lod write buildings.parquet buildings.lod.parquet \
  --thinning size:4+drop:2.5 --where "risk > 0" --carry risk
geoparquet-lod validate buildings.lod.parquet
geoparquet-lod inspect buildings.lod.parquet
```

Or from Python:

```python
from geoparquet_lod import write, drop_rate, only

write(
    "buildings.parquet", "buildings.lod.parquet",
    thinning=only("risk > 0", drop_rate(2.5)),
    carry=["risk"],
)
```

## Demo

`demo/index.html` is a MapLibre page that reads a file straight from a URL
and shows which LOD each zoom is drawing. Click a feature to read its row.

```bash
cd js && npm install && npm run demo
```

That bundles the reader into `demo/dist`, serves the repo at
`http://localhost:8091`, and opens the page pointed at `buildings.lod.parquet`
in the repo root. Pass any other `?url=` whose server answers HTTP range
requests, including plain S3. The page must be served; module scripts do not
load from `file://`.

To make a file worth looking at, pull a city of Overture buildings and run
the example, which guesses zooms from the data and carries height and class:

```bash
uv run python examples/overture_buildings.py overture_denver.parquet overture_denver.lod.parquet
```

## Measured

The same 155.6 million US building footprints served four ways:

- `lod`: this layout, 30.3 GB, 150 row groups, 15 LODs, every source column
  kept, read through its page indexes. LOD geometry is interleaved GeoArrow
  quantized to a quarter pixel, in 16 KB pages, and each LOD carries
  `rps_2011` and `rps_2047`. Thinning is
  `only('rps_2011 > 0 AND rps_2047 > 0', drop_rate(2.5))`: below zoom 13 only
  buildings with nonzero risk in both years are eligible, and every building
  is present from zoom 13. The reader also refines page selection with the
  bbox columns' page indexes when the footer's sizes say the extra reads cost
  less than the pages they skip.
- `lod_tiledir`: the same file read through its optional tile directory
  (see [below](#tile-directory)) at the zooms it covers, z7 to z16, and
  through the page indexes elsewhere.
- `cogp`: Kanahiro's cloud-optimized GeoParquet with quantized overviews, read
  with `@cogp/reader`. Its levels have no thinning other than the bbox-size
  rule that assigns a building to a level, so coarse tiles hold only the
  largest buildings.
- `pmtiles`: the production archive, built by tippecanoe v2.79
  (`-zg -Z 6 --drop-smallest-as-needed --extend-zooms-if-still-dropping`),
  zooms 6 to 14, seven attributes per feature, gzip-compressed tiles.
- `sorted`: a plain Hilbert-sorted GeoParquet with 1,552 row groups and no
  LOD and no page index. Its per-tile cost is computed from the footer, not
  transferred.

**Transport.** `lod` and `lod_tiledir` read the file and its sidecar from a
local http-server (port 8097) with range support; `cogp`, `pmtiles` and
`sorted` read from S3 (us-west-2, no CDN). Bytes and requests are comparable
across all of them; times are not, since the local reads skip the 50 to
100 ms of a round trip to S3. Compare times only between `lod` and
`lod_tiledir`, or between `cogp` and `pmtiles`. This http-server refuses
suffix range requests, so the directory open spends a refused request and a
HEAD where S3 would answer the first request.

**Method.** Open is counted once per reader, separately, as the median of 51
cold opens: footers, the PMTiles header and root directory, and every read a
reader starts at open. `LodReader.open` warms the prefix row groups' bbox page
indexes unconditionally, which is most of its 0.93 MB; `lod_tiledir`'s open
adds the directory's 0.91 MB footer. Each tile is then rendered on a fresh
reader (cold), and again on that same reader (warm, same tile): what a
revisit costs once indexes and directory groups are cached, since no reader
here caches data pages. The pans render four tiles eastward on one reader
after its open. Transferred counts the body of every 206 response, and of 200
responses other than HEAD; requests count every response. For PMTiles it is
the gzip bytes on the wire and decoded MVT is the decompressed tile; for the
Parquet readers decoded MVT is the tile they encode. Features are counted by
parsing each MVT; for PMTiles tiles past z14 the parent tile is fetched (and,
in the warm and pan runs, fetched again; MapLibre would reuse it) and only
features that reach the requested tile are counted. Time is the median of
three runs from one laptop under Node 24, whose fetch opens as many
connections as asked; a browser on HTTP/1.1, which is all S3 serves, runs six
at a time per host. `lod` and `lod_tiledir` returned identical features on
every tile, warm render and pan tile, and no implementation errored.

**This is a product comparison, not a controlled encoding comparison.** The
PMTiles archive and this file thin differently and carry different
attributes, so compare bytes at the same tile and read the feature counts to
judge visual parity. Tippecanoe drops buildings to fit its 500 KB tile limit
up to z13 (at z13 it keeps 46% of downtown San Francisco and 48% of downtown
Los Angeles); ours keeps every building from z13, and below z13 keeps only
buildings at risk, so at z11 Denver, with little modeled risk, shows 1,413
buildings to PMTiles' 4,741. PMTiles carries seven attributes per feature to
our two. From z13 COGP and ours hold the same buildings, and from z14 all
three agree within 6%, the rest being tile buffers.

Reproduce with `cd js && npm run build && node test/compare.mjs`, which
prints these tables (pass `z,lon,lat` arguments for other tiles, `RUNS=n` for
more runs, `IMPLS=lod,pmtiles` for a subset, `LOD_URL=` for another copy of
the file, `TILEDIR_URL=` for its sidecar to add `lod_tiledir`).

Open, once per map session (median of 51 cold opens):

| implementation | transferred | requests | time    |
| -------------- | ----------- | -------- | ------- |
| lod            | 0.93 MB     | 5        | 23 ms   |
| lod_tiledir    | 1.84 MB     | 9        | 44 ms   |
| cogp           | 1.00 MB     | 3        | 1073 ms |
| pmtiles        | 0.02 MB     | 1        | 232 ms  |
| sorted         | 1.96 MB     | 3        | 1407 ms |

Per tile after open: first render on a fresh reader (cold), then the same
tile again on that reader (warm); median time of 3 runs:

| tile                                 | impl        | cold                       | warm, same tile         | decoded MVT | features | note                                         |
| ------------------------------------ | ----------- | -------------------------- | ----------------------- | ----------- | -------- | -------------------------------------------- |
| z3 national                          | lod         | 0.24 MB, 1 req, 36 ms      | 0.00 MB, 0 req, 11 ms   | 226 KB      | 2,925    | LOD f3                                       |
| z3 national                          | lod_tiledir | 0.24 MB, 1 req, 28 ms      | 0.00 MB, 0 req, 7 ms    | 226 KB      | 2,925    | page indexes (zoom not in directory), LOD f3 |
| z3 national                          | cogp        | 2.13 MB, 42 req, 1411 ms   | 0.00 MB, 0 req, 77 ms   | 1 KB        | 10       | level 0, 43505 rows read                     |
| z3 national                          | pmtiles     |                            |                         |             |          | no tile: archive minzoom is 6                |
| z5 regional, Bay Area                | lod         | 0.24 MB, 4 req, 30 ms      | 0.24 MB, 3 req, 24 ms   | 339 KB      | 4,415    | LOD f5                                       |
| z5 regional, Bay Area                | lod_tiledir | 0.24 MB, 4 req, 24 ms      | 0.24 MB, 3 req, 23 ms   | 339 KB      | 4,415    | page indexes (zoom not in directory), LOD f5 |
| z5 regional, Bay Area                | cogp        | 1.06 MB, 23 req, 1106 ms   | 0.00 MB, 0 req, 44 ms   | 8 KB        | 99       | level 1, 17291 rows read                     |
| z5 regional, Bay Area                | pmtiles     |                            |                         |             |          | no tile: archive minzoom is 6                |
| z6 regional, Southern California     | lod         | 0.08 MB, 4 req, 11 ms      | 0.07 MB, 3 req, 11 ms   | 157 KB      | 2,049    | LOD f6                                       |
| z6 regional, Southern California     | lod_tiledir | 0.08 MB, 4 req, 11 ms      | 0.07 MB, 3 req, 10 ms   | 157 KB      | 2,049    | page indexes (zoom not in directory), LOD f6 |
| z6 regional, Southern California     | cogp        | 0.94 MB, 50 req, 1205 ms   | 0.00 MB, 0 req, 39 ms   | 14 KB       | 184      | level 2, 8668 rows read                      |
| z6 regional, Southern California     | pmtiles     | 0.11 MB, 2 req, 251 ms     | 0.10 MB, 1 req, 99 ms   | 242 KB      | 3,620    |                                              |
| z8 metro, Denver                     | lod         | 0.59 MB, 11 req, 246 ms    | 0.10 MB, 7 req, 25 ms   | 390 KB      | 5,014    | LOD f8                                       |
| z8 metro, Denver                     | lod_tiledir | 0.35 MB, 9 req, 125 ms     | 0.28 MB, 8 req, 109 ms  | 390 KB      | 5,014    | directory, LOD f8                            |
| z8 metro, Denver                     | cogp        | 1.79 MB, 61 req, 1465 ms   | 0.00 MB, 0 req, 75 ms   | 620 KB      | 8,081    | level 4, 24243 rows read                     |
| z8 metro, Denver                     | pmtiles     | 0.17 MB, 2 req, 395 ms     | 0.16 MB, 1 req, 180 ms  | 334 KB      | 4,875    |                                              |
| z9 metro, Atlanta                    | lod         | 1.32 MB, 31 req, 150 ms    | 1.06 MB, 23 req, 144 ms | 844 KB      | 10,651   | LOD f9                                       |
| z9 metro, Atlanta                    | lod_tiledir | 0.80 MB, 20 req, 120 ms    | 0.73 MB, 19 req, 110 ms | 844 KB      | 10,651   | directory, LOD f9                            |
| z9 metro, Atlanta                    | cogp        | 4.14 MB, 211 req, 10795 ms | 0.00 MB, 0 req, 305 ms  | 3.10 MB     | 38,840   | level 5, 49033 rows read                     |
| z9 metro, Atlanta                    | pmtiles     | 0.15 MB, 2 req, 363 ms     | 0.14 MB, 1 req, 94 ms   | 280 KB      | 3,891    |                                              |
| z11 city, Denver                     | lod         | 0.52 MB, 20 req, 80 ms     | 0.34 MB, 15 req, 66 ms  | 109 KB      | 1,413    | LOD f11                                      |
| z11 city, Denver                     | lod_tiledir | 0.30 MB, 8 req, 44 ms      | 0.24 MB, 7 req, 34 ms   | 109 KB      | 1,413    | directory, LOD f11                           |
| z11 city, Denver                     | cogp        | 2.41 MB, 215 req, 15951 ms | 0.00 MB, 0 req, 80 ms   | 520 KB      | 7,376    | level 7, 7381 rows read                      |
| z11 city, Denver                     | pmtiles     | 0.17 MB, 2 req, 375 ms     | 0.16 MB, 1 req, 153 ms  | 334 KB      | 4,741    |                                              |
| z12 suburbs, Atlanta                 | lod         | 0.52 MB, 16 req, 31 ms     | 0.40 MB, 11 req, 28 ms  | 179 KB      | 2,283    | LOD f12                                      |
| z12 suburbs, Atlanta                 | lod_tiledir | 0.34 MB, 8 req, 29 ms      | 0.26 MB, 7 req, 22 ms   | 179 KB      | 2,283    | directory, LOD f12                           |
| z12 suburbs, Atlanta                 | cogp        | 2.15 MB, 198 req, 9495 ms  | 0.00 MB, 0 req, 78 ms   | 463 KB      | 6,510    | level 8, 6512 rows read                      |
| z12 suburbs, Atlanta                 | pmtiles     | 0.13 MB, 2 req, 342 ms     | 0.12 MB, 1 req, 107 ms  | 223 KB      | 3,098    |                                              |
| z12 desert, Tonopah NV               | lod         | 0.27 MB, 13 req, 14 ms     | 0.20 MB, 10 req, 11 ms  | 25 KB       | 316      | LOD f12                                      |
| z12 desert, Tonopah NV               | lod_tiledir | 0.14 MB, 7 req, 16 ms      | 0.09 MB, 6 req, 9 ms    | 25 KB       | 316      | directory, LOD f12                           |
| z12 desert, Tonopah NV               | cogp        | 0.84 MB, 87 req, 1370 ms   | 0.00 MB, 0 req, 28 ms   | 59 KB       | 772      | level 8, 772 rows read                       |
| z12 desert, Tonopah NV               | pmtiles     | 0.01 MB, 2 req, 263 ms     | 0.00 MB, 1 req, 108 ms  | 8 KB        | 95       |                                              |
| z13 downtown, San Francisco          | lod         | 1.62 MB, 25 req, 178 ms    | 1.44 MB, 19 req, 194 ms | 627 KB      | 16,948   | LOD f13                                      |
| z13 downtown, San Francisco          | lod_tiledir | 0.70 MB, 8 req, 94 ms      | 0.63 MB, 7 req, 75 ms   | 627 KB      | 16,948   | directory, LOD f13                           |
| z13 downtown, San Francisco          | cogp        | 1.49 MB, 111 req, 1527 ms  | 0.00 MB, 0 req, 71 ms   | 525 KB      | 16,963   | level 9, 16969 rows read                     |
| z13 downtown, San Francisco          | pmtiles     | 0.22 MB, 2 req, 409 ms     | 0.21 MB, 1 req, 162 ms  | 483 KB      | 7,855    |                                              |
| z13 downtown, Los Angeles            | lod         | 1.33 MB, 27 req, 113 ms    | 1.00 MB, 20 req, 93 ms  | 381 KB      | 10,632   | LOD f13                                      |
| z13 downtown, Los Angeles            | lod_tiledir | 0.53 MB, 9 req, 61 ms      | 0.44 MB, 8 req, 49 ms   | 381 KB      | 10,632   | directory, LOD f13                           |
| z13 downtown, Los Angeles            | cogp        | 1.34 MB, 116 req, 1538 ms  | 0.00 MB, 0 req, 60 ms   | 333 KB      | 10,611   | level 9, 10647 rows read                     |
| z13 downtown, Los Angeles            | pmtiles     | 0.15 MB, 2 req, 325 ms     | 0.14 MB, 1 req, 107 ms  | 315 KB      | 5,091    |                                              |
| z13 rural, Broken Bow NE             | lod         | 0.48 MB, 18 req, 28 ms     | 0.39 MB, 14 req, 20 ms  | 149 KB      | 2,131    | LOD f13                                      |
| z13 rural, Broken Bow NE             | lod_tiledir | 0.19 MB, 7 req, 30 ms      | 0.15 MB, 6 req, 20 ms   | 149 KB      | 2,131    | directory, LOD f13                           |
| z13 rural, Broken Bow NE             | cogp        | 1.05 MB, 109 req, 1462 ms  | 0.00 MB, 0 req, 38 ms   | 146 KB      | 2,131    | level 9, 2132 rows read                      |
| z13 rural, Broken Bow NE             | pmtiles     | 0.06 MB, 2 req, 285 ms     | 0.05 MB, 1 req, 168 ms  | 85 KB       | 1,042    |                                              |
| z13 empty, Nebraska Sandhills        | lod         | 0.24 MB, 12 req, 9 ms      | 0.16 MB, 8 req, 9 ms    | 0 KB        | 0        | LOD f13                                      |
| z13 empty, Nebraska Sandhills        | lod_tiledir | 0.04 MB, 1 req, 8 ms       | 0.00 MB, 0 req, 1 ms    | 0 KB        | 0        | directory, LOD f13                           |
| z13 empty, Nebraska Sandhills        | cogp        | 0.81 MB, 62 req, 858 ms    | 0.00 MB, 0 req, 18 ms   | 0 KB        | 0        | level 9, 0 rows read                         |
| z13 empty, Nebraska Sandhills        | pmtiles     | 0.01 MB, 1 req, 94 ms      |                         |             |          | no tile: empty                               |
| z14 row-group edge, Chicago          | lod         | 0.85 MB, 24 req, 70 ms     | 0.63 MB, 20 req, 57 ms  | 279 KB      | 8,868    | LOD f13                                      |
| z14 row-group edge, Chicago          | lod_tiledir | 0.32 MB, 9 req, 44 ms      | 0.22 MB, 8 req, 35 ms   | 279 KB      | 8,868    | directory, LOD f13                           |
| z14 row-group edge, Chicago          | cogp        | 1.16 MB, 115 req, 1491 ms  | 0.00 MB, 0 req, 48 ms   | 267 KB      | 8,872    | level 10, 8877 rows read                     |
| z14 row-group edge, Chicago          | pmtiles     | 0.18 MB, 2 req, 353 ms     | 0.17 MB, 1 req, 118 ms  | 487 KB      | 9,020    |                                              |
| z15 street, Manhattan                | lod         | 0.73 MB, 21 req, 56 ms     | 0.35 MB, 11 req, 33 ms  | 50 KB       | 1,098    | LOD f15                                      |
| z15 street, Manhattan                | lod_tiledir | 0.25 MB, 4 req, 24 ms      | 0.12 MB, 3 req, 9 ms    | 50 KB       | 1,098    | directory, LOD f15                           |
| z15 street, Manhattan                | cogp        | 1.26 MB, 122 req, 2250 ms  | 0.00 MB, 0 req, 40 ms   | 47 KB       | 1,098    | level 11, 1099 rows read                     |
| z15 street, Manhattan                | pmtiles     | 0.10 MB, 2 req, 301 ms     | 0.09 MB, 1 req, 112 ms  | 204 KB      | 1,037    | overzoomed from z14                          |
| z15 street, Denver suburbs           | lod         | 0.46 MB, 20 req, 23 ms     | 0.18 MB, 12 req, 14 ms  | 18 KB       | 351      | LOD f15                                      |
| z15 street, Denver suburbs           | lod_tiledir | 0.17 MB, 5 req, 21 ms      | 0.06 MB, 4 req, 7 ms    | 18 KB       | 351      | directory, LOD f15                           |
| z15 street, Denver suburbs           | cogp        | 1.40 MB, 144 req, 5602 ms  | 0.00 MB, 0 req, 42 ms   | 17 KB       | 350      | level 11, 354 rows read                      |
| z15 street, Denver suburbs           | pmtiles     | 0.06 MB, 2 req, 316 ms     | 0.05 MB, 1 req, 110 ms  | 96 KB       | 333      | overzoomed from z14                          |
| z16 street, San Francisco            | lod         | 0.44 MB, 17 req, 22 ms     | 0.24 MB, 11 req, 14 ms  | 7 KB        | 148      | LOD f15                                      |
| z16 street, San Francisco            | lod_tiledir | 0.16 MB, 4 req, 18 ms      | 0.06 MB, 3 req, 6 ms    | 7 KB        | 148      | directory, LOD f15                           |
| z16 street, San Francisco            | cogp        | 0.98 MB, 103 req, 1403 ms  | 0.00 MB, 0 req, 34 ms   | 6 KB        | 148      | level 12, 154 rows read                      |
| z16 street, San Francisco            | pmtiles     | 0.13 MB, 2 req, 342 ms     | 0.12 MB, 1 req, 117 ms  | 269 KB      | 147      | overzoomed from z14                          |
| z17 beyond finest LOD, San Francisco | lod         | 0.42 MB, 16 req, 15 ms     | 0.33 MB, 11 req, 11 ms  | 1 KB        | 15       | primary WKB geometry                         |
| z17 beyond finest LOD, San Francisco | lod_tiledir | 0.42 MB, 16 req, 15 ms     | 0.33 MB, 11 req, 11 ms  | 1 KB        | 15       | primary WKB geometry                         |
| z17 beyond finest LOD, San Francisco | cogp        | 0.96 MB, 95 req, 1359 ms   | 0.00 MB, 0 req, 30 ms   | 1 KB        | 15       | level 12, 15 rows read                       |
| z17 beyond finest LOD, San Francisco | pmtiles     | 0.13 MB, 2 req, 376 ms     | 0.12 MB, 1 req, 106 ms  | 269 KB      | 14       | overzoomed from z14                          |

Pans on one reader after open, four tiles eastward, totals (median time of 3
runs):

| pan                             | impl        | transferred | requests | time    | features |
| ------------------------------- | ----------- | ----------- | -------- | ------- | -------- |
| z13 San Francisco, 4 tiles east | lod         | 2.69 MB     | 75       | 253 ms  | 27,745   |
| z13 San Francisco, 4 tiles east | lod_tiledir | 1.16 MB     | 19       | 165 ms  | 27,745   |
| z13 San Francisco, 4 tiles east | cogp        | 2.01 MB     | 171      | 3131 ms | 27,761   |
| z13 San Francisco, 4 tiles east | pmtiles     | 0.34 MB     | 5        | 689 ms  | 11,661   |
| z15 Manhattan, 4 tiles east     | lod         | 1.73 MB     | 58       | 94 ms   | 3,047    |
| z15 Manhattan, 4 tiles east     | lod_tiledir | 0.47 MB     | 17       | 48 ms   | 3,047    |
| z15 Manhattan, 4 tiles east     | cogp        | 1.47 MB     | 158      | 7108 ms | 3,051    |
| z15 Manhattan, 4 tiles east     | pmtiles     | 0.32 MB     | 5        | 630 ms  | 2,875    |

Plain Hilbert-sorted GeoParquet, computed from the footer (geometry and the
two attributes of every row group whose bbox statistics touch the tile):

| tile                                 | would transfer | requests | row groups and rows             |
| ------------------------------------ | -------------- | -------- | ------------------------------- |
| z3 national                          | 4995.78 MB     | 1104     | 552 row groups, 55,373,824 rows |
| z5 regional, Bay Area                | 1742.66 MB     | 338      | 169 row groups, 16,951,296 rows |
| z6 regional, Southern California     | 640.04 MB      | 110      | 55 row groups, 5,517,312 rows   |
| z8 metro, Denver                     | 168.25 MB      | 36       | 18 row groups, 1,806,336 rows   |
| z9 metro, Atlanta                    | 116.31 MB      | 28       | 14 row groups, 1,404,928 rows   |
| z11 city, Denver                     | 93.24 MB       | 20       | 10 row groups, 1,003,520 rows   |
| z12 suburbs, Atlanta                 | 46.93 MB       | 12       | 6 row groups, 602,112 rows      |
| z12 desert, Tonopah NV               | 8.87 MB        | 2        | 1 row group, 100,352 rows       |
| z13 downtown, San Francisco          | 45.97 MB       | 8        | 4 row groups, 401,408 rows      |
| z13 downtown, Los Angeles            | 24.74 MB       | 4        | 2 row groups, 200,704 rows      |
| z13 rural, Broken Bow NE             | 23.15 MB       | 6        | 3 row groups, 301,056 rows      |
| z13 empty, Nebraska Sandhills        | 7.67 MB        | 2        | 1 row group, 100,352 rows       |
| z14 row-group edge, Chicago          | 28.87 MB       | 8        | 4 row groups, 401,408 rows      |
| z15 street, Manhattan                | 16.05 MB       | 4        | 2 row groups, 200,704 rows      |
| z15 street, Denver suburbs           | 34.74 MB       | 6        | 3 row groups, 301,056 rows      |
| z16 street, San Francisco            | 45.97 MB       | 8        | 4 row groups, 401,408 rows      |
| z17 beyond finest LOD, San Francisco | 45.97 MB       | 8        | 4 row groups, 401,408 rows      |

### Findings

**Where this matches PMTiles.** The coarse LODs sit in one prefix row group,
so the z3 tile is 0.24 MB in one request and z5 is 0.24 MB in four; the
PMTiles archive has no tiles below z6. At z6 we read 0.08 MB to PMTiles'
0.11 MB, with 2,049 buildings to its 3,620. Without an LOD the cost is orders
of magnitude higher: a plain sorted GeoParquet would read 7.7 MB to 5 GB per
tile. COGP is behind us on every tile but the z13 city tiles, where through
the page indexes it reads about the same bytes (1.49 MB to our 1.62 in San
Francisco, 1.34 to 1.33 in Los Angeles) in 4 times the requests; through the
directory we read half. Elsewhere it reads 1.4 to 12 times our page-index
bytes in 5 to 42 times our requests, and its coarse tiles hold 10 to 184
buildings.

**Where it does not.** From z8 up, leaving out the two near-empty tiles, a
cold tile through the page indexes costs 3.1 to 8.9 times PMTiles' bytes
(median 6) and 5.5 to 15.5 times its requests. Through the directory the
same tiles cost 1.2 to 5.3 times PMTiles' bytes (median 2.7): 1.2 to 2.8
times on street tiles (z14 to z16) in 4 to 9 requests, 3.2 to 3.5 times on
the z13 city tiles in 8 or 9, and 5.3 times at z9 Atlanta in 20, where a
metro tile wants thousands of rows spread over many pages. At the
near-empty desert and Sandhills tiles PMTiles reads about 10 KB; the page
indexes read 0.24 to 0.27 MB and the directory 0.14 MB (desert) and one
40 KB directory read (Sandhills). Warm, the requests stay apart: revisiting
a tile costs 7 to 23 requests through the page indexes and 3 to 19 through
the directory (none for the empty tile), against one. A four-tile pan at z15
in Manhattan is 0.47 MB in 17 requests through the directory, 1.73 MB in 58
through the page indexes, and 0.32 MB in 5 for PMTiles. Opening costs
0.93 MB in five requests, 1.84 MB in nine with the directory, against
PMTiles' 16 KB in one.

What a cold tile spends its bytes on depends on zoom. Through the page
indexes, at the z13 city tiles the bbox refinement reads are the largest
item, 812 KB of San Francisco's 1.62 MB: the tile covers many pages, and
each row group it touches needs the bbox columns' indexes. On street tiles
(z14 to z17) the page indexes dominate: the tile needs a few data pages per
row group but pays for the column and offset indexes of every row group it
touches. The directory replaces both with one read of about 40 to 140 KB.

### Tile directory

The tile directory is an optional accelerator, defined in `spec/SPEC.md` as
a rendering profile and described [below](#tile-directory-sidecar): a
Parquet sidecar, `buildings.parquet.tiledir.parquet`, mapping each tile at
every zoom above the prefix to the page byte ranges it needs, so a tile
skips the bbox statistics, page indexes and refinement reads. Nothing
depends on it: a reader without it, or with a stale or missing one, reads
through the page indexes and draws the same features. For this file it is
219.6 MB in 3,535 row groups of about 64 KB, with a 913 KB footer, and
covers z7 to z16; zooms the prefix row group serves (z6 and below) and z17,
past the finest LOD, read as before. It was converted from an earlier
binary sidecar of the same entries, whose build from the 30 GB file took
40 minutes; `geoparquet-lod tiledir buildings.parquet` builds it directly.
The file was written before the `tile_directory` declaration existed and
was not rewritten to add it, so the sidecar is bound by file size and
footer length only and the reader warns about that.

Two row-group shapes of the same table were measured with the reader on the
13 tiles from z8 to z16 above, over local HTTP. "Lookup" is the sidecar
bytes a cold tile reads after the sidecar is open; round trips count
requests that had to wait for an earlier one.

| shape                                                   | file     | footer (open) | lookup per cold tile                          | round trips per cold tile, with data | next tile east         |
| ------------------------------------------------------- | -------- | ------------- | --------------------------------------------- | ------------------------------------ | ---------------------- |
| 75 groups of 200k rows, 16 KB pages, page indexes       | 224.3 MB | 30 KB         | 37 to 79 KB in 4 to 5 requests, 3 round trips | 4                                    | 5 to 33 KB, 2 requests |
| 3,535 groups of about 64 KB, a zoom never spans a group | 219.6 MB | 913 KB        | 39 to 137 KB in 1 request, 1 round trip       | 2                                    | none, same group       |

The first shape opens for 30 KB but a lookup is a chain: the zoom and tile
column indexes and the offset indexes, then the tile column's page to find
the row, then the page holding that row's list. The second is one range per
lookup chosen from footer statistics, and a neighbouring tile is usually in
the group already read, at the price of a 913 KB footer read once per
session. The directory keeps the second shape. The `pages` leaf is PLAIN
rather than delta coded: its values interleave six fields, so deltas are
large, and plain values compress about a third smaller under zstd. Page
indexes and statistics on `pages` are left out, since a lookup never reads
them; that is a quarter of the footer.

Known limits:

- The builder holds every entry of an LOD in memory as sets of pages per
  tile and loops over rows and tiles in Python. A row is added to every
  tile its padded bbox touches at every covered zoom, so large polygons and
  long lines cost memory and time in proportion to their extent in tiles at
  the finest zoom. The national build took 40 minutes; regional files take
  seconds.
- Antimeridian-crossing data, wrapped world tiles and tiles near the poles
  are untested; bboxes are clamped to the Web Mercator latitude limit.
- The directory is bound to one write of the file; any rewrite needs a new
  sidecar, and without a declared generation id a same-size rewrite with
  the same footer length would not be detected.
- The open is 0.91 MB, as large as the file's own; the PMTiles comparison
  above is a product comparison (different thinning, seven attributes to
  our two), not a controlled test of the encoding.

Reproduce with both files served locally with range support:

```bash
uv run geoparquet-lod tiledir buildings.parquet
cd js && npm run build
TILEDIR_URL=http://localhost:8097/buildings.parquet.tiledir.parquet \
  LOD_URL=http://localhost:8097/buildings.parquet \
  IMPLS=lod,lod_tiledir node test/compare.mjs
```

**Where this leaves the comparison.** On Parquet's own indexes a cold tile
costs a median 6 times PMTiles' bytes and 5.5 to 15.5 times its requests from
z8 up. Through the directory it costs a median 2.7 times the bytes, 1.2 to
2.8 times at street zooms, in 4 to 9 requests against 2, and a pan of four
street tiles 1.5 times PMTiles' bytes in 17 requests against 5. That is after
an open of 1.84 MB against 16 KB. The floor is a data read per leaf per row
group a tile touches, so request parity with a format that stores each tile
as one blob is out of reach while tiles are assembled from column chunks. If
the map is the product and the data is secondary, build PMTiles. This layout
is for the case where the file is also the dataset: one upload that DuckDB,
GeoPandas and a map read alike, attributes that stay queryable and can be
appended without retiling, thinning that can see the attributes (show the
highest-risk buildings first, not a random sample), and exact geometry past
the finest zoom.

**Next steps, by effect.**

1. Shrink the opens: 0.93 MB for the file, most of it the prefix bbox page
   indexes warmed at open and the column indexes in the footer, and 0.91 MB
   for the directory footer. Fewer, larger row groups shrink the file's
   footer; larger directory groups trade lookup bytes for footer bytes.
2. Build the directory in vectorized or compiled code, with bounded memory.
3. Rewrite the national file with `--tile-directory` so the sidecar is
   declared and bound by generation id.

The `lod` file is public; the demo above reads it directly with `?url=`:

```
https://carbonplan-ocr.s3.us-west-2.amazonaws.com/ocr-explore/geoparquet-overviews/lod/buildings.parquet
```

## Prior work

This builds on three proposals for zoomable GeoParquet and borrows from each:

- [GeoParquet PR 305](https://github.com/opengeospatial/geoparquet/pull/305)
  and [cloud-optimized-geoparquet](https://github.com/Kanahiro/cloud-optimized-geoparquet)
  by Kanahiro: levels as row-group prefixes, geometry stored once, analysis
  readers unaffected. Our `prefix` is one such level, and the tile encoder
  uses `@cogp/reader`'s MVT writer.
- [GeoParquet PR 299](https://github.com/opengeospatial/geoparquet/pull/299)
  by mmgeorge: per-resolution geometry columns declared in metadata. Our LOD
  columns are that idea with nullability carrying the row selection.
- [Esri Spatially Optimized Parquet](https://github.com/Esri/spatially-optimized-parquet):
  spatial clustering, page indexes and multiscale LOD columns in one file.
- [tippecanoe](https://github.com/felt/tippecanoe): the drop-rate and
  zoom-guessing rules are its `-r` and `-zg`.

`spec/SPEC.md` says where this layout differs and why.

## Status

Draft 0.1.0. The layout is settled and every encoding in the file is either
Parquet's own or COGP's `quantized_geoarrow`. See `spec/SPEC.md`.

## Tile directory sidecar

The optional tile directory is a Parquet table beside the file, an
accelerator a renderer may use and nothing else needs; `spec/SPEC.md`
defines it as the `parquet-table-1` rendering profile. Build it with the
file, or for an existing file:

```sh
uv run geoparquet-lod write src.parquet out.parquet --tile-directory
uv run geoparquet-lod tiledir overture_denver.lod.parquet   # writes overture_denver.lod.parquet.tiledir.parquet
cd js && npm run build && node test/tiledir-bench.mjs
```

```js
const tile = await renderTile(reader, z, x, y, {
  tiledir: tileDirectoryUrl(reader, fileUrl), // the declared path, else `${fileUrl}.tiledir.parquet`
})
```

`write(..., tile_directory=True)` declares the sidecar in the file's
`lod_columns.tile_directory` with a random generation id that the sidecar
repeats, and `validate` checks the declaration and, when the sidecar is
beside the file, its binding. For a file written without it (rewriting a
30 GB file to add metadata is not worth it), `geoparquet-lod tiledir` still
builds the sidecar, bound to the file by size and footer length only, and
the reader warns that no generation id is present. A missing, mismatched or
unreadable directory emits a warning and the tile is read through the page
indexes; a zoom the directory does not cover reads through the page indexes
without one, and tiles past the finest LOD read the primary geometry.
`TileDirectory.open(reader, url)`, `covers(lod, z)` and
`readLodByTile(lod, z, x, y, attributes)` are also exported.

The table has one row per tile with entries, sorted by `(zoom, tile)`:
`zoom` int8, `tile` int64 (Morton id on the zoom's grid), and `pages`, a list
of int64 sextuples `[row group, leaf, row start, row count, byte start, byte
length]` per page, with the row start relative to the row group and the leaf
an index into that directory's leaf paths. Row groups hold about 64 KB and
never span zooms, so footer statistics on `zoom` and `tile` pick one and a
lookup reads it whole in one range; opening reads the footer with a suffix
range request. Its footer metadata records the profile, tile size, buffer,
the binding to the file and, per LOD and zoom, the leaf paths and entry
count.

The Python builder reads bbox values and LOD struct null masks with PyArrow.
It parses the Parquet footer and offset indexes directly using Thrift compact
field IDs. For each shown row it pads the bbox by one LOD grid cell, projects
it to each of the LOD's zooms above the prefix, and adds its geometry and
carried-attribute pages to every intersecting buffered tile. Tile padding is
64/4096 of a tile, matching the renderer's eight-pixel buffer on 512px tiles.
The reader joins leaves by row number, fetches dictionaries through the
existing cache, and coalesces adjacent data pages. It filters rows using
bounds computed from decoded display coordinates, without reading bbox
columns. The existing MVT encoder performs the final clipping. Equality means
decoded feature IDs, geometries, and properties; feature ordering can change
serialized MVT size.

The benchmark defaults to Denver on port 8091 and New York `p16384.parquet` on
port 8098; `DENVER_URL`, `NYC_URL`, and `RUNS` override these. Each trial opens a
fresh reader, includes footer, prefix warmup, sidecar, and page requests, and
asserts decoded IDs, geometries, and attributes match. `RESULTS=/path/out.json`
saves machine-readable results. `FILE_TRANSPORT=1` uses a local-file byte-range
adapter when localhost networking is unavailable; its request counts are
logical requests and its timings measure local I/O, not HTTP latency.
`SCRATCHPAD` sets the New York fixture directory's parent. OS caches are not
flushed in either mode.
