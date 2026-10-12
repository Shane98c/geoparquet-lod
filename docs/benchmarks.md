# Benchmarks

The newer [spatial page index experiment](parity-experiment.md) measures
tile-aligned Parquet pages, a 16 KiB bootstrap, and direct spatial lookup on
the same Japan source. The tables below remain the baseline measurements.

Both sections were measured on 2026-10-12 against files written that day
by the current writer; the `cogp`, `pmtiles` and `sorted` numbers in the US
section are from 2026-10-10 against files that have not changed.

## Four readers on Esri's Japan buildings

Esri hosts a Spatially Optimized Parquet file of 53.7 million Overture
building footprints for Japan, which is also a valid GeoParquet file with a
WKB primary column. The same source, with Esri's multiscale and key columns
dropped, was written three more ways, so every panel below draws the same
features:

| file                            | writer, time                                                                                                                                                                                           | size    |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------- |
| `japan_buildings.parquet`       | the cleaned source, DuckDB                                                                                                                                                                             | 4.84 GB |
| `japan_buildings.lod15.parquet` | this layout, `chain(drop_smallest(lambda z: 1_100_000 if z <= 8 else 850_000), tiny_polygons())`, prefix to z10, complete from z14, 16 KB pages of at most 2048 rows, `class` as a value table, 22 min | 7.58 GB |
| `japan_buildings.cogp.parquet`  | `cogp convert --webmerc-minzoom 10` 2.2.1, the level ladder of COGP's own demo file, 5.7 min                                                                                                           | 9.69 GB |
| `japan_buildings_z0.pmtiles`    | tippecanoe 2.82, `-zg -Z 0 --drop-smallest-as-needed --extend-zooms-if-still-dropping`, 25 min                                                                                                         | 1.30 GB |
| `japan_buildings.sop.parquet`   | Esri's hosted file, `sop write`, 9 levels                                                                                                                                                              | 6.18 GB |

Each reader opens once, then each tile is read on a fresh reader, geometry
only, all four files served from one local range server. COGP is read with
`@cogp/reader` the way its MapLibre adapter does; Esri's file with the
reference reader from its repository's `viewer/src/maplibre`, which queries
the tile's extent at the tile's resolution and returns GeoJSON.

The four files do not agree on what a tile should contain, so the bytes
only mean something next to the feature counts. What each reader returned
for the tile:

| tile                      | LOD columns | COGP  | PMTiles | Esri SOP |
| ------------------------- | ----------- | ----- | ------- | -------- |
| z8 metro, Tokyo           | 95,850      | 1,152 | 95,774  | 28,000   |
| z11 city, Tokyo           | 28,337      | 860   | 24,373  | 177,679  |
| z13 downtown, Shibuya     | 29,913      | 2,739 | 28,908  | 51,996   |
| z13 suburbs, Chiba        | 5,084       | 454   | 4,929   | 6,841    |
| z15 street, Tokyo station | 229         | 213   | 1,441   | 229      |
| z15 street, Osaka         | 1,357       | 1,287 | 6,523   | 1,362    |

And what reading it cost:

| tile                      | LOD columns    | COGP        | PMTiles    | Esri SOP        |
| ------------------------- | -------------- | ----------- | ---------- | --------------- |
| open                      | 0.53 MB, 7 req | 1.52 MB, 3  | 0.02 MB, 1 | 2.62 MB, 3      |
| z8 metro, Tokyo           | 0.61 MB, 11    | 0.17 MB, 8  | 0.50 MB, 2 | 26.65 MB, 1,048 |
| z11 city, Tokyo           | 0.54 MB, 16    | 0.26 MB, 17 | 0.31 MB, 2 | 5.39 MB, 143    |
| z13 downtown, Shibuya     | 0.51 MB, 12    | 0.71 MB, 42 | 0.35 MB, 2 | 2.61 MB, 92     |
| z13 suburbs, Chiba        | 0.21 MB, 13    | 0.44 MB, 34 | 0.06 MB, 2 | 1.30 MB, 55     |
| z15 street, Tokyo station | 0.20 MB, 14    | 0.90 MB, 82 | 0.03 MB, 2 | 1.30 MB, 62     |
| z15 street, Osaka         | 0.25 MB, 14    | 0.93 MB, 80 | 0.11 MB, 2 | 1.01 MB, 57     |

