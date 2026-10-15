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

## Ordinary overview tables

A companion table with one actual row per shown feature per level, holding
nested int32 geometry and source attributes, gives standard Parquet columns
without the sparse null rows of source-aligned overview columns. Across a
14-step Tokyo navigation route it transferred 7.98 MB in 60 requests against
4.26 MB in 25 for the packed layout, with cold national views within 25 ms of
each other and dense views over-fetching. It is a viable direction for a
standard encoding at the cost of requests; a source-aligned nested column
without that compaction took 70 s for the cold national view and is not.

## Modified COGP reader

Patching a COGP checkout to read packed overview pages through a spatial
directory, while keeping COGP's cumulative level semantics, removed most of its
per-tile requests. It did not reach this layout's transfer sizes because the
cumulative prefixes still read every coarser level. It suggests the page-level
access path can be added to COGP as an optional extension.
