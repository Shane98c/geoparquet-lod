# Design alternatives

Layouts and encodings measured in the same role as the current design, on the
Japan building dataset or a Tokyo subset of it. Each was built with the same
writer and read with the same reader as the design it was compared against, so
the numbers isolate one choice. They were measured on the reader revision of
the time and show direction, not current values.

## Coarse prefix versus one order

Storing the rows shown at coarse zooms in leading row groups of their own,
the rest after, gives a reader that knows nothing about the format the same
thing COGP's coarse-to-fine order gives it: QGIS and other sequential readers
draw those first row groups first, so a whole-extent view fills in coarse
before fine. On the Tokyo file a prefix through zoom 10 is 166,545 rows, 3.4%
of the table, in one leading row group. The cost falls on the indexed map
reader, because fine levels then live in both sections and a tile needs a
range from each. Same Tokyo tiles as the table below, cold geometry reads
with the current reader:

| Tile                      | One order      | Coarse prefix  | Next tile, one order / prefix |
| ------------------------- | -------------- | -------------- | ----------------------------- |
| z8 metro, Tokyo           | 0.53 MB, 2 req | 0.39 MB, 2 req | 0.01 MB, 1 / 0.00 MB, 1       |
| z11 city, Tokyo           | 0.29 MB, 2 req | 0.28 MB, 3 req | 0.16 MB, 1 / 0.16 MB, 2       |
| z13 downtown, Shibuya     | 0.36 MB, 2 req | 0.36 MB, 3 req | 0.25 MB, 1 / 0.25 MB, 2       |
| z15 street, Tokyo station | 0.04 MB, 2 req | 0.06 MB, 4 req | 0.02 MB, 1 / 0.02 MB, 2       |

Feature counts are identical. The prefix saves bytes at the coarse zoom it
serves alone, costs one extra request per tile at city zooms and two at
street zooms, and doubles the requests of every neighbouring tile.

The benefit to readers that do not know the format is measured in QGIS 4.2
(GDAL 3.12), rendering each file headlessly with a parallel render job and
sampling the partial image every 100 ms, as the canvas preview does. Both
files finish in the same time; what differs is how much of the final picture
is on screen early (median of three alternating runs, fraction of the final
painted pixels):

| Extent, file           | 0.5 s | 1 s  | 2 s  | 5 s  | Done   |
| ---------------------- | ----- | ---- | ---- | ---- | ------ |
| Whole Tokyo, one order | 0.02  | 0.04 | 0.09 | 0.23 | 29.9 s |
| Whole Tokyo, prefix    | 0.21  | 0.44 | 0.48 | 0.56 | 29.9 s |
| City, one order        | 0.12  | 0.22 | 0.50 | 1.00 | 5.0 s  |
| City, prefix           | 0.39  | 0.48 | 0.63 | 1.00 | 5.0 s  |

With one order QGIS fills the map region by region along the curve; with the
prefix the coarse selection covers the whole extent within the first
second, because GDAL delivers row groups in file order and the selection is
the first group. The layout keeps one order for now; the prefix is the lever
if sequential readers matter more than a request per tile, and the spatial
index works either way. `benchmarks/standard-overviews/write-tokyo.py` builds
both Tokyo files, the prefix from the writer at `0a06e75^`, which has the
option, and `qgis-progressive.py` beside it reproduces the measurement.

Without the index, removing the prefix initially doubled the coarse Tokyo
tile's bytes, because a coarse overview is sparse and a fixed row cap left
pages holding few values. The page writer therefore scales each LOD chunk's
row cap by its density, targeting 2,048 present values per page, up to
262,144 analytical rows.

## Page size and CPU workers

With tile-aligned pages, 64 KiB uncompressed overview pages against 16 KiB cut
the cold Tokyo city viewport from 256 to 209 ms with two CPU workers, or 195 ms
with four, and a cached repeat from 125 to 69 ms. Two workers are the default
because a third and fourth each cost a decoder and projection cache in memory;
devices reporting at most two cores render serially.

## Separate attribute columns

Carried attributes could live in ordinary nullable LIST columns beside a
geometry-only payload, one list slot per own row and boundary copy, so a style
reads only the attributes it needs. On the 4.9-million-row Tokyo subset this
saved modest bytes for geometry-only views but made styled views slower: the
companion pages add requests and decode work that outweighs the saving. The
layout keeps attributes packed with the geometry, and styles that need other
source columns project them by row id on demand.

## Shared detail levels