The open includes what opening starts in the background: the key and
coarse LOD indexes of the prefix row groups, so a map opening at a coarse
zoom reads them once.

The open includes what opening starts in the background: the key and
coarse LOD indexes of the prefix row groups, so a map opening at a coarse
zoom reads them once.

The same two files on S3 (us-west-2), read cold from a laptop in the US
with wall-clock per tile, since over a network the requests cost round
trips as well as bytes:

| tile, over S3             | LOD columns             | PMTiles                |
| ------------------------- | ----------------------- | ---------------------- |
| open                      | 1.23 MB, 6 req          | 0.02 MB, 1 req         |
| z8 metro, Tokyo           | 0.68 MB, 5 req, 589 ms  | 0.50 MB, 2 req, 463 ms |
| z11 city, Tokyo           | 0.50 MB, 14 req, 835 ms | 0.31 MB, 2 req, 329 ms |
| z13 downtown, Shibuya     | 0.47 MB, 9 req, 655 ms  | 0.35 MB, 2 req, 312 ms |
| z13 suburbs, Chiba        | 0.16 MB, 9 req, 444 ms  | 0.06 MB, 2 req, 345 ms |
| z15 street, Tokyo station | 0.15 MB, 8 req, 396 ms  | 0.03 MB, 2 req, 295 ms |
| z15 street, Osaka         | 0.14 MB, 8 req, 581 ms  | 0.11 MB, 2 req, 357 ms |

A cold tile there was three dependent stages after the footer (the key
index, the boundary key pages, the LOD run) against the archive's one
after its directory, which is what the 1.3 to 2.5 times longer wait was.
The reader has since dropped the middle stage, taking a tile's key runs at
page granularity whenever trimming them to exact rows would save under a
quarter megabyte, and opens with a 64 KB probe and then exactly the rest
of the footer. The same two files on Cloudflare R2, read cold from node
(HTTP/1.1; a browser would multiplex over HTTP/2):

| tile, over R2             | LOD columns             | PMTiles                |
| ------------------------- | ----------------------- | ---------------------- |
| open                      | 0.54 MB, 7 req          | 0.02 MB, 1 req         |
| z8 metro, Tokyo           | 0.68 MB, 5 req, 665 ms  | 0.50 MB, 2 req, 353 ms |
| z11 city, Tokyo           | 0.51 MB, 10 req, 945 ms | 0.31 MB, 2 req, 358 ms |
| z13 downtown, Shibuya     | 0.51 MB, 6 req, 515 ms  | 0.35 MB, 2 req, 351 ms |
| z13 suburbs, Chiba        | 0.19 MB, 6 req, 592 ms  | 0.06 MB, 2 req, 261 ms |
| z15 street, Tokyo station | 0.21 MB, 6 req, 517 ms  | 0.03 MB, 2 req, 640 ms |
| z15 street, Osaka         | 0.23 MB, 6 req, 695 ms  | 0.11 MB, 2 req, 323 ms |

The next tile on the same reader is two requests, one run per section,
issued together: one round trip, as the archive's tile is. What stays
slower is the round trip for a row group's indexes the first time a
session touches it, which the reader now hides by warming the neighbouring
groups' indexes on every read, and the decode and MVT encode of each tile
in the worker, which a tile archive does not do.

Those are cold reads, each on a fresh reader. The tile beside each, read
next on the same reader, is what a session pays once a row group's indexes
are in:

