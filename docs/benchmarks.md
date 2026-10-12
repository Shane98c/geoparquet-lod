# Benchmarks

The Japan section was measured on 2026-10-12 against a file written that
day by the current writer. The US section below it was measured on
2026-10-11 against the national file written by the writer of that day,
which stored nested Arrow geometry read through the bbox page indexes and
an optional tile directory sidecar, neither of which the current layout
has; those numbers stand until the national file is rewritten. Its `cogp`,
`pmtiles` and `sorted` numbers were measured the day before against files
that have not changed.

## Four readers on Esri's Japan buildings

Esri hosts a Spatially Optimized Parquet file of 53.7 million Overture
building footprints for Japan, which is also a valid GeoParquet file with a
WKB primary column. The same source, with Esri's multiscale and key columns
dropped, was written three more ways, so every panel below draws the same
features:

| file                            | writer, time                                                                                                                                                                 | size    |
| ------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- |
| `japan_buildings.parquet`       | the cleaned source, DuckDB                                                                                                                                                   | 4.84 GB |
| `japan_buildings.lod14.parquet` | this layout, `chain(drop_smallest(lambda z: 1_300_000 if z <= 8 else 850_000), tiny_polygons())`, prefix to z10, complete from z14, 16 KB pages of at most 2048 rows, 21 min | 7.62 GB |
| `japan_buildings.cogp.parquet`  | `cogp convert --webmerc-minzoom 10` 2.2.1, the level ladder of COGP's own demo file, 5.7 min                                                                                 | 9.69 GB |
| `japan_buildings_z0.pmtiles`    | tippecanoe 2.82, `-zg -Z 0 --drop-smallest-as-needed --extend-zooms-if-still-dropping`, 25 min                                                                               | 1.30 GB |
| `japan_buildings.sop.parquet`   | Esri's hosted file, `sop write`, 9 levels                                                                                                                                    | 6.18 GB |

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
| z8 metro, Tokyo           | 115,775     | 1,152 | 95,774  | 28,000   |
| z11 city, Tokyo           | 28,201      | 860   | 24,373  | 177,679  |
| z13 downtown, Shibuya     | 29,834      | 2,739 | 28,908  | 51,996   |
| z13 suburbs, Chiba        | 4,955       | 454   | 4,929   | 6,841    |
| z15 street, Tokyo station | 229         | 213   | 1,441   | 229      |
| z15 street, Osaka         | 1,357       | 1,287 | 6,523   | 1,362    |

And what reading it cost:

| tile                      | LOD columns    | COGP        | PMTiles    | Esri SOP        |
| ------------------------- | -------------- | ----------- | ---------- | --------------- |
| open                      | 0.47 MB, 8 req | 1.52 MB, 3  | 0.02 MB, 1 | 2.62 MB, 3      |
| z8 metro, Tokyo           | 0.69 MB, 9     | 0.17 MB, 8  | 0.50 MB, 2 | 26.65 MB, 1,048 |
| z11 city, Tokyo           | 0.50 MB, 16    | 0.26 MB, 17 | 0.31 MB, 2 | 5.39 MB, 143    |
| z13 downtown, Shibuya     | 0.47 MB, 9     | 0.71 MB, 42 | 0.35 MB, 2 | 2.61 MB, 92     |
| z13 suburbs, Chiba        | 0.16 MB, 9     | 0.44 MB, 34 | 0.06 MB, 2 | 1.30 MB, 55     |
| z15 street, Tokyo station | 0.15 MB, 8     | 0.90 MB, 82 | 0.03 MB, 2 | 1.30 MB, 62     |
| z15 street, Osaka         | 0.14 MB, 8     | 0.93 MB, 80 | 0.11 MB, 2 | 1.01 MB, 57     |

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

A cold tile here is three dependent stages after the footer (the key
index, the boundary key pages, the LOD run) against the archive's one
after its directory, which is what the 1.3 to 2.5 times longer wait is.
The open over S3 reads the file's last megabyte speculatively, hence its
size.

Those are cold reads, each on a fresh reader. The tile beside each, read
next on the same reader, is what a session pays once a row group's indexes
are in:

