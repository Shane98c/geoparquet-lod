# Writer and reader guide

The [README](../README.md) covers the recommended workflow and current
measurements. This guide explains how the writer chooses and encodes overviews,
how to install and run the packages, and what the reader APIs return.

## How it works

Rows are sorted once along a Hilbert curve over the Web Mercator world
square in large row groups, so a tile's rows are one contiguous run and a
city tile touches one or two row groups. A float32 bbox covering with page
statistics lets a reader pick pages without reading geometry. Each LOD is a
nullable binary child of the required `lod` struct. Present values pack whole
quantized geometry, carried attributes, and boundary copies; the spatial index
selects these pages directly.

Which rows appear at which zoom is a _thinning function_, one shape:

```python
def thin(rows: pa.Table, zoom: int, ctx: Context) -> pa.Table
```

The writer calls it once per zoom with every row, as tippecanoe builds each
zoom from the whole dataset, and it returns the rows to show there; the
finest zoom always shows every row. Membership need not nest across zooms.
Shapes stay shapes at every zoom on a quarter-pixel grid; one too small for
it is stored as a square on its centroid's cell, so zoomed out a thinned
building set reads as a faint density texture in the same fill layer, as
tippecanoe draws it. `rows` carries `id`, centroid, bbox diagonal, area,
vertex count, a per-row uniform `u` hashed from the geometry, and the
carried attributes; `Context` converts area and size to screen pixels at
the row's latitude. Returning a carried column overrides its value in that
zoom's LOD, which is how a cluster passes its count along. Built-ins are a
few lines each and meant to be copied:

| thinning                        | rule                                                                                                                                     | good for                                   |
| ------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------ |
| `drop_rate(2.5)`                | each zoom coarser keeps 1/rate, uniformly, so relative density survives (tippecanoe `-r`); the default                                   | points, small polygons                     |
| `size_threshold(4)`             | keep a feature while it still spans 4 pixels                                                                                             | large polygons and lines first             |
| `tiny_polygons(0.25)`           | tippecanoe's tiny-polygon reduction: areas under a quarter-pixel square accumulate per tile and one square is kept per square's worth    | area-true coverage of buildings zoomed out |
| `importance('population', 300)` | keep the top N per tile by a column                                                                                                      | named places, ranked facilities            |
| `cluster(8, count='count')`     | one representative per 8 px cell carrying how many it stands for                                                                         | dense points                               |
| `density_budget(60_000)`        | cap the busiest tile with a uniform sample; flattens dense places                                                                        | bounding the largest tiles                 |
| `drop_smallest(600_000)`        | one area threshold per zoom, raised until the busiest tile's estimated bytes fit; inspired by tippecanoe, with different cost estimation | buildings, before `tiny_polygons`          |
| `only('risk > 0', inner)`       | restrict eligibility before `inner`                                                                                                      | partial datasets                           |
| `combine(a, b)`                 | keep what any part keeps                                                                                                                 | size plus sample, etc.                     |
| `chain(a, b)`                   | apply `b` to what `a` kept                                                                                                               | `drop_smallest` then `tiny_polygons`       |

Anything the built-ins cannot express is a short function of a table. The
writer's other options (row group and page sizes, compression level) shape
the file, not the row selection.

Zooms are derived from the data unless given: the coarsest zoom is one
coarser than where the dataset's whole extent spans about a pixel, the base
zoom a rate-based thinning counts from is where the median feature spans a
pixel, and the finest zoom is where the median vertex spacing spans four
pixels. The writer logs the guess and
records which zooms actually keep every row; pass `zooms=` or
`complete_from=` to override. On Overture buildings for a city that is zooms
2 to 16, complete from 13; on US counties, complete at every zoom; on census
tracts, complete from 4.

### Attributes