| next tile                 | LOD columns | PMTiles    |
| ------------------------- | ----------- | ---------- |
| z8 metro, Tokyo           | 0.07 MB, 8  | 0.02 MB, 1 |
| z11 city, Tokyo           | 0.24 MB, 4  | 0.17 MB, 1 |
| z13 downtown, Shibuya     | 0.32 MB, 5  | 0.26 MB, 1 |
| z13 suburbs, Chiba        | 0.14 MB, 3  | 0.08 MB, 1 |
| z15 street, Tokyo station | 0.06 MB, 3  | 0.11 MB, 1 |
| z15 street, Osaka         | 0.11 MB, 4  | 0.13 MB, 1 |

Read the two tables together. COGP assigns a building to the coarsest
level where it spans a pixel, with no density target, so on footprints its
coarse levels are nearly empty: all of Japan at the coarsest level fits in
one row group, and a z11 tile of central Tokyo holds 860 buildings. Its
small bytes there are the cost of drawing almost nothing. The level,
bounds and resolution are the ones `@cogp/maplibre` computes for the tile,
and the file is written the way COGP's demo file is: a ladder from zoom 10
avoids levels whose grid would quantize a building to nothing. As a check
on the conversion, the script run with `COGP=` pointing at the file behind
COGP's MapLibre demo, a later Overture release of the same Japan buildings
with 73.4 million rows in 12.9 GB, returns 1,150, 875, 2,730, 484, 213 and
1,287 features for the six tiles, within a few buildings of the file here.
Esri's layout never drops a row, so a view reads every
building in it at the chosen level, and its reference reader fetches by XZ
key ranges rather than pages, which is where the request counts come from;
its README says a real implementation would tile. tippecanoe drops the
smallest buildings until a tile fits its limit, and the archive stops at
z14, so its z15 rows are the parent tile. Ours follows the same two rules
as tippecanoe, written as a chain and built independently at each zoom from
every building, as tippecanoe builds its zooms: one area bar per zoom,
raised until the busiest tile's estimated bytes fit a budget and applied
nationwide, then buildings under a quarter-pixel square accumulate into
squares holding their summed area. Each zoom of tippecanoe's archive was
read back to calibrate the budget on 213 tiles sampled across the density
range at zooms 6 to 12: 1.1 MB of this layout's bytes at z8 and below and
850 KB above lands within about 15% of tippecanoe's feature count at every
zoom, and the sparse and dense ends of the range agree equally well. The
archive is the target, not a constant: an earlier build of it from a
GeoJSON stream that wrote null attributes had features a third larger and
so dropped a third more of them, and one 600 KB budget matched that build
at every zoom. The same rule applied to the rows each finer zoom kept,
the nested membership of earlier writes, matched at zooms 9 to 12 and fell
to a quarter of tippecanoe's squares by zoom 7, because tippecanoe's bar is
not monotone across zooms: a coarse zoom folds buildings that a finer one
dropped.

The share of a tile that the polygons cover, after decoding, and the
distribution of shape sizes, now agree with the archive. On the z12 tile
of central Tokyo this layout paints 18.2% of the tile with 23,375 shapes
and PMTiles 18.0% with 21,700; the z11 tile 9.5% against 9.3%, and z13
Shibuya 29.1% against 30.4%. Binned by bbox pixels, the shapes a pixel or
larger agree within a few percent at every zoom; the difference is a few
thousand more sub-pixel squares in ours. An earlier write with a uniform
sample, `combine(size_threshold(4), density_budget(40_000))`, painted 12%
of the same z12 tile with 33,106 mostly sub-pixel shapes, a haze with more
features and less ink than either.

Where every format draws close to the full set, at z15, this layout reads
a fifth of the bytes of COGP in a seventh of the requests, and a sixth of
what the Esri reader fetches. At coarser zooms it is the only one of the
four parquet readers returning the archive's density picture, for about
the bytes of the PMTiles tile at z8 and 1.2 to 1.6 times them at city
zooms. What a tile costs, after the layout changes measured one at a time:

| z11 Tokyo, cold, earlier calibration      | KB  | requests |
| ----------------------------------------- | --- | -------- |
| key pages at the interval's ends          | 14  | 5        |
| the LOD column run, copies and attributes | 325 | 1        |
| offset and column indexes                 | 61  | 6        |
| PMTiles tile                              | 300 | 1        |