| next tile                 | LOD columns | PMTiles    |
| ------------------------- | ----------- | ---------- |
| z8 metro, Tokyo           | 0.07 MB, 9  | 0.02 MB, 1 |
| z11 city, Tokyo           | 0.25 MB, 6  | 0.17 MB, 1 |
| z13 downtown, Shibuya     | 0.32 MB, 5  | 0.26 MB, 1 |
| z13 suburbs, Chiba        | 0.12 MB, 5  | 0.08 MB, 1 |
| z15 street, Tokyo station | 0.06 MB, 4  | 0.11 MB, 1 |
| z15 street, Osaka         | 0.12 MB, 4  | 0.13 MB, 1 |

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
range at zooms 6 to 12: 1.3 MB of this layout's bytes at z8 and below and
850 KB above lands within about 20% of tippecanoe's feature count at every
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
demo/compare.html?url=…/japan_buildings.lod14.parquet&cogp=…/japan_buildings.cogp.parquet&pmtiles=…/japan_buildings_z0.pmtiles&esri=…/japan_buildings.sop.parquet&center=139.76,35.68&zoom=12
```

## Measured

The same 155.6 million US building footprints served four ways:

- `lod`: this layout, 24.2 GB, 150 row groups, 15 LODs, every source column
  kept, read through its page indexes. LOD geometry is interleaved GeoArrow
  quantized to a quarter pixel, in 16 KB pages, and each LOD carries
  `rps_2011` and `rps_2047`. Thinning is
  `only('rps_2011 > 0 AND rps_2047 > 0', drop_rate(2.5))`: below zoom 13 only
  buildings with nonzero risk in both years are eligible, and every building
  is present from zoom 13. The reader also refines page selection with the
  bbox columns' page indexes when the footer's sizes say the extra reads cost
  less than the pages they skip.
- `lod_tiledir`: the same file read through its optional tile directory
  (see [Tile directory](#tile-directory)) at the zooms it covers, z7 to z16, and
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
suffix range requests, so each open spends a refused request and a HEAD where
S3 would answer the first request.

**Method.** Open is counted once per reader, separately, as the median of 51
cold opens: footers, the PMTiles header and root directory, and every read a
reader starts at open. `LodReader.open` reads the file's last 1 MB in one request,
which holds the 922 KB footer, then warms the prefix row groups' bbox page
indexes and the coarse LODs' offset indexes; `lod_tiledir`'s open adds the
directory's 0.91 MB footer. Each tile is then rendered on a fresh
reader (cold), and again on that same reader (warm, same tile): what a
revisit costs once indexes, directory groups and, for `lod`, decoded bbox
pages are cached; no reader keeps LOD data pages. The pans render four tiles
eastward on one reader after its open. Transferred counts the body of every 206 response, and of 200
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
| lod            | 0.94 MB     | 6        | 22 ms   |
| lod_tiledir    | 1.81 MB     | 10       | 45 ms   |
| cogp           | 1.00 MB     | 3        | 1073 ms |
| pmtiles        | 0.02 MB     | 1        | 232 ms  |
| sorted         | 1.96 MB     | 3        | 1407 ms |

Per tile after open: first render on a fresh reader (cold), then the same
tile again on that reader (warm); median time of 3 runs:

| tile                                 | impl        | cold                       | warm, same tile         | decoded MVT | features | note                                         |
| ------------------------------------ | ----------- | -------------------------- | ----------------------- | ----------- | -------- | -------------------------------------------- |
| z3 national                          | lod         | 0.22 MB, 1 req, 29 ms      | 0.00 MB, 0 req, 8 ms    | 226 KB      | 2,925    | LOD f3                                       |
| z3 national                          | lod_tiledir | 0.22 MB, 1 req, 30 ms      | 0.00 MB, 0 req, 9 ms    | 226 KB      | 2,925    | page indexes (zoom not in directory), LOD f3 |
| z3 national                          | cogp        | 2.13 MB, 42 req, 1411 ms   | 0.00 MB, 0 req, 77 ms   | 1 KB        | 10       | level 0, 43505 rows read                     |
| z3 national                          | pmtiles     |                            |                         |             |          | no tile: archive minzoom is 6                |
| z5 regional, Bay Area                | lod         | 0.29 MB, 3 req, 32 ms      | 0.29 MB, 3 req, 33 ms   | 339 KB      | 4,415    | LOD f5                                       |
| z5 regional, Bay Area                | lod_tiledir | 0.29 MB, 3 req, 29 ms      | 0.29 MB, 3 req, 29 ms   | 339 KB      | 4,415    | page indexes (zoom not in directory), LOD f5 |
| z5 regional, Bay Area                | cogp        | 1.06 MB, 23 req, 1106 ms   | 0.00 MB, 0 req, 44 ms   | 8 KB        | 99       | level 1, 17291 rows read                     |
| z5 regional, Bay Area                | pmtiles     |                            |                         |             |          | no tile: archive minzoom is 6                |
| z6 regional, Southern California     | lod         | 0.17 MB, 3 req, 26 ms      | 0.17 MB, 3 req, 20 ms   | 157 KB      | 2,049    | LOD f6                                       |
| z6 regional, Southern California     | lod_tiledir | 0.17 MB, 3 req, 20 ms      | 0.17 MB, 3 req, 16 ms   | 157 KB      | 2,049    | page indexes (zoom not in directory), LOD f6 |
| z6 regional, Southern California     | cogp        | 0.94 MB, 50 req, 1205 ms   | 0.00 MB, 0 req, 39 ms   | 14 KB       | 184      | level 2, 8668 rows read                      |
| z6 regional, Southern California     | pmtiles     | 0.11 MB, 2 req, 251 ms     | 0.10 MB, 1 req, 99 ms   | 242 KB      | 3,620    |                                              |
| z8 metro, Denver                     | lod         | 0.54 MB, 7 req, 292 ms     | 0.09 MB, 3 req, 22 ms   | 390 KB      | 5,014    | LOD f8                                       |
| z8 metro, Denver                     | lod_tiledir | 0.33 MB, 9 req, 319 ms     | 0.26 MB, 8 req, 305 ms  | 390 KB      | 5,014    | directory, LOD f8                            |
| z8 metro, Denver                     | cogp        | 1.79 MB, 61 req, 1465 ms   | 0.00 MB, 0 req, 75 ms   | 620 KB      | 8,081    | level 4, 24243 rows read                     |
| z8 metro, Denver                     | pmtiles     | 0.17 MB, 2 req, 395 ms     | 0.16 MB, 1 req, 180 ms  | 334 KB      | 4,875    |                                              |
| z9 metro, Atlanta                    | lod         | 1.02 MB, 31 req, 289 ms    | 0.88 MB, 18 req, 290 ms | 844 KB      | 10,651   | LOD f9                                       |
| z9 metro, Atlanta                    | lod_tiledir | 0.72 MB, 21 req, 242 ms    | 0.64 MB, 20 req, 226 ms | 844 KB      | 10,651   | directory, LOD f9                            |
| z9 metro, Atlanta                    | cogp        | 4.14 MB, 211 req, 10795 ms | 0.00 MB, 0 req, 305 ms  | 3.10 MB     | 38,840   | level 5, 49033 rows read                     |
| z9 metro, Atlanta                    | pmtiles     | 0.15 MB, 2 req, 363 ms     | 0.14 MB, 1 req, 94 ms   | 280 KB      | 3,891    |                                              |
| z11 city, Denver                     | lod         | 0.42 MB, 22 req, 138 ms    | 0.27 MB, 11 req, 132 ms | 109 KB      | 1,413    | LOD f11                                      |
| z11 city, Denver                     | lod_tiledir | 0.27 MB, 8 req, 105 ms     | 0.22 MB, 7 req, 101 ms  | 109 KB      | 1,413    | directory, LOD f11                           |
| z11 city, Denver                     | cogp        | 2.41 MB, 215 req, 15951 ms | 0.00 MB, 0 req, 80 ms   | 520 KB      | 7,376    | level 7, 7381 rows read                      |
| z11 city, Denver                     | pmtiles     | 0.17 MB, 2 req, 375 ms     | 0.16 MB, 1 req, 153 ms  | 334 KB      | 4,741    |                                              |
| z12 suburbs, Atlanta                 | lod         | 0.41 MB, 16 req, 39 ms     | 0.28 MB, 7 req, 32 ms   | 179 KB      | 2,283    | LOD f12                                      |
| z12 suburbs, Atlanta                 | lod_tiledir | 0.31 MB, 8 req, 33 ms      | 0.23 MB, 7 req, 26 ms   | 179 KB      | 2,283    | directory, LOD f12                           |
| z12 suburbs, Atlanta                 | cogp        | 2.15 MB, 198 req, 9495 ms  | 0.00 MB, 0 req, 78 ms   | 463 KB      | 6,510    | level 8, 6512 rows read                      |
| z12 suburbs, Atlanta                 | pmtiles     | 0.13 MB, 2 req, 342 ms     | 0.12 MB, 1 req, 107 ms  | 223 KB      | 3,098    |                                              |
| z12 desert, Tonopah NV               | lod         | 0.21 MB, 14 req, 11 ms     | 0.10 MB, 6 req, 8 ms    | 25 KB       | 316      | LOD f12                                      |
| z12 desert, Tonopah NV               | lod_tiledir | 0.14 MB, 7 req, 13 ms      | 0.09 MB, 6 req, 9 ms    | 25 KB       | 316      | directory, LOD f12                           |
| z12 desert, Tonopah NV               | cogp        | 0.84 MB, 87 req, 1370 ms   | 0.00 MB, 0 req, 28 ms   | 59 KB       | 772      | level 8, 772 rows read                       |
| z12 desert, Tonopah NV               | pmtiles     | 0.01 MB, 2 req, 263 ms     | 0.00 MB, 1 req, 108 ms  | 8 KB        | 95       |                                              |
| z13 downtown, San Francisco          | lod         | 1.39 MB, 24 req, 162 ms    | 0.46 MB, 7 req, 137 ms  | 627 KB      | 16,948   | LOD f13                                      |
| z13 downtown, San Francisco          | lod_tiledir | 0.54 MB, 8 req, 81 ms      | 0.46 MB, 7 req, 71 ms   | 627 KB      | 16,948   | directory, LOD f13                           |
| z13 downtown, San Francisco          | cogp        | 1.49 MB, 111 req, 1527 ms  | 0.00 MB, 0 req, 71 ms   | 525 KB      | 16,963   | level 9, 16969 rows read                     |
| z13 downtown, San Francisco          | pmtiles     | 0.22 MB, 2 req, 409 ms     | 0.21 MB, 1 req, 162 ms  | 483 KB      | 7,855    |                                              |
| z13 downtown, Los Angeles            | lod         | 1.09 MB, 26 req, 107 ms    | 0.32 MB, 7 req, 88 ms   | 381 KB      | 10,632   | LOD f13                                      |
| z13 downtown, Los Angeles            | lod_tiledir | 0.42 MB, 8 req, 57 ms      | 0.32 MB, 7 req, 72 ms   | 381 KB      | 10,632   | directory, LOD f13                           |
| z13 downtown, Los Angeles            | cogp        | 1.34 MB, 116 req, 1538 ms  | 0.00 MB, 0 req, 60 ms   | 333 KB      | 10,611   | level 9, 10647 rows read                     |
| z13 downtown, Los Angeles            | pmtiles     | 0.15 MB, 2 req, 325 ms     | 0.14 MB, 1 req, 107 ms  | 315 KB      | 5,091    |                                              |
| z13 rural, Broken Bow NE             | lod         | 0.44 MB, 18 req, 28 ms     | 0.15 MB, 6 req, 17 ms   | 149 KB      | 2,131    | LOD f13                                      |
| z13 rural, Broken Bow NE             | lod_tiledir | 0.19 MB, 7 req, 25 ms      | 0.15 MB, 6 req, 18 ms   | 149 KB      | 2,131    | directory, LOD f13                           |
| z13 rural, Broken Bow NE             | cogp        | 1.05 MB, 109 req, 1462 ms  | 0.00 MB, 0 req, 38 ms   | 146 KB      | 2,131    | level 9, 2132 rows read                      |
| z13 rural, Broken Bow NE             | pmtiles     | 0.06 MB, 2 req, 285 ms     | 0.05 MB, 1 req, 168 ms  | 85 KB       | 1,042    |                                              |
| z13 empty, Nebraska Sandhills        | lod         | 0.25 MB, 12 req, 11 ms     | 0.00 MB, 0 req, 2 ms    | 0 KB        | 0        | LOD f13                                      |
| z13 empty, Nebraska Sandhills        | lod_tiledir | 0.04 MB, 1 req, 9 ms       | 0.00 MB, 0 req, 0 ms    | 0 KB        | 0        | directory, LOD f13                           |
| z13 empty, Nebraska Sandhills        | cogp        | 0.81 MB, 62 req, 858 ms    | 0.00 MB, 0 req, 18 ms   | 0 KB        | 0        | level 9, 0 rows read                         |
| z13 empty, Nebraska Sandhills        | pmtiles     | 0.01 MB, 1 req, 94 ms      |                         |             |          | no tile: empty                               |
| z14 row-group edge, Chicago          | lod         | 0.79 MB, 28 req, 87 ms     | 0.19 MB, 8 req, 67 ms   | 279 KB      | 8,868    | LOD f13                                      |
| z14 row-group edge, Chicago          | lod_tiledir | 0.29 MB, 9 req, 44 ms      | 0.19 MB, 8 req, 57 ms   | 279 KB      | 8,868    | directory, LOD f13                           |
| z14 row-group edge, Chicago          | cogp        | 1.16 MB, 115 req, 1491 ms  | 0.00 MB, 0 req, 48 ms   | 267 KB      | 8,872    | level 10, 8877 rows read                     |
| z14 row-group edge, Chicago          | pmtiles     | 0.18 MB, 2 req, 353 ms     | 0.17 MB, 1 req, 118 ms  | 487 KB      | 9,020    |                                              |
| z15 street, Manhattan                | lod         | 0.37 MB, 18 req, 25 ms     | 0.07 MB, 3 req, 16 ms   | 50 KB       | 1,098    | LOD f15                                      |
| z15 street, Manhattan                | lod_tiledir | 0.21 MB, 4 req, 20 ms      | 0.07 MB, 3 req, 9 ms    | 50 KB       | 1,098    | directory, LOD f15                           |
| z15 street, Manhattan                | cogp        | 1.26 MB, 122 req, 2250 ms  | 0.00 MB, 0 req, 40 ms   | 47 KB       | 1,098    | level 11, 1099 rows read                     |
| z15 street, Manhattan                | pmtiles     | 0.10 MB, 2 req, 301 ms     | 0.09 MB, 1 req, 112 ms  | 204 KB      | 1,037    | overzoomed from z14                          |
| z15 street, Denver suburbs           | lod         | 0.32 MB, 18 req, 22 ms     | 0.04 MB, 4 req, 11 ms   | 18 KB       | 351      | LOD f15                                      |
| z15 street, Denver suburbs           | lod_tiledir | 0.15 MB, 5 req, 14 ms      | 0.04 MB, 4 req, 8 ms    | 18 KB       | 351      | directory, LOD f15                           |
| z15 street, Denver suburbs           | cogp        | 1.40 MB, 144 req, 5602 ms  | 0.00 MB, 0 req, 42 ms   | 17 KB       | 350      | level 11, 354 rows read                      |
| z15 street, Denver suburbs           | pmtiles     | 0.06 MB, 2 req, 316 ms     | 0.05 MB, 1 req, 110 ms  | 96 KB       | 333      | overzoomed from z14                          |
| z16 street, San Francisco            | lod         | 0.35 MB, 16 req, 27 ms     | 0.03 MB, 3 req, 15 ms   | 7 KB        | 148      | LOD f15                                      |
| z16 street, San Francisco            | lod_tiledir | 0.13 MB, 4 req, 15 ms      | 0.03 MB, 3 req, 6 ms    | 7 KB        | 148      | directory, LOD f15                           |
| z16 street, San Francisco            | cogp        | 0.98 MB, 103 req, 1403 ms  | 0.00 MB, 0 req, 34 ms   | 6 KB        | 148      | level 12, 154 rows read                      |
| z16 street, San Francisco            | pmtiles     | 0.13 MB, 2 req, 342 ms     | 0.12 MB, 1 req, 117 ms  | 269 KB      | 147      | overzoomed from z14                          |
| z17 beyond finest LOD, San Francisco | lod         | 0.35 MB, 16 req, 20 ms     | 0.07 MB, 3 req, 9 ms    | 1 KB        | 15       | primary WKB geometry                         |
| z17 beyond finest LOD, San Francisco | lod_tiledir | 0.35 MB, 16 req, 18 ms     | 0.07 MB, 3 req, 7 ms    | 1 KB        | 15       | primary WKB geometry                         |
| z17 beyond finest LOD, San Francisco | cogp        | 0.96 MB, 95 req, 1359 ms   | 0.00 MB, 0 req, 30 ms   | 1 KB        | 15       | level 12, 15 rows read                       |
| z17 beyond finest LOD, San Francisco | pmtiles     | 0.13 MB, 2 req, 376 ms     | 0.12 MB, 1 req, 106 ms  | 269 KB      | 14       | overzoomed from z14                          |

Pans on one reader after open, four tiles eastward, totals (median time of 3
runs):

| pan                             | impl        | transferred | requests | time    | features |
| ------------------------------- | ----------- | ----------- | -------- | ------- | -------- |
| z13 San Francisco, 4 tiles east | lod         | 1.74 MB     | 42       | 266 ms  | 27,745   |
| z13 San Francisco, 4 tiles east | lod_tiledir | 0.90 MB     | 19       | 167 ms  | 27,745   |
| z13 San Francisco, 4 tiles east | cogp        | 2.01 MB     | 171      | 3131 ms | 27,761   |
| z13 San Francisco, 4 tiles east | pmtiles     | 0.34 MB     | 5        | 689 ms  | 11,661   |
| z15 Manhattan, 4 tiles east     | lod         | 0.49 MB     | 31       | 60 ms   | 3,047    |
| z15 Manhattan, 4 tiles east     | lod_tiledir | 0.33 MB     | 17       | 43 ms   | 3,047    |
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
so the z3 tile is 0.22 MB in one request and z5 is 0.29 MB in three; the
PMTiles archive has no tiles below z6. At z6 we read 0.17 MB to PMTiles'
0.11 MB, with 2,049 buildings to its 3,620; on the earlier file this tile
was 0.08 MB, and the difference is the refinement cost model, which with
smaller LOD leaves now judges the bbox reads not worth it there. Without an LOD the cost is orders
of magnitude higher: a plain sorted GeoParquet would read 7.7 MB to 5 GB per
tile. COGP is behind us on every tile but the z13 city tiles, where through
the page indexes it reads somewhat more bytes (1.49 MB to our 1.39 in San
Francisco, 1.34 to 1.09 in Los Angeles) in 4 to 5 times the requests;
through the directory we read a third. Elsewhere it reads 1.4 to 12 times our page-index
bytes in 5 to 42 times our requests, and its coarse tiles hold 10 to 184
buildings.

**Where it does not.** From z8 up, leaving out the two near-empty tiles, a
cold tile through the page indexes costs 2.5 to 7.3 times PMTiles' bytes
(median 4.0) and 3.5 to 15.5 times its requests. Through the directory the
same tiles cost 1.0 to 4.8 times PMTiles' bytes (median 2.4): 1.0 to 2.5
times on street tiles (z14 to z16) in 4 to 9 requests, 2.5 to 2.8 times on
the z13 city tiles in 8, and 4.8 times at z9 Atlanta in 21, where a
metro tile wants thousands of rows spread over many pages. At the
near-empty desert and Sandhills tiles PMTiles reads about 10 KB; the page
indexes read 0.21 to 0.25 MB and the directory 0.14 MB (desert) and one
40 KB directory read (Sandhills). Warm, the requests stay apart: revisiting
a tile costs 3 to 20 requests through either path (none for the empty tile),
the LOD data pages again, against one. A four-tile pan at z15 in Manhattan is
0.33 MB in 17 requests through the directory, 0.49 MB in 31 through the page
indexes, and 0.32 MB in 5 for PMTiles. Opening costs
0.94 MB in six requests, 1.81 MB in ten with the directory, against
PMTiles' 16 KB in one.

What a cold tile spends its bytes on depends on zoom. Through the page
indexes, at the z13 city tiles the bbox refinement reads are the largest
item, about 750 KB of San Francisco's 1.39 MB: a bbox page holds 7,000 rows
and the Hilbert order crosses the tile several times, so a quarter of the
candidate rows are in the tile. On street tiles (z14 to z17) the indexes
dominate: a tile touches three or four row groups whose bbox statistics
overlap at the edges, pays 27 KB of bbox column and offset indexes in each,
and finds rows in one; the prefix row group, whose bbox is the country,
costs every tile about 150 KB of bbox pages to learn it holds nothing there.
The LOD leaves' offset indexes (55 to 80 KB per row group, most of it the
geometry leaf's 1,700 pages) are fetched only for the row group that has
candidate rows. The directory replaces all of it with one read of about 40
to 140 KB.

### Tile directory

The tile directory is a Parquet table beside the file,
`buildings.parquet.tiledir.parquet`, built from the page indexes by
`geoparquet-lod tiledir` and used by the reader when given its URL. It maps
each tile at every zoom above the prefix to the page byte ranges it needs, so
a tile skips the bbox statistics, page indexes and refinement reads. It is
bound to one write of the file by size, footer length and footer hash, and
nothing in the file refers to it; without it, or with a stale one, the
reader goes through the page indexes and draws the same features. For this
file it is 212.5 MB in 3,400 row groups of about 64 KB, with an 878 KB
footer, and covers z7 to z16; the build takes about 40 minutes. Row groups
never span a zoom, so footer statistics pick one and a lookup is one range,
at the cost of the 878 KB footer read once per session; 75 large groups with page
indexes open for 30 KB, but a lookup is then three dependent round trips.
The builder holds an LOD's entries in memory and loops in Python, so large
polygons cost in proportion to their extent in tiles, and antimeridian,
wrapped-world and polar tiles are untested.

Reproduce with both files served locally with range support:

```bash
uv run geoparquet-lod tiledir buildings.parquet
cd js && npm run build
TILEDIR_URL=http://localhost:8097/buildings.parquet.tiledir.parquet \
  LOD_URL=http://localhost:8097/buildings.parquet \
  IMPLS=lod,lod_tiledir node test/compare.mjs
