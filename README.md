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
reading data. One nullable binary column per zoom band holds, for exactly the
rows shown at those zooms, the carried attributes and the geometry quantized
to that band's grid.

Which rows appear at which zoom is a *thinning function*. Three are built in:

| thinning | rule | good for |
| --- | --- | --- |
| `density_budget(60_000, where=...)` | keep a uniform fraction per zoom so the busiest tile holds at most N | points, small polygons |
| `size_threshold(pixels=4)` | show a feature once it spans N pixels | polygons, lines |
| `importance(order_by, per_tile)` | keep the top N per tile by an expression | named places, ranked facilities |

Combine them with `combine(...)`, or write your own `custom(fn)`.

## Quick start

```bash
uv pip install -e .
geoparquet-lod write buildings.parquet buildings.lod.parquet \
  --thinning "density:60000:risk > 0" --carry risk:f32 --polygons-from 13
geoparquet-lod validate buildings.lod.parquet
geoparquet-lod inspect buildings.lod.parquet
```

Or from Python:

```python
from geoparquet_lod import write, density_budget

write(
    "buildings.parquet", "buildings.lod.parquet",
    thinning=density_budget(60_000, where="risk > 0"),
    zooms=range(2, 17), polygons_from=13,
    carry=[("risk", "f32")],
)
```

## Measured

155.6 million US building footprints, 25.9 GB, read over plain S3 with no
CDN, one 512 px tile, cold reader:

| view | bytes | requests |
| --- | --- | --- |
| national, z3 | 0.34 MB | 8 |
| regional, z5 | 0.28 MB | 7 |
| city, z13 | 0.58 MB | 8 |
| street, z16 | 0.40 MB | 7 |

The equivalent PMTiles tiles are 0.24 MB and 0.10 MB in one request each.
Bytes include page-index reads; they are cached for the session.

## Status

Version 0.2.0 of the metadata. The layout is settled; the payload encodings
are ours and are the part a standard would replace. See `spec/SPEC.md`.
