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
- `python/`: writer, validator and CLI (`geoparquet-lod write | validate | inspect`)
- `js/`: a browser reader and a MapLibre tile protocol
- `examples/`: thinning functions for common datasets
- `docs/benchmarks.md`: measurements against PMTiles, COGP and plain GeoParquet

### Spatial page index experiment

`geoparquet-lod optimize input.parquet output.parquet` now adds an optional
map access path to an existing **packed** LOD file. It aligns LOD data pages
with tile boundaries, clusters each LOD's column chunks together, and adds a
small spatial directory. Every source and LOD value, row order, feature id,
and analytical row group is preserved. There is no second set of tile payloads.

The MapLibre worker detects these files automatically. Direct JS callers use
`openReader(coalescing(await httpSource([url])))` instead of `LodReader.open`.
Opening an indexed file takes one 16 KiB range; analytical row inspection and
the exact-geometry fallback load the ordinary Parquet footer when needed.
Expose `Content-Range` in CORS responses to avoid a separate sizing HEAD.
`--keep-pages` isolates the effect of the directory and physical clustering;
the default also re-encodes the LOD pages on their finest tile grid.

For new indexed-file experiments, `write --no-prefix` (Python
`prefix_zoom=None`) uses one Hilbert order. This rewrites row positions and
remaps boundary-copy IDs. The optimizer now sizes sparse LOD pages by present
values; `--fixed-pages` restores the 2,048-source-row cap. The reader decodes
straight into coordinate buffers and streams MVT output. See the
[single-order and rendering results](docs/parity-followup.md) for request parity
in six sampled tiles, faster rendering, and the remaining coarse-zoom tradeoff.

The optimizer uses a 64 KiB uncompressed page target (`--page-bytes 65536`);
`--page-bytes 16384` reproduces the earlier layout. Tile boundaries and row
caps still apply. Indexed map tiles now share one fetch/cache coordinator and
two CPU workers (one on devices reporting two or fewer logical cores).
See [worker and page-size results](docs/parity-workers.md) for the measured
latency, byte savings, and startup/memory tradeoffs.

This is experimental and remains behind PMTiles in some workloads. The Japan
measurements, tradeoffs, and reproduction commands are in
[the parity experiment](docs/parity-experiment.md); the physical layout is in
[the spatial index profile](spec/SPATIAL-INDEX.md). Ordinary analytical readers
can read the optimized file without understanding that profile. A general
Parquet rewrite will discard the directory; run the optimizer again afterward.

### Whole-feature reads and runtime attributes

Indexed packed tiles now retain decoded geometry and hydrate newly requested
source attributes without decoding that geometry again. The MapLibre adapter
uses an additional bounded 32 MiB / 64-tile cache and runs source-page projection
and tile encoding in its CPU pool. A Tokyo city style change improved from
797 to 402 ms on first use, and from 245 to 81 ms on repeat, with unchanged
requests and bytes. Cold styled loads were slightly slower. See
[style-change results and memory costs](docs/parity-styles.md).

Shared navigation work now stops when its last consumer leaves, including
compressed pages, decoded tiles, and unused analytical footer reads. In a
controlled pan/zoom route this reduced completed payload by 30–76%; fully
loaded views retained the same bytes and output. See
[navigation results and cancellation tradeoffs](docs/parity-navigation.md).

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
so **packed remains the default**. See [results and limitations](docs/parity-separate.md).

## How it works

Rows are sorted once along a Hilbert curve over the Web Mercator world
square in large row groups, so a tile's rows are one contiguous run and a
city tile touches one or two row groups. The rows shown at the coarsest zooms come
first in their own order, so a view of the whole dataset reads one row group.
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

