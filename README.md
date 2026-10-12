# geoparquet-lod

A draft layout for GeoParquet files that serve a zoomable web map directly,
with a reference writer and reader. "LOD" is level of detail. Every feature
is stored once with its exact geometry; a few extra columns hold what a
renderer needs at each zoom; a reader that does not know the layout sees
ordinary GeoParquet.

**Status: experimental, draft 0.1.0.** One writer and one reader exist, both
in this repository, and the layout may still change in ways that need a
rewrite. The goal is per-tile reads comparable to a pre-cut vector tile
archive from one file that stays queryable; see [Measured](#measured) for
how close it is today.

- `spec/SPEC.md`: the layout and metadata contract
- `python/`: writer, validator and CLI (`geoparquet-lod write | validate | inspect | tiledir`)
- `js/`: a browser reader and a MapLibre tile protocol
- `examples/`: thinning functions for common datasets
- `docs/benchmarks.md`: measurements against PMTiles, COGP and plain GeoParquet

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
same fill layer, as tippecanoe draws it. `rows` carries `id`, centroid, bbox
diagonal, area, a per-row uniform `u` hashed from the geometry, and the
carried attributes; returning a carried column overrides its value in that
zoom's LOD, which is how a cluster passes its count along. Built-ins are a
few lines each and meant to be copied:

| thinning                        | rule                                                                                                                         | good for                                           |
| ------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------- |
| `drop_rate(2.5)`                | each zoom coarser keeps 1/rate, uniformly, so relative density survives (tippecanoe `-r`); the default                       | points, small polygons                             |
| `size_threshold(4)`             | keep a feature while it still spans 4 pixels                                                                                 | large polygons and lines first                     |
| `tiny_polygons(0.5)`            | inspired by tippecanoe's tiny-polygon reduction: small areas accumulate and one half-pixel square is kept per square of area | area-true coverage; nearly empty at national scale |
| `importance('population', 300)` | keep the top N per tile by a column                                                                                          | named places, ranked facilities                    |
| `cluster(8, count='count')`     | one representative per 8 px cell carrying how many it stands for                                                             | dense points                                       |
| `density_budget(60_000)`        | cap the busiest tile with a uniform sample; flattens dense places                                                            | bounding the largest tiles                         |
| `drop_smallest(40_000)`         | a tile over budget keeps its largest by area (tippecanoe `--drop-smallest-as-needed`)                                        | buildings, after `tiny_polygons`                   |
| `only('risk > 0', inner)`       | restrict eligibility before `inner`                                                                                          | partial datasets                                   |
| `combine(a, b)`                 | keep what any part keeps                                                                                                     | size plus sample, etc.                             |
| `chain(a, b)`                   | apply `b` to what `a` kept                                                                                                   | `tiny_polygons` then `drop_smallest`               |

Anything the built-ins cannot express is a short function of a table. The
writer's other options (zooms, the complete zoom, prefix zoom, row group and
page sizes, compression level) shape the file, not the row selection.

Zooms are derived from the data unless given: the coarsest zoom is where the
dataset's extent fits a tile, every feature is present from the zoom where
the median feature spans a pixel, and the finest zoom is where the median
vertex spacing spans four pixels. The writer logs the guess; pass `zooms=` or
`complete_from=` to override. On Overture buildings for a city that is zooms
2 to 16, complete from 13; on US counties, complete at every zoom; on census
tracts, complete from 4.

### Attributes

Source columns are kept as they are and stay queryable. Separately, the
writer copies the attributes a renderer styles with into every LOD struct,
chosen with `carry=`. Below the complete zoom only a fraction of rows is
shown, and the primary attribute columns hold every row, so a tile reading
them would fetch pages that are mostly unwanted rows; the LOD copies are
dense in exactly the shown rows. From the complete zoom up the copies
duplicate the primary values, which costs file size but nothing per tile.
Carry a few scalar columns; everything else is read per feature on demand
with `readRow`. Adding a new styling attribute means rewriting the LOD
structs. Thinning functions can read any source column via `columns=`
without carrying it.

## Geometry

