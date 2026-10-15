# Design alternatives

Layouts and encodings measured in the same role as the current design, on the
Japan building dataset or a Tokyo subset of it. Each was built with the same
writer and read with the same reader as the design it was compared against, so
the numbers isolate one choice. They were measured on the reader revision of
the time and show direction, not current values.

## Coarse prefix versus one order

A construction that stores rows shown at coarse zooms in leading row groups
of their own lets a coarse tile touch a small part of the file. With the
spatial directory this stops paying: a single Hilbert order reaches **two cold
requests and one neighbour request** on every sampled Japan tile, matching
PMTiles, while the prefix version needed three or four. The prefix also made a
fine tile that straddled the two sections need two data requests. The layout
now has one order and no prefix.

Removing the prefix initially doubled the coarse Tokyo tile's bytes, because a
coarse overview is sparse and a fixed row cap left pages holding few values.
The page writer therefore scales each LOD chunk's row cap by its density,
targeting 2,048 present values per page, up to 262,144 analytical rows.

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
`benchmarks/standard-overviews/` holds the converter, the simulator, and the
results.

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
at city zooms, slightly fewer at street zooms, and 344 MB against 219 MB for
the packed overview columns. Styled reads cost the same as geometry-only in
both: with 4,096-row groups the attribute chunks sit inside the gaps the
coalescer bridges anyway. The table is ordinary Parquet; DuckDB queries it
and reads the nested coordinates directly, though no tool treats them as
geometry without the level's scale and offset. Not measured: decode CPU,
which the standard-geometry ablation above puts at about twice the packed
rows, and larger datasets, where the 15,345 row groups here become hundreds
of thousands and the directory must carry them.

## Modified COGP reader

Patching a COGP checkout to read packed overview pages through a spatial
directory, while keeping COGP's cumulative level semantics, removed most of its
per-tile requests. It did not reach this layout's transfer sizes because the
cumulative prefixes still read every coarser level. It suggests the page-level
access path can be added to COGP as an optional extension.