```

**Where this leaves the comparison.** On Parquet's own indexes a cold tile
costs a median 4.0 times PMTiles' bytes and 3.5 to 15.5 times its requests
from z8 up. Through the directory it costs a median 2.4 times the bytes, 1.0
to 2.5 times at street zooms, in 4 to 9 requests against 2, and a pan of
four street tiles the same bytes as PMTiles in 17 requests against 5 (1.5
times in 31 through the page indexes). That is after an open of 1.81 MB
against 16 KB. The floor is a data read per leaf per row
group a tile touches, so request parity with a format that stores each tile
as one blob is out of reach while tiles are assembled from column chunks. If
the map is the product and the data is secondary, build PMTiles. This layout
is for the case where the file is also the dataset: one upload that DuckDB,
GeoPandas and a map read alike, attributes that stay queryable and can be
appended without retiling, thinning that can see the attributes (show the
highest-risk buildings first, not a random sample), and exact geometry past
the finest zoom.

**Next steps, by effect.**

1. Shrink the opens: 0.94 MB for the file, nearly all its 922 KB footer,
   and 0.88 MB for the directory footer. The footer is 9,150 column chunks
   at about 100 bytes each: 257 KB of nested leaf paths, 126 KB of size
   statistics pyarrow cannot be told to omit, 77 KB of encoding statistics.
   Fewer, larger row groups shrink it in proportion but enlarge every
   per-tile index read; larger directory groups trade lookup bytes for
   footer bytes.
2. Build the directory in vectorized or compiled code, with bounded memory.
3. Retune the refinement cost model: with LOD leaves a third smaller it now
   skips the bbox reads at z5 and z6, which doubled the z6 tile's bytes.

The `lod` file is public; the demo above reads it directly with `?url=`:

```
https://carbonplan-ocr.s3.us-west-2.amazonaws.com/ocr-explore/geoparquet-overviews/lod/buildings.parquet
```