Points, lines and polygons, one family per file, and a family keeps its
geometry type at every zoom, as tippecanoe does. Lines and polygons carry a
quantized copy per LOD: one per zoom below the complete zoom, where the
thinning changes the row set, and one per two zooms above it. Each copy is
nested lists of interleaved int32 `[x, y]` pairs on the LOD's pixel grid,
decoded as `offset + scale * integer`. The transform and grid are COGP's
`quantized_geoarrow`; the physical layout is not, since COGP stores a
`struct<x, y>` per vertex, has no point family and no attribute fields, so a
COGP overview reader cannot decode these columns. Nothing in the file is a
private byte format. When the full-detail shapes of the whole dataset are
small (16 MB by default) a single LOD serves every zoom instead of copies,
which is what happens for things like counties. Finer than the finest zoom
the reader draws the exact primary geometry, so full detail is always
reachable and costs nothing to store.

## Install

Python 3.12 and Node 22 or later. From a clone:

```bash
uv sync --group dev          # the writer, validator and CLI, with test deps
cd js && npm install && npm run build && cd ..
```

Neither package is on PyPI or npm yet; install the Python package with
`uv pip install -e .` or `pip install -e .`, and the JS package from the
path or a git URL (`npm install ../geoparquet-lod/js`).

## Quick start