| thinning                        | rule                                                                                                                                    | good for                                   |
| ------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------ |
| `drop_rate(2.5)`                | each zoom coarser keeps 1/rate, uniformly, so relative density survives (tippecanoe `-r`); the default                                  | points, small polygons                     |
| `size_threshold(4)`             | keep a feature while it still spans 4 pixels                                                                                            | large polygons and lines first             |
| `tiny_polygons(0.25)`           | tippecanoe's tiny-polygon reduction: areas under a quarter-pixel square accumulate per tile and one square is kept per square's worth   | area-true coverage of buildings zoomed out |
| `importance('population', 300)` | keep the top N per tile by a column                                                                                                     | named places, ranked facilities            |
| `cluster(8, count='count')`     | one representative per 8 px cell carrying how many it stands for                                                                        | dense points                               |
| `density_budget(60_000)`        | cap the busiest tile with a uniform sample; flattens dense places                                                                       | bounding the largest tiles                 |
| `drop_smallest(600_000)`        | one area bar per zoom, raised until the busiest tile's estimated bytes fit, applied everywhere (tippecanoe `--drop-smallest-as-needed`) | buildings, before `tiny_polygons`          |
| `only('risk > 0', inner)`       | restrict eligibility before `inner`                                                                                                     | partial datasets                           |
| `combine(a, b)`                 | keep what any part keeps                                                                                                                | size plus sample, etc.                     |
| `chain(a, b)`                   | apply `b` to what `a` kept                                                                                                              | `drop_smallest` then `tiny_polygons`       |

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
features that reach into its tile, so a tile is one range read of that
column. Grid coordinates decode as `offset + scale * integer`; the
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

Indexed tiles are fetched through one shared worker and decoded/encoded in a
small worker pool. `setWorkerCount(1 | 2 | 3 | 4)` from the MapLibre entry point
can override the default before calling `prefetch`, `readMetadata`, or
`registerProtocol`; one disables the pool. Four helps dense viewports but costs
more memory. Ordinary files, exact geometry, and analytical row reads stay on
the coordinator. CPU workers reuse
the same `worker.js` URL, so there is no additional worker asset to deploy.

The server must answer HTTP range requests with CORS, which plain S3 does. `tileUrl` also takes
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
show transferred bytes, requests, tile feature counts, and map idle times.
The default blank background keeps basemap loading out of those timings;
add `basemap=1` for a basemap. Map idle includes rendering and is not a precise
first-paint measurement. These controls target the Japan building fixtures:
height is carried in both formats, while subtype is fetched from GeoParquet
source rows and is already stored in this PMTiles archive.

Use **Fresh load** to recreate readers at the current location and style;
**Reset counters** keeps caches warm. Pan away and return, then switch between
geometry, height, and subtype to feel navigation and attribute reuse. The files
have different thinning and overzoom rules, so compare visual coverage as well
as speed. GeoParquet retains whole overview features and original source rows;
this PMTiles archive stops at z14 and overzooms beyond it.

## Measured

The method and tables are in [docs/benchmarks.md](docs/benchmarks.md). On
Esri's 53.7 million Japan building footprints, written as this layout, as
Kanahiro's COGP, as a tippecanoe PMTiles archive and as Esri's Spatially
Optimized Parquet, with every reader cold on each tile and feature counts
beside the bytes, since the four thin so differently that bytes alone
mislead:

- The thinning is calibrated against the archive: within about 20% of
  tippecanoe's feature count at every zoom from 6 to 12, and the share of
  a city tile the buildings paint agrees within a point.
- A z8 metro tile is 0.69 MB in 9 requests against the archive's 0.50 MB
  in 2; a z11 city tile 0.50 MB against 0.31; z13 downtown 0.47 against
  0.35; street tiles 0.14 to 0.16 MB against 0.03 to 0.11. The next tile
  on a warm reader is 0.06 to 0.32 MB in 4 to 9 requests against 0.02 to
  0.26 in 1.
- Opening costs 0.47 MB, the footer and the prefix indexes, against 20 KB
  for PMTiles.
- COGP and the Esri reference reader cost more than either at every zoom
  measured, and COGP draws a small fraction of the buildings.

On the 155.6 million US buildings behind CarbonPlan's fire-risk map, where
the file keeps every building from z13 and the archive drops half, a warm
tile is one round trip per section against the archive's one, street tiles
read 0.03 to 0.05 MB warm against 0.05 to 0.12, and a z13 downtown tile
reads 2.5 times the archive's bytes to draw twice its buildings.

A tile is one range read of its LOD column plus the key pages at the ends
of its interval and, cold, the row group's indexes; the floor is therefore
a few requests, not the one a tile archive makes. If the map is the product
and the data is secondary, build PMTiles. This layout is for the case where
the file is also the dataset: one upload, one URL, the thinned map and the
exact rows behind it.

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
  transform and grid are its `quantized_geoarrow`, and the tile encoder uses
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
