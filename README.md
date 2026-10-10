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
reading data. One nullable binary column per LOD, a zoom or a run of zooms, holds for
exactly the rows shown there the carried attributes and the geometry on that
LOD's grid.

Which rows appear at which zoom is a *thinning function*, one shape:

```python
def thin(rows: pa.Table, zoom: int, ctx: Context) -> pa.Table
```

The writer calls it once per zoom from fine to coarse, each time with the
rows kept at the zoom just finer, and it returns the subset to keep. Nesting
is guaranteed by construction. `rows` carries `id`, centroid, bbox diagonal, a
stable per-row uniform `u`, and the carried attributes; returning a carried
column overrides its value in that zoom's payload, which is how a cluster
passes its count along. Built-ins are a few lines each and meant to be copied:

| thinning | rule | good for |
| --- | --- | --- |
| `drop_rate(2.5)` | each zoom coarser keeps 1/rate, uniformly, so relative density survives (tippecanoe `-r`); the default | points, small polygons |
| `size_threshold(4)` | keep a feature while it spans 4 pixels | polygons, lines |
| `importance('population', 300)` | keep the top N per tile by a column | named places, ranked facilities |
| `cluster(8, count='count')` | one representative per 8 px cell carrying how many it stands for | dense points |
| `density_budget(60_000)` | cap the busiest tile; flattens dense places | hard byte ceilings |
| `only('risk > 0', inner)` | restrict eligibility before `inner` | partial datasets |
| `combine(a, b)` | keep what any part keeps | size plus sample, etc. |

There are no other knobs. Anything the built-ins cannot express is a short
function of a table.

Zooms are derived from the data unless given: the coarsest zoom is where the
dataset's extent fits a tile, footprints start where the median feature spans
a pixel, and the finest zoom is where the median vertex spacing reaches a
pixel. The writer logs the guess; pass `zooms=` or `polygons_from=` to override.
On Overture buildings that is zooms 0 to 14 with footprints from 12; on US
counties, footprints at every zoom; on census tracts, footprints from 4.

## Geometry

Points, lines and polygons, one family per file. Below the shape zoom every
family is drawn as a point; from it up, lines and polygons carry a quantized
copy per LOD, one column serving two zooms. When the full-detail shapes of
the whole dataset are small (16 MB by default) a single LOD serves every zoom
instead of copies, which is what happens for things like counties.

## Quick start

```bash
uv pip install -e .
geoparquet-lod write buildings.parquet buildings.lod.parquet \
  --thinning size:4+drop:2.5 --where "risk > 0" --carry risk:f32
geoparquet-lod validate buildings.lod.parquet
geoparquet-lod inspect buildings.lod.parquet
```

Or from Python:

```python
from geoparquet_lod import write, drop_rate, only

write(
    "buildings.parquet", "buildings.lod.parquet",
    thinning=only("risk > 0", drop_rate(2.5)),
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