The run itself is close to the PMTiles tile: ring-step varints make a
placeholder square about six bytes and a footprint two bytes per vertex,
and the copies of buildings reaching in from neighbouring tiles are a
tenth of it. The rest is finding the rows, and in a session the indexes
are read once per row group. Earlier layouts of the same file, measured
on the same tiles: nested Arrow geometry read through the bbox page
indexes cost 0.60 MB in 18 requests here, a sorted key with a ring of
neighbouring cells for straddlers 0.83 MB in 34, and straddler copies in
Arrow spill lists 0.55 MB in 31; packing each LOD row into one binary
value halved the requests and took the open from 1.08 MB to 0.29 MB,
since the footer describes one leaf per LOD rather than seven. Pages of
16 KB and at most 2048 rows, against 64 KB, moved bytes from the key pages
and the column run into the indexes, which a session reads once: the cold
tile is about the same and the next tile a third to a half cheaper at
street zooms.

Reproduce with the four files served with range support:

```bash
cd js && npm run build
npx esbuild vendor/esri-sop/index.ts --bundle --platform=node --format=esm --outfile=test/esri-node.mjs
BASE=http://localhost:8099/ node test/compare4.mjs
```

The same four files side by side in a browser, each pane with its own byte
and request tally:

```
demo/compare.html?url=…/japan_buildings.lod15.parquet&cogp=…/japan_buildings.cogp.parquet&pmtiles=…/japan_buildings_z0.pmtiles&esri=…/japan_buildings.sop.parquet&center=139.76,35.68&zoom=12
```

## Measured

The same 155.6 million US building footprints served four ways:

- `lod`: this layout, 25.6 GB, 149 row groups, 15 LODs, every source column
  kept. Each LOD is one packed binary column; the prefix holds the rows
  shown by z10 in six row groups. Thinning is
  `only('rps_2011 > 0 AND rps_2047 > 0', drop_rate(2.5))`, the product's
  rule rather than tippecanoe's: below zoom 13 only buildings with nonzero
  risk in both years are eligible, and every building is present from zoom 13. Each LOD carries `rps_2011` and `rps_2047`.
- `cogp`: Kanahiro's cloud-optimized GeoParquet with quantized overviews, read
  with `@cogp/reader`. Its levels have no thinning other than the bbox-size
  rule that assigns a building to a level, so coarse tiles hold only the
  largest buildings. Its rows below are from the earlier measurement, since
  neither the file nor its reader changed.
- `pmtiles`: the production archive, built by tippecanoe v2.79
  (`-zg -Z 6 --drop-smallest-as-needed --extend-zooms-if-still-dropping`),
  zooms 6 to 14, seven attributes per feature, gzip-compressed tiles.
- `sorted`: a plain Hilbert-sorted GeoParquet with 1,552 row groups and no
  LOD and no page index. Its per-tile cost is computed from the footer, not
  transferred.

**Transport.** `lod` reads from a local http-server with range support;
`cogp`, `pmtiles` and `sorted` read from S3 (us-west-2, no CDN). Bytes and
requests are comparable across all of them; times are not, since the local
reads skip the round trip to S3. The Japan section above has both readers
on the same bucket with wall-clock per tile.

**Method.** Open is counted once per reader: footers, the PMTiles header and
root directory, and every read a reader starts at open, including the key
and coarse LOD indexes of the prefix row groups that `LodReader.open` warms.
Each tile is then rendered on a fresh reader (cold), and again on that same
reader (warm, same tile): what a revisit costs once indexes are cached; no
reader keeps LOD data pages. The pans render four tiles eastward on one
reader after its open. Transferred counts the body of every 206 response,
and of 200 responses other than HEAD; requests count every response. For
PMTiles it is the gzip bytes on the wire and decoded MVT is the
decompressed tile; for the Parquet readers decoded MVT is the tile they
encode. Features are counted by parsing each MVT; for PMTiles tiles past z14
the parent tile is fetched and only features that reach the requested tile
are counted. Time is one run from one laptop under Node 24.

