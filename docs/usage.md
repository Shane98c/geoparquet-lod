# Writer and reader guide

See the [README](../README.md) for the recommended indexed workflow and current
measurements. The experiments linked below document individual design decisions.

## Whole-feature reads and runtime attributes

Indexed packed tiles now retain decoded geometry and hydrate newly requested
source attributes without decoding that geometry again. The MapLibre adapter
uses an additional bounded 32 MiB / 64-tile cache and runs source-page projection
and tile encoding in its CPU pool. A Tokyo city style change improved from
797 to 402 ms on first use, and from 245 to 81 ms on repeat, with unchanged
requests and bytes. Cold styled loads were slightly slower. See
[style-change results and memory costs](parity-styles.md).

Shared navigation work now stops when its last consumer leaves, including
compressed pages, decoded tiles, and unused analytical footer reads. In a
controlled pan/zoom route this reduced completed payload by 30–76%; fully
loaded views retained the same bytes and output. See
[navigation results and cancellation tradeoffs](parity-navigation.md).

The packed path now supports styling with source scalar columns that were not
carried at write time. These are batched by source row ID and cached on demand;
the first read can be substantially slower than reading carried attributes.
A file rewrite is useful for making a new attribute fast, but is not required
to use it. `readColumns` also projects original source values directly.

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
thinning. They do not perform exact intersection tests. The current implementation
uses up to 256 tiles by default; see the API's `maxTiles` option.

`geoparquet-lod separate packed.parquet split.parquet`, followed by `optimize`,
is an opt-in experiment with independently selectable display-attribute columns.
It preserved functionality but lost styled-map performance in the Tokyo ablation,
so **packed remains the default**. See [results and limitations](parity-separate.md).

## How it works

Rows are sorted once along a Hilbert curve over the Web Mercator world
square in large row groups, so a tile's rows are one contiguous run and a
city tile touches one or two row groups. An optional coarse prefix puts early-visible rows first in their own order.
The current indexed Japan profile disables that prefix to keep one Hilbert stream.
A float32 bbox covering with page statistics lets a reader pick pages without
reading geometry. Each LOD is a nullable binary child of the required `lod`
struct. Present values pack whole quantized geometry, carried attributes, and
boundary copies; the spatial index selects these pages directly.

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
writer's other options (zooms, the complete zoom, prefix zoom, row group and
page sizes, compression level) shape the file, not the row selection.

Zooms are derived from the data unless given: the coarsest zoom is where the
dataset's extent fits a tile, the base zoom a rate-based thinning counts
from is where the median feature spans a pixel, and the finest zoom is where
the median vertex spacing spans four pixels. The writer logs the guess and
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
on demand. Adding one to the packed fast path requires rewriting the LOD
payloads. Thinning functions can read any source column via `columns=`
without carrying it.

## Geometry

Points, lines and polygons, one family per file, and a family keeps its
geometry type at every zoom, as tippecanoe does. Lines and polygons carry a
quantized copy per LOD: one per zoom below the complete zoom, where the
thinning changes the row set, and one per two zooms above it. Each LOD is
one binary column: a row's value packs its geometry on the LOD's pixel grid
as varint ring steps, its carried attributes, and copies of neighbouring
features that reach into its tile, so a tile can read a contiguous range of that column. Page, directory,
and row-group boundaries can add requests. Grid coordinates decode as `offset + scale * integer`; the
transform and grid are COGP's `quantized_geoarrow`, the byte format is this
layout's own, a page of spec. When the full-detail shapes of the whole dataset are
small (16 MB by default) a single LOD serves every zoom instead of copies,
which is what happens for things like counties. Finer than the finest zoom
the reader draws the exact primary geometry, so full detail is always
reachable and costs nothing to store.

## Install

Python 3.12 and Node 22 or later. From a clone:

```bash
uv sync --group dev          # the writer, validator and CLI, with test deps
npm --prefix js ci
npm --prefix js run build
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

Indexed tiles are fetched through one shared worker and decoded/encoded in a
small worker pool. `setWorkerCount(1 | 2 | 3 | 4)` from the MapLibre entry point
can override the default before calling `prefetch`, `readMetadata`, or
`registerProtocol`; one disables the pool. Four helps dense viewports but costs
more memory. Ordinary files, exact geometry, and analytical row reads stay on
the coordinator. CPU workers reuse
the same `worker.js` URL, so there is no additional worker asset to deploy.

The server must answer HTTP range requests with CORS, which a suitably configured object store can do. Expose `Content-Range` through CORS. `tileUrl` also takes
`pointLayer` for point datasets.
Opening probes the first 16 KiB for the optional spatial index. Indexed files
need only that request; ordinary files then read a 64 KiB tail and any missing
footer bytes. A HEAD is needed only when CORS hides `Content-Range`.

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

To compare the latest reader with a local PMTiles archive:

```bash
npm --prefix js run demo:compare -- --lod /path/current.parquet --pmtiles /path/baseline.pmtiles
```

Open the printed localhost URL. The server supports range reads without copying
the files, binds to loopback, and adds the same selectable 0/50/100 ms delay to
each data request. It does not simulate a bandwidth cap. The synchronized panes
show response-body bytes, requests, cumulative tile feature deliveries, and map idle times.
These are not unique visible-feature counts or wire-transfer measurements.
The default blank background keeps basemap loading out of those timings;
add `basemap=1` for a basemap. Map idle includes rendering and is not a precise
first-paint measurement. These controls target the Japan building fixtures:
height is carried in both formats, while subtype is fetched from GeoParquet
source rows and is already stored in this PMTiles archive.

Use **Fresh load** to recreate readers at the current location and style;
**Reset counters** keeps caches warm. Browser HTTP caches can survive Fresh load. Pan away and return, then switch between
geometry, height, and subtype to feel navigation and attribute reuse. The files
have different thinning and overzoom rules, so compare visual coverage as well
as speed. GeoParquet retains whole overview features and original source rows;
this PMTiles archive stops at z14 and overzooms beyond it.