Source columns are kept as they are and stay queryable. Separately, the
writer copies the attributes a renderer styles with into every LOD payload,
chosen with `carry=`. Below the complete zoom only a fraction of rows is
shown, and the primary attribute columns hold every row, so a tile reading
them would fetch pages that are mostly unwanted rows; the LOD copies are
dense in exactly the shown rows. From the complete zoom up the copies
duplicate the primary values, which costs file size but nothing per tile.
Carry a few frequently used scalar columns. Other attributes can be read with
`readRow`, batched with `readColumns`, or requested in `renderTile` properties
on demand; the first read of a source column is substantially slower than a
carried attribute, and a file rewrite is the way to make one fast. Thinning
functions can read any source column via `columns=` without carrying it.

## Geometry

Points, lines and polygons, one family per file, and a family keeps its
geometry type at every zoom, as tippecanoe does. Lines and polygons carry a
quantized copy per LOD: one per zoom below the complete zoom, where the
thinning changes the row set, and one per two zooms from it up. Each LOD is
one binary column: a row's value packs its geometry on the LOD's pixel grid
as varint ring steps, its carried attributes, and copies of neighbouring
features that reach into its tile, so a tile can read a contiguous range of
that column. Page, directory, and row-group boundaries can add requests. Grid
coordinates decode as `offset + scale * integer`; the transform and grid are
COGP's `quantized_geoarrow`, the byte format is this layout's own, a page of
spec. When the full-detail shapes of the whole dataset are small (16 MB by
default) a single LOD serves every zoom from the complete zoom up instead of
copies, which is what happens for things like counties. Finer than the finest
zoom the reader draws the exact primary geometry, so full detail is always
reachable and costs nothing to store.

## Install

Python 3.12 and Node 22 or later. From a clone:

```bash
uv sync --group dev          # the writer, validator and CLI, with test deps
npm --prefix js ci
npm --prefix js run build
```

Neither package is on PyPI or npm yet; install the Python package with
`uv pip install -e .` or `pip install -e .`, and the JS package from a local
path after building it (`npm install ../geoparquet-lod/js`).

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

The same from the CLI with a thinning spec, or from Python. In a spec `+`
unions thinnings (`combine`) and `>` chains them (`chain`), as in
`smallest:600000>tiny`:

```bash
uv run geoparquet-lod write buildings.parquet buildings.lod.parquet \
  --thinning size:4+drop:2.5 --where "risk > 0" --carry risk
```

```python
from geoparquet_lod import write, combine, drop_rate, only, size_threshold

write(
    "buildings.parquet", "buildings.lod.parquet",
    thinning=only("risk > 0", combine(size_threshold(4), drop_rate(2.5))),
    carry=["risk"],
)
```

The input is any GeoParquet with a WKB geometry column in OGC:CRS84 or
EPSG:4326. `write()` builds the overviews, then the tile-aligned pages and
spatial directory, in one call; `lod_page_bytes` (`--lod-page-bytes`) sets the
overview page target, 64 KiB by default. Writing the 155.6 million building
national file takes about 25 minutes on a laptop with 12 workers.

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

Tiles are fetched through one shared worker and decoded and encoded in a
small worker pool. `setWorkerCount(1 | 2 | 3 | 4)` from the MapLibre entry point
overrides the default before calling `prefetch`, `readMetadata`, or
`registerProtocol`; one disables the pool. Four helps dense viewports but costs
more memory. Exact geometry and analytical row reads stay on the coordinator.
CPU workers reuse the same `worker.js` URL, so there is no additional worker
asset to deploy.

The server must answer HTTP range requests with CORS and expose
`Content-Range`, which a suitably configured object store can do. `tileUrl`
also takes `pointLayer` for point datasets. Opening probes the first 16 KiB for
the spatial index. Indexed files need only that request; files without the
index then read a 64 KiB tail and any missing footer bytes. A HEAD is needed
only when CORS hides `Content-Range`.

### Reader behaviour

