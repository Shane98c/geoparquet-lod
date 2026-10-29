# GeoParquet LOD

_The `lod_columns` extension: a layout and metadata contract for GeoParquet
files that serve a zoomable map directly. Implemented by the writer and reader in this repository. Draft 0.1.0._

The key words MUST, SHOULD and MAY are to be interpreted as in RFC 2119.
'Layout', 'Metadata' and 'Geometry' are the contract; 'Reading without the
spatial index' describes the reference reader and binds no one. The optional
[spatial index](SPATIAL-INDEX.md) is a physical profile over this layout.

## Goal

One GeoParquet file is both the dataset and the map source. Every feature is
stored exactly once with its lossless geometry. A renderer reads, per tile,
bytes comparable to a pre-cut vector tile. A reader that does not know this
extension sees ordinary GeoParquet with a bbox covering, Hilbert key, and LOD
column alongside the source columns.

## Terms

- **Zoom**: Web Mercator zoom for 512 px tiles. `tile_size` in metadata
  records the assumption.
- **CRS**: zooms, grids and tiles are Web Mercator. The primary geometry
  MUST be OGC:CRS84: `crs` omitted (GeoParquet's default), or an explicit
  OGC:CRS84 or EPSG:4326 in x/y order, with planar edges. An explicit null
  `crs` means unknown and does not qualify. LOD geometry is stored in
  EPSG:3857 metres, so a grid cell is the same size on screen at every
  latitude; the bbox covering stays in the primary geometry's degrees.
- **LOD**: one stored display representation (row selection, carried
  attributes and geometry) serving a contiguous range of zooms. Zooms may
  share an LOD only when row selection and carried attributes agree, and an
  LOD uses its finest zoom's grid.
- **LOD column**: one nullable binary child per LOD in the required `lod`
  struct. Each value packs quantized geometry, carried attributes, and any
  boundary copies it hosts. Attribute types are declared in metadata.
- **Thinning**: the rule selecting displayed features independently at each
  zoom. Membership need not nest across zooms. The reference writer can force
  completeness from a chosen zoom; null and empty primary geometries remain
  absent from display LODs. This extension records the rule's description; it
  does not define rules.

## Layout

A conforming file MUST satisfy:

1. **Order.** Rows sorted by a space-filling curve over the primary
   geometry. `order` names the curve:
   `hilbert_webmercator` is the standard Hilbert curve over the Web Mercator
   world square divided into `2^depth` cells per side, numbered as XYZ tiles
   are, on the cell holding the bbox centre with latitude clamped to the
   Mercator limit. The curve's quadtree prefix property makes a tile at zoom
   `z <= depth` with Hilbert index `h` the key interval
   `[h * 4^(depth - z), (h + 1) * 4^(depth - z))`, so a tile's rows are one
   contiguous run. The key is stored in the `key` column
   with row group statistics and a page index; a reader finds a tile's row
   groups from the footer, its key pages from the page index, and its exact
   rows by reading the two boundary key pages, then reads only those rows of
   the LOD column. A row whose primary geometry is null or empty has a null
   key and sorts after every row with a key. A feature centred outside a
   tile whose geometry reaches into the tile or within 2 pixels of it
   (16/4096 of the tile) is a _straddler_. A renderer draws each tile's
   shapes clipped to the tile, so a feature is needed only where it draws
   inside it, and the margin holds an outline drawn along the edge. A
   straddler's row is in another interval, so each packed LOD row carries the
   copies it hosts: the tile each serves as `zoom << 48 | x * 2^zoom + y`, the
   feature's file row index, its geometry on that LOD's grid and its
   attributes. The writer MUST place every
   copy a tile needs on the tile's _host_: the first row whose key is at or
   past the tile's interval start, or the last row with a non-null key when
   none is, and a row
   shown only to host copies has an empty geometry. A
   shape under a pixel at the zoom draws as a placeholder square its own
   tile shows whole, so the reference writer copies only shapes a pixel or
   larger. A reader therefore reads the interval's rows plus the host row
   and keeps the copies whose `tile` is the tile's. The per-row
   uniform `u` the reference writer hands thinnings is a hash of the
   geometry bytes, so it does not depend on sort order.
2. **Covering.** A nullable `bbox` struct column of four required float32
   values, `xmin`, `ymin`, `xmax` and `ymax`, declared as the primary
   geometry's GeoParquet 1.1 bbox covering at the paths `["bbox", "xmin"]`
   and so on, with column statistics on
   every row group, and a column index (page statistics) and offset index on
   each of its four leaves. Values are rounded outward. A row whose primary
   geometry is null or empty has a null covering, so statistics hold only
   real bounds and a page of such rows is a null page of the column index.
3. **Page indexes.** Every column has an offset index.
4. **LOD columns.** A top-level required struct column named by `column`
   with one nullable binary child per LOD, named by the LOD. The child is
   non-null for a row when it is shown at the LOD's zooms or hosts boundary copies;
   since zooms sharing an LOD agree on row selection, that is one condition
   per LOD. Each child is a nullable `binary` column, one Parquet leaf,
   holding the row packed as Geometry describes: its geometry, its carried
   attributes and the straddler copies it hosts, see Layout 1. A host-only row has empty own geometry. A row whose
   primary geometry is null or empty is shown at no LOD.

The validator checks the version, encoding and per-LOD metadata, the schema
shapes and covering declaration, covering statistics, page indexes, and a
sample from the first row group: every LOD's values there decode, and the
covering is null on exactly the rows whose primary geometry is null or empty.
In full mode it decodes every LOD value and copy, checks in every row group
that the covering and the key are null on the same rows, and, when the file
has the [spatial index](SPATIAL-INDEX.md), checks it and
the key order against the footer. It does not check the rules on `zooms`
below, nor that every row is shown at the finest LOD; that is what the
reference writer does, and a conformance suite with fixtures for it is open
work.

The following SHOULD hold and are what the reference writer does:

- Row groups of about one million rows; analytical pages target 16 KiB and
  tile-aligned overview pages target 64 KiB uncompressed, with v1 page headers.
  These are writer policies, not requirements on other producers. The
  [spatial index](SPATIAL-INDEX.md) selects overview pages directly without
  fetching entire row-group offset indexes, and requires them to be v1 PLAIN
  pages.
- Compression that decodes in a browser (zstd); no dictionary encoding on
  LOD or bbox columns. Packed LODs are binary values rather than separate
  coordinate leaves. The standard footer reader also handles dictionary pages.

## Metadata

Under the `geo` key, a `lod_columns` object:

| field          | type     | meaning                                                                                                                                                                                                                     |
| -------------- | -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `version`      | string   | `0.1.0`; readers reject a different major version, and while the major is 0 a different minor as well, since minors may change the layout                                                                                   |
| `column`       | string   | name of the LOD struct column                                                                                                                                                                                               |
| `order`        | string   | `hilbert_webmercator`, see Layout 1                                                                                                                                                                                         |
| `key`          | object   | `{column, depth}`: the nullable int64 column holding each row's Hilbert index at `depth` of its Web Mercator cell, with statistics and a page index, see Layout 1                                                           |
| `zooms`        | int[]    | every zoom the file serves, ascending                                                                                                                                                                                       |
| `tile_size`    | int      | pixels per tile the zooms assume (512)                                                                                                                                                                                      |
| `encoding`     | string   | `packed`, see Geometry                                                                                                                                                                                                      |
| `pack_version` | int      | `2`, the packed row format version; readers reject any other value                                                                                                                                                          |
| `attributes`   | object[] | `{name, type, values?}` of carried attributes; `type` is the Arrow type name used to decode packed values; optional `values` is a string dictionary holding every value packed for the attribute                            |
| `thinning`     | object   | description of the rule used; informational                                                                                                                                                                                 |
| `lods`         | object   | LOD name to `{zooms, geometry_type, scale, offset, placeholder}`; `scale` and `offset` are `[x, y]` pairs in EPSG:3857 metres, `placeholder` the width in grid cells of the square drawn for a shape too small for the grid |

All fields are required; readers MUST ignore fields they do not know. Every
zoom in `zooms` MUST appear in exactly one LOD and lie in `0..key.depth`, so
each tile is one interval of keys (Layout 1); `zooms` MUST be contiguous,
and `scale` and `offset` MUST be finite. Every row with a non-null, non-empty
primary geometry MUST
be shown at the finest LOD. Membership at the other LODs is the writer's
choice per LOD and need not nest: a row may be shown zoomed out and hidden
one zoom in, as tippecanoe's per-zoom dropping produces. An LOD's zooms are
contiguous and ascending. Which zooms share an LOD is writer policy: the
reference writer gives each zoom its own LOD until the zoom from which the
thinning keeps every row, pairs zooms from it upward, and uses one LOD for all
complete zooms when the full-detail geometry is small.

### Example

Abbreviated metadata for a building file with two carried attributes, under
the `geo` key beside the usual GeoParquet fields (only two LOD entries shown):

```json
"lod_columns": {
  "version": "0.1.0",
  "column": "lod",
  "order": "hilbert_webmercator",
  "key": { "column": "hilbert", "depth": 24 },
  "zooms": [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16],
  "tile_size": 512,
  "encoding": "packed",
  "pack_version": 2,
  "attributes": [{ "name": "height", "type": "float" }, { "name": "class", "type": "string" }],
  "thinning": { "name": "combine(size_threshold(4), drop_rate(2.5))" },
  "lods": {
    "f8": { "zooms": [8], "geometry_type": "MultiPolygon", "scale": [76.437, 76.437], "offset": [-11699678.5, 4807984.5], "placeholder": 2 },
    "f13": { "zooms": [13, 14], "geometry_type": "MultiPolygon", "scale": [1.1943, 1.1943], "offset": [-11699678.5, 4807984.5], "placeholder": 2 }
  }
}
```

and the physical schema, with the source attributes and `geometry` as the
source had them:

```
bbox: struct<xmin: float not null, ymin: float not null, xmax: float not null, ymax: float not null>
hilbert: int64
lod: struct<
  f8: binary,
  f13: binary
> not null
height: float
class: string
geometry: binary (WKB)
```

## Geometry

Every LOD value is one packed row, a byte string a tile reads in one run:
the row's geometry on the LOD's grid, its carried attributes, and on a host
row the straddler copies it carries. Everything is an unsigned LEB128 varint
except string bytes, which follow all of a row's varints in order, so a
reader walks the varints knowing the structure and then takes string
payloads in turn:

```
row    := geometry attrs spill strings...
geometry :=
  MultiPolygon:    n, per polygon: rings, per ring: vertices, then per vertex
                   zigzag(dx) zigzag(dy), steps within the ring and the first
                   vertex from the origin; the closing vertex is not stored
  MultiLineString: n parts, per part: vertices, steps as above
  Point:           zigzag(x) zigzag(y)
attrs  := presence bitmap (one varint, bit i for attribute i in metadata
          order), then each present value: integers zigzag, float32 as its
          bit pattern, float64 as its bit pattern, bool 0/1, string as its
          byte length, or its zero-based dictionary index when the attribute
          declares `values` (no trailing string bytes for dictionary values)
spill  := count, per copy: tile (zoom << 48 | x * 2^zoom + y), file row
          index, geometry, attrs
```

`zigzag(v)` is `(v << 1) ^ (v >> 63)`. A decoded grid coordinate is
`offset + scale * integer` in EPSG:3857 metres, with `scale` and `offset` per
LOD: the transform of COGP's `quantized_geoarrow`, applied in Web Mercator
rather than the primary geometry's CRS, in one private byte format a COGP
reader cannot decode. Grid coordinates MUST fit in a signed 32-bit integer.
A polygon ring is stored without its closing vertex and has at least three
stored vertices; a reader closes it by repeating its first vertex, so decoded
rings are closed as WKB and GeoArrow expect. Every line part has at least two
vertices, and a row shown only to host copies has zero polygons; empty
geometry is otherwise never written. Ring steps make a building a handful of
one-byte varints: a placeholder square packs to 13 to 18 bytes before
compression, a footprint to about two bytes per vertex.
The reference writer projects the primary geometry to Web Mercator, with
latitude clamped to the Mercator limit, and uses a quarter of the LOD's
finest zoom pixel as `scale` and the dataset's minimum corner as the origin.
Quantization rounds half away from zero. It refuses an extent too wide for
the finest zoom's grid to fit in 32 bits.

A family keeps its geometry type at every zoom. A polygon narrower in both
directions than its placeholder square, or empty or invalid after snapping
to the LOD's grid, becomes that square: `placeholder` cells on a side, on the
grid cell holding its centroid, half a pixel unless the thinning asks for
another size, so a thinned set of buildings zoomed out reads as a faint
texture of its density, as tippecanoe's tiny-polygon squares do. A line
shorter than a cell in both directions becomes a segment `placeholder`
cells long from its centroid's cell. The grid
is a quarter of a Web Mercator pixel at the LOD's finest zoom, close to
tippecanoe's eighth-pixel tolerance; features a pixel or two across keep
their own shape.

Carried attributes keep their declared Arrow types and nulls. Integers are
zigzagged as int64, so a packed carried uint64 value MUST NOT exceed
2^63 - 1; the source column itself is unrestricted. A copy MUST carry the
attribute values its row has at the LOD: a thinning that overrides an
attribute writes the overridden value, cast to the declared type, into that
LOD's rows and their copies, so a feature reads the same in every tile it
reaches.

## Reading without the spatial index

The indexed map path is described in [SPATIAL-INDEX.md](SPATIAL-INDEX.md).
The logical layout alone is enough to read a tile; this is how the reference
reader serves files without the index and how it reaches analytical rows.

1. Read the footer. The reference HTTP source first probes the first 16 KiB
   for the [spatial index](SPATIAL-INDEX.md), which bypasses this algorithm
   for LOD tiles. Otherwise it reads a 64 KiB tail and any missing footer bytes
   and validates `lod_columns`.
2. For a tile at zoom z choose the LOD containing z, clamping to the
   coarsest LOD below `zooms`. Finer than the finest zoom, read the primary
   geometry instead: every row is shown at the finest LOD, so the bbox pages
   alone choose the rows, skipping null pages and null coverings, and their
   exact WKB is small at those zooms.
3. The tile's key interval, see Layout 1. Candidate row groups: those whose
   key statistics intersect it, plus the host group, the first group whose
   maximum key is at or past the interval start, or the last group with
   keys when none is.
4. In each group, the interval's two boundaries: the first rows whose keys
   are at or past its start and its end. Keys are sorted, so the page index
   places each boundary in the first key page whose maximum reaches it, on
   that page's first row when its minimum does too; only a key page a
   boundary falls inside is read. The rows between the boundaries are the
   tile's own, and the start boundary is the host. When no key reaches the
   interval start, the host is the last row with a key, read from the last
   key page that has one.
5. Read the LOD column's pages overlapping the own rows and the host,
   merging adjacent pages into one range. Decode each row: an own row with
   geometry is a feature, and each hosted copy whose tile is this tile is a
   feature with the copy's row index as its id.
6. Points are clipped to the tile; polygons and lines are clipped by the
   tile encoder. The MVT feature id is the file row index.

No bbox statistics or pages are read for an LOD tile; the covering serves
analysis readers and the primary geometry fallback.

## Choosing zooms

The extension does not fix zooms. The reference writer derives them from the
data when not given: the coarsest zoom is one coarser than the zoom at which
the dataset extent spans about one pixel; every feature is shown from the
zoom where the median feature spans one pixel; the finest zoom is where the
median vertex spacing spans four pixels, capped at 15, past which a map
overzooms the finest LOD. A zoom whose thinning keeps 99.99% of rows or more
keeps them all. Writers SHOULD record the zooms in `zooms` and MAY use any
rule.

## Analysis readers

Nothing here changes the primary geometry or attribute columns. `SELECT
count(*)`, spatial joins and attribute queries are correct with no filter.
The `bbox` column is the standard 1.1 covering. The LOD column is a
struct of binary columns a reader can drop. The reference writer keeps
statistics and page indexes on the source's scalar columns, so a filter on
one, such as an ID or a region code, skips row groups and pages that cannot
match.

## Relation to other proposals

Several GeoParquet overview designs are in progress. Where they differ from
this layout is in which rows a coarse view reads and how it finds them.

- **COGP** ([Kanahiro/cloud-optimized-geoparquet](https://github.com/Kanahiro/cloud-optimized-geoparquet),
  proposed to GeoParquet as [PR 305](https://github.com/opengeospatial/geoparquet/pull/305))
  stores every feature once, orders row groups coarse to fine, and declares
  each level as a cumulative row-group prefix in `geo.lod`, with resolutions
  that strictly decrease. An optional overview column holds `quantized_geoarrow`
  geometry: nested int32 coordinates with a scale and offset per LoD, derived
  directly from the primary geometry rather than from the previous level.
  Reading a fine level reads every coarser level's row groups that touch the
  view, so requests grow with the level count, and a feature's level is fixed
  by its row group. This layout keeps COGP's quantization transform, applied in
  Web Mercator, with `Point` added, but moves zoom selection into nullable per-LOD columns
  so membership is decided per zoom and read at page granularity, and packs
  each row into one binary value a COGP reader cannot decode. A review comment
  on PR 305 suggested storing the level in a column with statistics for exactly
  that page-level granularity; nullable LOD columns with an index over their
  pages are one answer to it, and the [spatial index](SPATIAL-INDEX.md) could
  be offered to COGP as an optional access profile.
- **tylertoo** ([geoparquet-io/tylertoo](https://github.com/geoparquet-io/tylertoo))
  writes overviews as rows with a `level` column and simplified WKB, declared
  in `geo:overviews` metadata, either once per level or partitioned. Its
  maintainers have decided to converge that output on COGP, which makes COGP
  the layout most other tooling will produce. The question this repository
  asks is whether the page-level access path is worth adding to it.
- **GeoParquet PR 299** ([LOD extension, mmgeorge](https://github.com/opengeospatial/geoparquet/pull/299))
  declares simplified geometry columns with a resolution each, nested paths
  allowed, with the primary geometry as the fallback. It reduces geometric
  detail but not feature count. What this layout adds is row selection per
  LOD and per-LOD attribute state; its metadata could become a PR 299
  extension once that is settled. Our LOD geometry is integer rather than a
  GeoParquet primary encoding for the same reason COGP's overviews are:
  integer grids compress several times better than float64 WKB and decode
  without a parser. The [design alternatives](../docs/design-alternatives.md)
  quantify that difference.
- **Esri Spatially Optimized Parquet** ([Esri/spatially-optimized-parquet](https://github.com/Esri/spatially-optimized-parquet))
  clusters rows by an XZ-order code, relies on the Parquet page index for
  extent queries, and adds per-level geometry columns of delta-encoded
  protobuf simplified with Douglas-Peucker on a power-of-two scale ladder,
  declared under a `geodisplay` key. Every feature is stored at every level,
  so coarse views read many pages; the reference reader skips geometries that
  collapse at the level's resolution and stops at 650,000 features per query.
  This layout drops features per zoom instead, as tippecanoe does.
- **GeoParquet 2.0**: orthogonal. The primary geometry is WKB in both 1.1 and
  2.0; 2.0 annotates it with Parquet's `GEOMETRY` type and gains row-group
  geospatial statistics. 2.0 allows no other primary encoding, which is why
  the GeoArrow-shaped layout appears only in the LOD columns. Page pruning
  still needs the bbox covering under either version.

## Open questions

- Whether the overviews should be an ordinary table instead of packed rows:
  one row per shown feature per level and per boundary copy, geometry as
  COGP's `quantized_geoarrow` shape, tile-sized row groups and a directory
  over them. [Design alternatives](../docs/design-alternatives.md) measures
  it on Tokyo with identical tile membership: the same request counts, 40 to
  55% more bytes at city zooms, 57% more overview storage, and an estimated
  twice the decode CPU, in exchange for overviews any Parquet reader can open.
- Whether `quantized_geoarrow` should admit a `Point` type, or point LODs
  should use a GeoArrow point struct in metres at float64.
- Poles and the antimeridian. Overview geometry is clamped at the Mercator
  limit vertex by vertex rather than clipped, and a feature crossing ±180°
  is neither split nor indexed on both sides.
- Partitioned datasets: one manifest listing files and their extents.
- Whether to order the table coarse-first for readers that do not know the
  format, as COGP does, so QGIS draws the coarse selection before the rest.
  [Design alternatives](../docs/design-alternatives.md) measures both sides
  on Tokyo: QGIS shows 44% of the final picture after one second with the
  prefix against 4% with one order, and the indexed reader pays one extra
  request per tile at city zooms and two at street zooms.