**This is a product comparison, not a controlled encoding comparison.** The
PMTiles archive and this file thin differently and carry different
attributes, so compare bytes at the same tile and read the feature counts to
judge visual parity. Tippecanoe drops buildings to fit its tile limit up to
z13 (at z13 it keeps 46% of downtown San Francisco); ours keeps every
building from z13, and below z13 keeps only buildings at risk, so at z11
Denver, with little modeled risk, shows 1,333 buildings to PMTiles' 4,741.
PMTiles carries seven attributes per feature to our two.

Reproduce with `cd js && npm run build && node test/compare.mjs`, which
prints these tables (pass `z,lon,lat` arguments for other tiles, `RUNS=n` for
more runs, `IMPLS=lod,pmtiles` for a subset, `LOD_URL=` for another copy of
the file).

Open, once per map session:

| implementation | transferred | requests | time  |
| -------------- | ----------- | -------- | ----- |
| lod            | 1.19 MB     | 15       | 20 ms |
| cogp           | 1.63 MB     | 4        | 97 ms |
| pmtiles        | 0.02 MB     | 1        | 94 ms |

Per tile after open: first render on a fresh reader (cold), then the same
tile again on that reader (warm):

| tile                                 | impl    | cold                       | warm, same tile        | decoded MVT | features | note                          |
| ------------------------------------ | ------- | -------------------------- | ---------------------- | ----------- | -------- | ----------------------------- |
| z3 national                          | lod     | 0.19 MB, 12 req, 166 ms    | 0.11 MB, 3 req, 135 ms | 215 KB      | 2,729    | LOD f3                        |
| z3 national                          | cogp    | 2.13 MB, 42 req, 1411 ms   | 0.00 MB, 0 req, 77 ms  | 1 KB        | 10       | level 0, 43505 rows read      |
| z3 national                          | pmtiles |                            |                        |             |          | no tile: archive minzoom is 6 |
| z5 regional, Bay Area                | lod     | 0.17 MB, 11 req, 74 ms     | 0.09 MB, 2 req, 75 ms  | 333 KB      | 4,269    | LOD f5                        |
| z5 regional, Bay Area                | cogp    | 1.06 MB, 23 req, 1106 ms   | 0.00 MB, 0 req, 44 ms  | 8 KB        | 99       | level 1, 17291 rows read      |
| z5 regional, Bay Area                | pmtiles |                            |                        |             |          | no tile: archive minzoom is 6 |
| z6 regional, Southern California     | lod     | 0.11 MB, 11 req, 23 ms     | 0.04 MB, 2 req, 19 ms  | 151 KB      | 1,940    | LOD f6                        |
| z6 regional, Southern California     | cogp    | 0.94 MB, 50 req, 1205 ms   | 0.00 MB, 0 req, 39 ms  | 14 KB       | 184      | level 2, 8668 rows read       |
| z6 regional, Southern California     | pmtiles | 0.11 MB, 2 req, 245 ms     | 0.10 MB, 1 req, 89 ms  | 242 KB      | 3,620    |                               |
| z8 metro, Denver                     | lod     | 0.15 MB, 11 req, 24 ms     | 0.07 MB, 2 req, 18 ms  | 380 KB      | 4,879    | LOD f8                        |
| z8 metro, Denver                     | cogp    | 1.79 MB, 61 req, 1465 ms   | 0.00 MB, 0 req, 75 ms  | 620 KB      | 8,081    | level 4, 24243 rows read      |
| z8 metro, Denver                     | pmtiles | 0.17 MB, 2 req, 253 ms     | 0.16 MB, 1 req, 146 ms | 334 KB      | 4,875    |                               |
| z9 metro, Atlanta                    | lod     | 0.22 MB, 11 req, 58 ms     | 0.15 MB, 2 req, 52 ms  | 803 KB      | 10,137   | LOD f9                        |
| z9 metro, Atlanta                    | cogp    | 4.14 MB, 211 req, 10795 ms | 0.00 MB, 0 req, 305 ms | 3.10 MB     | 38,840   | level 5, 49033 rows read      |
| z9 metro, Atlanta                    | pmtiles | 0.15 MB, 2 req, 288 ms     | 0.14 MB, 1 req, 155 ms | 280 KB      | 3,891    |                               |
| z11 city, Denver                     | lod     | 0.18 MB, 12 req, 25 ms     | 0.07 MB, 2 req, 18 ms  | 103 KB      | 1,333    | LOD f11                       |
| z11 city, Denver                     | cogp    | 2.41 MB, 215 req, 15951 ms | 0.00 MB, 0 req, 80 ms  | 520 KB      | 7,376    | level 7, 7381 rows read       |
| z11 city, Denver                     | pmtiles | 0.17 MB, 2 req, 326 ms     | 0.16 MB, 1 req, 150 ms | 334 KB      | 4,741    |                               |
| z12 suburbs, Atlanta                 | lod     | 0.20 MB, 13 req, 22 ms     | 0.08 MB, 3 req, 11 ms  | 178 KB      | 2,287    | LOD f12                       |
| z12 suburbs, Atlanta                 | cogp    | 2.15 MB, 198 req, 9495 ms  | 0.00 MB, 0 req, 78 ms  | 463 KB      | 6,510    | level 8, 6512 rows read       |
| z12 suburbs, Atlanta                 | pmtiles | 0.13 MB, 2 req, 350 ms     | 0.12 MB, 1 req, 93 ms  | 223 KB      | 3,098    |                               |
| z12 desert, Tonopah NV               | lod     | 0.17 MB, 14 req, 13 ms     | 0.05 MB, 4 req, 6 ms   | 25 KB       | 309      | LOD f12                       |
| z12 desert, Tonopah NV               | cogp    | 0.84 MB, 87 req, 1370 ms   | 0.00 MB, 0 req, 28 ms  | 59 KB       | 772      | level 8, 772 rows read        |
| z12 desert, Tonopah NV               | pmtiles | 0.01 MB, 2 req, 338 ms     | 0.00 MB, 1 req, 104 ms | 8 KB        | 95       |                               |
| z13 downtown, San Francisco          | lod     | 0.54 MB, 14 req, 52 ms     | 0.37 MB, 4 req, 42 ms  | 626 KB      | 16,928   | LOD f13                       |
| z13 downtown, San Francisco          | cogp    | 1.49 MB, 111 req, 1527 ms  | 0.00 MB, 0 req, 71 ms  | 525 KB      | 16,963   | level 9, 16969 rows read      |
| z13 downtown, San Francisco          | pmtiles | 0.22 MB, 2 req, 296 ms     | 0.21 MB, 1 req, 100 ms | 483 KB      | 7,855    |                               |
| z13 downtown, Los Angeles            | lod     | 0.41 MB, 14 req, 51 ms     | 0.24 MB, 4 req, 26 ms  | 378 KB      | 10,536   | LOD f13                       |
| z13 downtown, Los Angeles            | cogp    | 1.34 MB, 116 req, 1538 ms  | 0.00 MB, 0 req, 60 ms  | 333 KB      | 10,611   | level 9, 10647 rows read      |
| z13 downtown, Los Angeles            | pmtiles | 0.15 MB, 2 req, 335 ms     | 0.14 MB, 1 req, 259 ms | 315 KB      | 5,091    |                               |
| z13 rural, Broken Bow NE             | lod     | 0.30 MB, 17 req, 21 ms     | 0.09 MB, 4 req, 12 ms  | 149 KB      | 2,124    | LOD f13                       |
| z13 rural, Broken Bow NE             | cogp    | 1.05 MB, 109 req, 1462 ms  | 0.00 MB, 0 req, 38 ms  | 146 KB      | 2,131    | level 9, 2132 rows read       |
| z13 rural, Broken Bow NE             | pmtiles | 0.06 MB, 2 req, 241 ms     | 0.05 MB, 1 req, 103 ms | 85 KB       | 1,042    |                               |
| z13 empty, Nebraska Sandhills        | lod     | 0.24 MB, 16 req, 12 ms     | 0.03 MB, 3 req, 5 ms   | 0 KB        | 0        | LOD f13                       |
| z13 empty, Nebraska Sandhills        | cogp    | 0.81 MB, 62 req, 858 ms    | 0.00 MB, 0 req, 18 ms  | 0 KB        | 0        | level 9, 0 rows read          |
| z13 empty, Nebraska Sandhills        | pmtiles | 0.01 MB, 1 req, 92 ms      |                        |             |          | no tile: empty                |
| z14 row-group edge, Chicago          | lod     | 0.28 MB, 14 req, 28 ms     | 0.12 MB, 4 req, 19 ms  | 279 KB      | 8,866    | LOD f13                       |
| z14 row-group edge, Chicago          | cogp    | 1.16 MB, 115 req, 1491 ms  | 0.00 MB, 0 req, 48 ms  | 267 KB      | 8,872    | level 10, 8877 rows read      |
| z14 row-group edge, Chicago          | pmtiles | 0.18 MB, 2 req, 407 ms     | 0.17 MB, 1 req, 106 ms | 487 KB      | 9,020    |                               |
| z15 street, Manhattan                | lod     | 0.22 MB, 13 req, 12 ms     | 0.05 MB, 3 req, 6 ms   | 50 KB       | 1,098    | LOD f15                       |
| z15 street, Manhattan                | cogp    | 1.26 MB, 122 req, 2250 ms  | 0.00 MB, 0 req, 40 ms  | 47 KB       | 1,098    | level 11, 1099 rows read      |
| z15 street, Manhattan                | pmtiles | 0.10 MB, 2 req, 262 ms     | 0.09 MB, 1 req, 110 ms | 204 KB      | 1,037    | overzoomed from z14           |
| z15 street, Denver suburbs           | lod     | 0.23 MB, 14 req, 19 ms     | 0.05 MB, 4 req, 8 ms   | 18 KB       | 351      | LOD f15                       |
| z15 street, Denver suburbs           | cogp    | 1.40 MB, 144 req, 5602 ms  | 0.00 MB, 0 req, 42 ms  | 17 KB       | 350      | level 11, 354 rows read       |
| z15 street, Denver suburbs           | pmtiles | 0.06 MB, 2 req, 267 ms     | 0.05 MB, 1 req, 135 ms | 96 KB       | 333      | overzoomed from z14           |
| z16 street, San Francisco            | lod     | 0.20 MB, 13 req, 11 ms     | 0.03 MB, 3 req, 6 ms   | 7 KB        | 148      | LOD f15                       |
| z16 street, San Francisco            | cogp    | 0.98 MB, 103 req, 1403 ms  | 0.00 MB, 0 req, 34 ms  | 6 KB        | 148      | level 12, 154 rows read       |
| z16 street, San Francisco            | pmtiles | 0.13 MB, 2 req, 247 ms     | 0.12 MB, 1 req, 97 ms  | 269 KB      | 147      | overzoomed from z14           |
| z17 beyond finest LOD, San Francisco | lod     | 0.50 MB, 28 req, 30 ms     | 0.18 MB, 4 req, 7 ms   | 1 KB        | 15       | primary WKB geometry          |
| z17 beyond finest LOD, San Francisco | cogp    | 0.96 MB, 95 req, 1359 ms   | 0.00 MB, 0 req, 30 ms  | 1 KB        | 15       | level 12, 15 rows read        |
| z17 beyond finest LOD, San Francisco | pmtiles | 0.13 MB, 2 req, 217 ms     | 0.12 MB, 1 req, 100 ms | 269 KB      | 14       | overzoomed from z14           |
| z13 San Francisco, 4 tiles east      | lod     | 0.87 MB                    | 25                     | 121 ms      | 27,704   |
| z13 San Francisco, 4 tiles east      | cogp    | 2.01 MB                    | 171                    | 3131 ms     | 27,761   |
| z13 San Francisco, 4 tiles east      | pmtiles | 0.34 MB                    | 5                      | 456 ms      | 11,661   |
| z15 Manhattan, 4 tiles east          | lod     | 0.34 MB                    | 22                     | 30 ms       | 3,047    |
| z15 Manhattan, 4 tiles east          | cogp    | 1.47 MB                    | 158                    | 7108 ms     | 3,051    |
| z15 Manhattan, 4 tiles east          | pmtiles | 0.32 MB                    | 5                      | 516 ms      | 2,875    |