The coordinator caches up to 16 MiB of compressed data pages and 128 directory
shards, and retains up to 64 decoded tiles within an estimated 32 MiB, so a
style change on a loaded view reuses decoded geometry and only projects the new
attribute. Neighbouring tiles share pending fetches. When the last consumer of a
pending fetch or decode leaves, that work is cancelled; completed cache entries
stay reusable. These budgets bound cache accounting, not total browser memory.

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

To compare with a local PMTiles archive:

```bash
npm --prefix js run demo:compare -- --lod /path/current.parquet --pmtiles /path/baseline.pmtiles
```

Open the printed localhost URL. The server supports range reads without copying
the files, binds to loopback, and adds the same selectable 0/50/100 ms delay to
each data request. It does not simulate a bandwidth cap. The synchronized panes
show response-body bytes, requests, cumulative tile feature deliveries, and map
idle times. These are not unique visible-feature counts or wire-transfer
measurements. The default blank background keeps basemap loading out of those
timings; add `basemap=1` for a basemap. Map idle includes rendering and is not
a precise first-paint measurement. The style controls target the Japan building
fixtures: height is carried in both formats, while subtype is fetched from
GeoParquet source rows and is stored in the PMTiles archive.

Use **Fresh load** to recreate readers at the current location and style;
**Reset counters** keeps caches warm. Browser HTTP caches can survive Fresh
load. Pan away and return, then switch between geometry, height, and subtype to
feel navigation and attribute reuse. The files have different thinning and
overzoom rules, so compare visual coverage as well as speed. GeoParquet retains
whole overview features and original source rows; this PMTiles archive stops at
z14 and overzooms beyond it.

### Validation

`geoparquet-lod validate file.parquet` checks the layout metadata, required page
indexes, and up to 2,000 rows of the first row group. Use `--sample-rows N` to
change that quick sample. Use `geoparquet-lod validate file.parquet --full`
(Python: `validate(path, full=True)`) to decode every overview value in every
row group, including boundary copies. Full mode checks closed polygon rings,
packed byte consumption, copy source ids and tile tags, finite transforms,
standard page ranges, and the complete spatial directory against the footer and
source keys: metadata, codecs, page membership, row spans, key bounds, fences,
and branch bounds. Files without a spatial directory get the packed-value and
offset-index checks. This is a storage and reader contract check; it does not
prove polygon topology or compare simplification accuracy with primary WKB. Full
mode reads the complete data, keeps one row group at a time, and uses temporary
disk space of eight bytes per source row for key verification. The returned
`mode` and `checked_non_null` describe the work performed.

### Whole-feature reads

```js
import { readFeatures } from 'geoparquet-lod'

const features = await readFeatures(reader, {
  bbox: [139.7, 35.6, 139.8, 35.7],
  resolution: 360 / (512 * 2 ** 12), // longitude degrees per pixel
  columns: ['height', 'version'],
  geometry: 'overview',
})
// features.coords are whole quantized geometries; apply features.lod.scale/offset.
// geometry: 'primary' instead returns original WKB and original source values.
```

Overview queries return bbox-overlapping, deduplicated features and retain LOD
thinning. They do not perform exact intersection tests. The query enumerates
the intersecting tiles, up to 256 by default; see the `maxTiles` option.
Carried display attributes take precedence in overview queries; other columns
are projected from the source by row id. Primary queries return original source
attributes.

### Nested source attributes

`reader.readColumns(rowIds, ['details.height', 'floors[0].name', 'tags.name'])`
preserves requested row order and duplicates. A struct path selects only its
requested descendant leaves; requesting a whole struct reconstructs all its
children. Lists use zero-based nonnegative bracket indexes; requesting the list
itself returns its full array. Maps use string keys with dot notation and return
objects when requested whole. Missing map keys, out-of-range list indexes, null
ancestors and null elements return `null`; empty lists remain `[]` and empty
maps remain `{}`. Unknown struct fields and malformed paths raise errors before
source reads. Literal top-level scalar names take priority over path syntax,
including names containing dots or brackets. Nested names or map keys containing
dots/brackets cannot be addressed individually; request their parent container.
Map keys follow the Parquet decoder's object representation (string coercion and
last value for duplicate keys); non-string or reserved JavaScript object keys
should be accessed with a dedicated Arrow/Parquet analytical reader.