Serving z14 through z16 from one z16-grid payload shrinks the Japan file by
10.7% (817 MB) but grows cold city reads at z14 and z15 by 26 to 33% in bytes,
because the finer geometry and the copies for other zooms ride along. Zooming
into an already loaded place becomes free at z16. The writer supports this when
the full-detail payload is small (`single_lod_bytes`); the evaluated Japan file
keeps separate detail levels because cold transfer matters more than storage.

## Standard geometry encodings

The packed rows are a private byte format. On a matched 542,728-feature sample
of already-quantized Japan overviews, the same geometry as standard GeoArrow
float64 multipolygons takes **2.16×** the compressed bytes and WKB **2.60×**,
and each takes about **2.1×** the decoding CPU; MVT encoding costs the same.
Quantized nested int32 coordinates with Parquet delta encoding, the shape COGP's
`quantized_geoarrow` uses, approach the packed size at dense levels. WKB has a
concrete benefit the others lack: DuckDB spatial recognizes it and runs spatial
SQL on the overviews without a custom decoder.

## Standard overview table with tile-sized row groups

The closest standard-Parquet equivalent of the packed rows, built from the
same Tokyo file so the two hold identical overviews: one table with a row per
shown feature per level and a row per boundary copy tagged with the tile it
serves, columns `level, row, tile, geometry, height, class, key`, geometry as
nested int32 x/y on the level's grid (COGP's `quantized_geoarrow` shape), in
Hilbert order with row groups cut at the level's tile boundaries and at 4,096
rows. A tile then reads whole row groups, each one contiguous byte range, and
a directory over the row groups replaces the 11 MB footer. The reader's
coalescing policy is applied to both; the packed numbers are the real reader
on the local file, the standard numbers a simulation from the footer with
membership checked equal to the packed reader on every tile.
`benchmarks/standard-overviews/` holds the Tokyo build, the converter, the
simulator, and the results.

4,881,955 Tokyo buildings, zooms 8 to 16, cold geometry-only reads after a
16 KiB open, then the tile one column east on the same reader:

| Tile                      | Packed cold                   | Standard cold  | Packed next    | Standard next  |
| ------------------------- | ----------------------------- | -------------- | -------------- | -------------- |
| z8 metro, Tokyo           | 0.53 MB, 2 req, 102,403 feat. | 0.81 MB, 2 req | 0.01 MB, 1 req | 0.01 MB, 1 req |
| z11 city, Tokyo           | 0.29 MB, 2 req, 24,086 feat.  | 0.41 MB, 2 req | 0.16 MB, 1 req | 0.24 MB, 1 req |
| z12 city, Tokyo           | 0.36 MB, 2 req, 29,342 feat.  | 0.51 MB, 2 req | 0.32 MB, 1 req | 0.47 MB, 1 req |
| z13 downtown, Shibuya     | 0.36 MB, 2 req, 29,877 feat.  | 0.51 MB, 2 req | 0.25 MB, 1 req | 0.38 MB, 1 req |
| z13 suburbs, Kawasaki     | 0.26 MB, 2 req, 20,283 feat.  | 0.36 MB, 2 req | 0.34 MB, 1 req | 0.52 MB, 1 req |
| z15 street, Tokyo station | 0.04 MB, 2 req, 229 feat.     | 0.03 MB, 2 req | 0.02 MB, 1 req | 0.03 MB, 1 req |
| z15 street, Shinjuku      | 0.05 MB, 2 req, 1,107 feat.   | 0.04 MB, 2 req | 0.02 MB, 1 req | 0.03 MB, 1 req |

Request counts are identical. The standard table costs 40 to 55% more bytes
at city zooms; at street zooms it costs slightly fewer on a cold read and 29%
more on the next tile; and it stores 344 MB against 219 MB for the packed
overview columns. Styled reads cost the same as geometry-only in the packed
rows and at most 3% more in the standard table, apart from its z8 next tile
at 14% more: with 4,096-row groups the attribute chunks mostly sit inside the
gaps the coalescer bridges anyway. The table is ordinary Parquet; DuckDB
queries it and reads the nested coordinates directly, though no tool treats
them as geometry without the level's scale and offset. Not measured: decode CPU,
which the standard-geometry ablation above puts at about twice the packed
rows, and larger datasets, where the 15,345 row groups here become hundreds
of thousands and the directory must carry them.

## Modified COGP reader

Patching a COGP checkout to read packed overview pages through a spatial
directory, while keeping COGP's cumulative level semantics, removed most of its
per-tile requests. It did not reach this layout's transfer sizes because the
cumulative prefixes still read every coarser level. It suggests the page-level
access path can be added to COGP as an optional extension.