Pans on one reader after open, four tiles eastward, totals:

| pan                             | impl    | transferred | requests | time    | features |
| ------------------------------- | ------- | ----------- | -------- | ------- | -------- |
| z13 San Francisco, 4 tiles east | lod     | 0.87 MB     | 25       | 121 ms  | 27,704   |
| z13 San Francisco, 4 tiles east | cogp    | 2.01 MB     | 171      | 3131 ms | 27,761   |
| z13 San Francisco, 4 tiles east | pmtiles | 0.34 MB     | 5        | 456 ms  | 11,661   |
| z13 San Francisco, 4 tiles east | cogp    | 2.01 MB     | 171      | 3131 ms | 27,761   |
| z15 Manhattan, 4 tiles east     | lod     | 0.34 MB     | 22       | 30 ms   | 3,047    |
| z15 Manhattan, 4 tiles east     | cogp    | 1.47 MB     | 158      | 7108 ms | 3,051    |
| z15 Manhattan, 4 tiles east     | pmtiles | 0.32 MB     | 5        | 516 ms  | 2,875    |
| z15 Manhattan, 4 tiles east     | cogp    | 1.47 MB     | 158      | 7108 ms | 3,051    |

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

**Where this matches PMTiles.** The coarse LODs sit in the prefix row
groups, so the z3 national tile is 0.19 MB and z5 is 0.17 MB; the PMTiles
archive has no tiles below z6. From z6 to z8 a cold tile costs about the
archive's bytes (0.11 MB to 0.11 at z6, 0.15 to 0.17 at z8 Denver) and a
warm one a third to a half of them. Without an LOD the cost is orders of
magnitude higher: a plain sorted GeoParquet would read 7.7 MB to 5 GB per
tile. COGP is behind on every tile.