Flat scalars use sparse page projection and a scalar value cache. Nested struct
scalars also read only selected pages. A list or map projection decodes all
pages of the selected collection leaves in each selected row group, so values
crossing page boundaries reconstruct correctly; it materializes full
collections before extracting one index or key and is not cached. Explicit
physical Parquet collection leaf names such as `nums.list.element` return their
low-level wrapped values. The same logical paths work in `readFeatures` and
`readGeometryBatch` `columns`.

### Renderer-neutral geometry batches

```js
import {
  openReader,
  httpSource,
  readGeometryBatch,
  batchToGeoJSON,
  batchToGeoArrow,
  batchToMvt,
} from 'geoparquet-lod'

const reader = await openReader(await httpSource([url]))
const batch = await readGeometryBatch(reader, {
  bbox: [-105.1, 39.6, -104.9, 39.8],
  resolution: 360 / (512 * 2 ** 12),
  columns: ['details.height'],
  geometry: 'overview', // or 'primary' for original WKB/source attributes
})
const features = batchToGeoJSON(batch)
const arrowIPC = batchToGeoArrow(batch, { crs: 'OGC:CRS84' })
const mvt = batchToMvt(batch, { z: 12, x: 852, y: 1555 })
```

`toGeometryBatch(await readFeatures(reader, query))` normalizes a
`readFeatures` result without another read. The batch exposes `geometry`,
`rowIndex`, `columns` and `source`. Geometry uses typed x/y buffers and
feature→polygon→ring/line→vertex offsets, with coordinates
`offset + scale * [x, y]` in longitude/latitude. Overview coordinates remain
quantized `Int32Array`; primary WKB becomes `Float64Array` and retains supported
Z coordinates. Overview batches may be thinned and simplified. Sparse zoom
profiles select the first declared zoom at least as detailed as the requested
resolution, clamped at the finest profile; queries enumerate tiles at that same
declared zoom so boundary copies match.

GeoJSON is an optional object adapter for small results; attribute values retain
their decoded JS types, so BigInt needs conversion before JSON serialization.
GeoArrow produces an Arrow IPC stream through the `@cogp/reader` converter. It
dequantizes coordinates, uses a GeoArrow multi geometry family, and preserves
scalar attribute types where supported; nested and mixed attributes become
display strings. Mixed geometry families cannot be written in one GeoArrow
geometry column. `batchToMvt` clips the same buffers to one tile, defaults to
layer `shapes`, and uses the same MVT encoder and property semantics as
`renderTile`, which remains the optimized tile-serving path.

A second renderer is hosted at
<https://geoparquet-lod-compare.pages.dev/deck/> and runnable locally at
`http://localhost:8091/demo/deck/index.html?url=/buildings.lod.parquet` after
`npm --prefix js run demo`. `demo/deck/main.js` bundles deck.gl 9.1 with the
GeoArrow deck.gl layers under a `TileLayer`: each tile reads its extent as a
geometry batch at the tile's zoom, converts it to a GeoArrow table, and
renders through `GeoArrowPolygonLayer` (or the path and scatterplot layers
for lines and points), so coordinates move from Arrow buffers into GPU
attributes without per-feature objects. The tile layer keeps loaded tiles and
their GPU buffers while panning, cancels abandoned loads, and overzooms the
finest overview. Polygon triangulation runs in an earcut worker pool served
from the bundle. Whole features that straddle a tile edge are read by both
tiles, so counts in the status line include those repeats. Outlines are drawn
from zoom 14, where features are larger than the placeholder squares coarse
overviews use. `?view=lon,lat,zoom` opens at a place and `?url=` opens another
file; there is no basemap dependency. For external files use the comparison
server's `/demo/deck/index.html?url=/data/current.parquet` route; the server
needs both `--lod` and `--pmtiles`.