Pull a city of buildings with the [Overture CLI](https://docs.overturemaps.org/getting-data/overturemaps-py/)
and write it. The example guesses zooms from the data and carries height and
class:

```bash
pip install overturemaps
overturemaps download --bbox=-105.1,39.6,-104.8,39.9 -f geoparquet --type=building -o overture_denver.parquet
uv run python examples/overture_buildings.py overture_denver.parquet overture_denver.lod.parquet
uv run geoparquet-lod validate overture_denver.lod.parquet
uv run geoparquet-lod inspect overture_denver.lod.parquet
```

The same from the CLI with a thinning spec, or from Python:

```bash
uv run geoparquet-lod write buildings.parquet buildings.lod.parquet \
  --thinning size:4+drop:2.5 --where "risk > 0" --carry risk
```

```python
from geoparquet_lod import write, drop_rate, only

write(
    "buildings.parquet", "buildings.lod.parquet",
    thinning=only("risk > 0", drop_rate(2.5)),
    carry=["risk"],
)
```

The input is any GeoParquet with a WKB geometry column in OGC:CRS84 or
EPSG:4326. Writing the 155.6 million building national file takes about 25
minutes on a laptop with 12 workers.

### In a MapLibre app

```js
import maplibregl from 'maplibre-gl'
import { registerProtocol, tileUrl, prefetch, readRow } from 'geoparquet-lod/maplibre'

const url = 'https://example.com/buildings.lod.parquet'
prefetch(url) // optional: opens the file in the worker before the map exists
registerProtocol(maplibregl)

map.addSource('buildings', {
  type: 'vector',
  tiles: [tileUrl({ url, layer: 'buildings', properties: ['risk'] })],
})
map.addLayer({ id: 'fill', type: 'fill', source: 'buildings', 'source-layer': 'buildings' })

// the MVT feature id is the file row; read any source columns for it
const row = await readRow(url, feature.id, ['name', 'risk'])
```

Tiles are read and encoded in a Web Worker. The server must answer HTTP
range requests with CORS, which plain S3 does. `tileUrl` also takes
`pointLayer` for point datasets and `tiledir` for a tile directory sidecar.
Opening a file reads its last 1 MB in one request, which holds the footer
of files up to a few hundred row groups, alongside a HEAD for the size.

### Demo

`demo/index.html` is a MapLibre page that reads a file straight from a URL
and shows which LOD each zoom is drawing. Click a feature to read its row.

```bash
cd js && npm run demo
```

That bundles the reader into `demo/dist`, serves the repo at
`http://localhost:8091`, and opens the page pointed at `buildings.lod.parquet`
in the repo root; pass `?url=` for another file, such as the
`overture_denver.lod.parquet` written above. The page must be served; module
scripts do not load from `file://`.

## Tile directory

An optional sidecar, `<file>.tiledir.parquet`, maps each tile at every zoom
above the prefix to the page byte ranges it needs, so a tile skips the bbox
statistics, page indexes and refinement reads. Build it with the file or
beside an existing one, then give the reader its URL:

```sh
uv run geoparquet-lod write src.parquet out.parquet --tile-directory
uv run geoparquet-lod tiledir out.parquet
```

```js
tileUrl({ url, tiledir: url + '.tiledir.parquet' })
```

It is bound to one write of the file by size, footer length and footer hash,
and nothing in the file refers to it; without it, or with a stale one, the
reader warns and reads through the page indexes, drawing the same features.
The builder reads the bbox values and each LOD struct of every row group to
decide tile membership, holds one LOD's entries in memory and loops in
Python, so it is slow on large files (40 minutes for the national file) and
large polygons cost in proportion to their extent in tiles. The table has one
row per tile with entries, sorted by `(zoom, tile)`: `zoom` int8, `tile` int64
(Morton id on the zoom's grid) and `pages`, a list of int64 sextuples
`[row group, leaf, row start, row count, byte start, byte length]`, in row
groups of about 64 KB that never span a zoom.

## Measured

The full method and tables are in [docs/benchmarks.md](docs/benchmarks.md):
the same 155.6 million US building footprints served as this layout, as
Kanahiro's COGP, as the production PMTiles archive, and as plain sorted
GeoParquet. From a cold reader on the 24.2 GB national file:

- A national view (z3) is 0.22 MB in one request; the PMTiles archive has no
  tiles below z6.
- Street tiles (z15 to z16) are 0.3 to 0.4 MB in 16 to 18 requests through
  the page indexes, 0.13 to 0.21 MB in 4 or 5 through the tile directory,
  against 0.06 to 0.13 MB in 2 for PMTiles.
- City tiles (z13) are 1.1 to 1.4 MB; COGP reads 1.3 to 1.5 MB there in four
  to five times the requests, and more everywhere else.
- Opening costs 0.94 MB, nearly all footer, against 16 KB for PMTiles.
- A plain sorted GeoParquet would read 8 MB to 5 GB per tile.

A second comparison in the same document puts this layout, COGP, PMTiles and
Esri's Spatially Optimized Parquet side by side on Esri's own 53.7 million
Japan buildings, with feature counts beside the bytes, since the four thin
so differently that bytes alone mislead. `demo/compare.html` draws any of
the four from URLs with a byte and request tally per pane.

The floor is one data read per leaf per row group a tile touches, so request
parity with a format that stores each tile as one blob is out of reach. If
the map is the product and the data is secondary, build PMTiles. This layout
is for the case where the file is also the dataset: one upload that DuckDB,
GeoPandas and a map read alike, attributes that stay queryable, thinning
that can see the attributes, and exact geometry past the finest zoom.

## Limitations

- Web Mercator zooms at 512 px tiles, geographic coordinates only.
- The validator checks metadata, schema shapes, covering statistics, page
  indexes and a sample decode; it does not verify sort order, LOD
  membership nesting, or finest-LOD completeness.
- Large polygons spanning many row groups, lines, and datasets that cannot
  be thinned have not been measured.
- Page size is global, so the 16 KB pages chosen for tiles also apply to the
  source columns, and the footer grows with LOD count times row groups.
- One writer and one reader; no conformance suite.

## Prior work

- [GeoParquet PR 305](https://github.com/opengeospatial/geoparquet/pull/305)
  and [cloud-optimized-geoparquet](https://github.com/Kanahiro/cloud-optimized-geoparquet)
  by Kanahiro: levels as row-group prefixes, geometry stored once, analysis
  readers unaffected. Our `prefix` is one such level, the LOD geometry
  transform is its `quantized_geoarrow`, and the tile encoder uses
  `@cogp/reader`'s MVT writer.
- [GeoParquet PR 299](https://github.com/opengeospatial/geoparquet/pull/299)
  by mmgeorge: per-resolution geometry columns declared in metadata. Our LOD
  columns are that idea with nullability carrying the row selection.
- [Esri Spatially Optimized Parquet](https://github.com/Esri/spatially-optimized-parquet)
  ([spec](https://github.com/Esri/spatially-optimized-parquet/blob/main/spec/display-optimization.md)):
  XZ spatial clustering, page indexes and one multiscale geometry column per
  level, with no row thinning.
- [tippecanoe](https://github.com/felt/tippecanoe): the drop-rate and
  zoom-guessing rules are its `-r` and `-zg`.

`spec/SPEC.md` says where this layout differs and why. Whether it could
become a GeoParquet extension is open.

## License

MIT.