**Where it does not.** At the z13 city tiles, where this file keeps every
building and the archive keeps half, a cold tile is 2.5 times the archive's
bytes (San Francisco 0.54 MB to 0.22, drawing 16,928 buildings to 7,855) and
a warm one 1.7 times. On street tiles (z15 and z16) a cold tile is 0.20 to
0.23 MB against 0.06 to 0.13, almost all of it the row group's indexes read
once per session; warm, the same tiles are 0.03 to 0.05 MB in 3 or 4
requests against the archive's 0.05 to 0.12 in 1, since the archive serves
them overzoomed from z14. A four-tile pan at z15 in Manhattan is 0.34 MB in
22 requests against 0.32 MB in 5; at z13 in San Francisco 0.87 MB against
0.34, drawing 2.4 times the buildings. Opening costs 1.19 MB in 15 requests,
the 404 KB footer and the six prefix groups' key and coarse LOD indexes,
against PMTiles' 16 KB in one.

**Where this leaves the comparison.** A warm tile is one round trip in each
section the file has, as the archive's tile is one; a cold tile adds a round
trip for the row group's indexes, which the reader hides by warming the
neighbouring groups' indexes on each read; and bytes are at parity where the
two draw the same buildings and 1.5 to 2.5 times where this file draws more.
If the map is the product and the data is secondary, build PMTiles. This
layout is for the case where the file is also the dataset: one upload that
DuckDB, GeoPandas and a map read alike, attributes that stay queryable and
can be appended without retiling, thinning that can see the attributes (show
the highest-risk buildings first, not a random sample), and exact geometry
past the finest zoom.

The `lod` file measured here is the rebuilt one; the public copy at the
URL below is replaced with it when uploaded:

```
https://carbonplan-ocr.s3.us-west-2.amazonaws.com/ocr-explore/geoparquet-overviews/lod/buildings.parquet
```
