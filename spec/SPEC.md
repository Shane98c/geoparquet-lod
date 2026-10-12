# LOD columns

_A layout and metadata contract for GeoParquet files that serve a zoomable
map directly. Implemented by the writer and reader in this repository. Draft 0.1.0._

The key words MUST, SHOULD and MAY are to be interpreted as in RFC 2119.
'Layout', 'Metadata' and 'Geometry' are the contract; 'Reader algorithm'
describes the reference reader and binds no one.

## Goal

One GeoParquet file is both the dataset and the map source. Every feature is
stored exactly once with its lossless geometry. A renderer reads, per tile,
bytes comparable to a pre-cut vector tile. A reader that does not know this
extension sees ordinary GeoParquet with two extra columns.

## Terms

- **Zoom**: Web Mercator zoom for 512 px tiles. `tile_size` in metadata
  records the assumption.
- **CRS**: zooms, grids and tiles are Web Mercator over longitude and
  latitude, so the layout requires the primary geometry in OGC:CRS84:
  `crs` omitted (GeoParquet's default), or an explicit OGC:CRS84 or
  EPSG:4326 in x/y order. An explicit null `crs` means unknown and does not
  qualify. LOD `scale` and `offset` are in degrees.
- **LOD**: one stored display representation (row selection, carried
  attributes and geometry) serving a contiguous range of zooms. Zooms may
  share an LOD only when row selection and carried attributes agree, and an
  LOD uses its finest zoom's grid.
- **LOD column**: one nullable struct per LOD holding, for each feature
  shown there, its geometry on the LOD's grid and the carried attributes as
  typed children.
- **Thinning**: the rule assigning each feature the coarsest zoom at which it
  is shown. A feature shown at zoom z is shown at every finer zoom. The
  reference writer thins only below a complete zoom; every feature is present
  from there up. This extension records the rule's description; it does not
  define rules.

## Layout

A conforming file MUST satisfy:

1. **Order.** Rows sorted by a space-filling curve over the primary
   geometry, within each of the two sections below. `order` names the curve:
   `hilbert_webmercator` is the standard Hilbert curve over the Web Mercator
   world square divided into `2^depth` cells per side, numbered as XYZ tiles
   are, on the cell holding the bbox centre with latitude clamped to the
   Mercator limit. The curve's quadtree prefix property makes a tile at zoom
   `z <= depth` with Hilbert index `h` the key interval
   `[h * 4^(depth - z), (h + 1) * 4^(depth - z))`, so a tile's rows are one
   contiguous run in each section. The key is stored in the `key` column
   with row group statistics and a page index; a reader finds a tile's row
   groups from the footer, its key pages from the page index, and its exact
   rows by reading the two boundary key pages, then reads only those rows of
   the LOD column. A feature centred outside a tile whose geometry reaches
   into the tile or its buffer (64/4096 of the tile) is a _straddler_; its
   row is in another interval, so each packed LOD row carries the copies it
   hosts: the tile each serves as `zoom << 48 | x * 2^zoom + y`, the
   feature's file row index, its geometry on that LOD's grid and its
   attributes. The writer MUST place every
   copy a tile needs on the tile's _host_: the first row of the main section
   whose key is at or past the tile's interval start, or the last row when
   none is, and a row shown only to host copies has an empty geometry. A
   shape under a pixel at the zoom draws as a placeholder square its own
   tile shows whole, so the reference writer copies only shapes a pixel or
   larger. A reader therefore reads the interval's rows plus the host row
   and keeps the copies whose `tile` is the tile's. The per-row
   uniform `u` the reference writer hands thinnings is a hash of the
   geometry bytes, so it does not depend on sort order.
2. **Prefix.** When `prefix` is present, every row whose coarsest zoom is at
   or below `prefix.zoom` is stored in row groups `0..prefix.row_group_end`,
   and no other row is. Those row groups are sorted on their own; the rest of
   the file is sorted on its own.
3. **Covering.** A `bbox` struct column of four float32 values with the
   GeoParquet 1.1 covering declaration, column statistics on every row
   group, and a column index (page statistics) and offset index on each of
   its four leaves. Values are rounded outward.
4. **Page indexes.** Every column has an offset index.
5. **LOD columns.** A top-level required struct column named by `column`
   with one nullable struct child per LOD, named by the LOD. The child is
   non-null for a row if and only if the row is shown at the LOD's zooms;
   since zooms sharing an LOD agree on row selection, that is one condition
   per LOD. Each child is a nullable `binary` column, one Parquet leaf,
   holding the row packed as Geometry describes: its geometry, its carried
   attributes and the straddler copies it hosts, see Layout 1. A row whose
   primary geometry is null or empty is shown at no LOD.

The validator checks the metadata, the schema shapes, covering statistics,
page indexes, that prefix row groups hold only prefix rows, and a sample
decode of every LOD from the first row group. It does not verify the sort
order or that every row is shown at the finest LOD; those are what the
reference writer does, and a conformance suite with fixtures for them is
open work.

The following SHOULD hold and are what the reference writer does:

- Row groups of about one million rows; data pages of about 16 KB
  uncompressed with v1 page headers. A reader fetches a row group's whole
  offset index per leaf it reads, so page count is a per-tile cost; 4 KB
  pages cost more there than they save in data, 64 KB the reverse.
- Compression that decodes in a browser (zstd); no dictionary encoding on
  LOD or bbox columns, though readers MUST handle a dictionary page when
  one is present; `BYTE_STREAM_SPLIT` on coordinate leaves.

## Metadata

Under the `geo` key, a `lod_columns` object:

| field          | type           | meaning                                                                                                                                                                                                            |
| -------------- | -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `version`      | string         | `0.1.0`; readers reject a different major version, and while the major is 0 a different minor as well, since minors may change the layout                                                                          |
| `column`       | string         | name of the LOD struct column                                                                                                                                                                                      |
| `order`        | string         | `hilbert_webmercator`, see Layout 1                                                                                                                                                                                |
| `key`          | object         | `{column, depth}`: the uint32 column holding each row's Hilbert index at `depth` of its Web Mercator cell, with statistics and a page index, see Layout 1                                                          |
| `zooms`        | int[]          | every zoom the file serves, ascending                                                                                                                                                                              |
| `tile_size`    | int            | pixels per tile the zooms assume (512)                                                                                                                                                                             |
| `prefix`       | object or null | `{zoom, row_group_end}`, see Layout 2                                                                                                                                                                              |
| `encoding`     | string         | `packed`, see Geometry                                                                                                                                                                                             |
| `pack_version` | int            | `1`, the packed row format version                                                                                                                                                                                 |
| `attributes`   | object[]       | `{name, type}` of the carried attributes; `type` is the Arrow type name, informational since the schema carries it                                                                                                 |
| `thinning`     | object         | description of the rule used; informational                                                                                                                                                                        |
| `lods`         | object         | LOD name to `{zooms, geometry_type, scale, offset, placeholder}`; `scale` and `offset` are `[x, y]` pairs in degrees, `placeholder` the width in grid cells of the square drawn for a shape too small for the grid |

All fields are required. Every zoom in `zooms` MUST appear in exactly one
LOD, `zooms` MUST be contiguous, and `scale` and `offset` MUST be finite. The
prefix's `zoom` MUST be in `zooms` and `row_group_end` MUST be less than the
row group count. Every row with a non-null, non-empty primary geometry MUST
be shown at the finest LOD. Membership at the other LODs is the writer's
choice per LOD and need not nest: a row may be shown zoomed out and hidden
one zoom in, as tippecanoe's per-zoom dropping produces. An LOD's zooms are
contiguous and ascending. Which zooms share an LOD is writer policy: the
reference writer gives each zoom its own LOD until the zoom from which the
thinning keeps every row, pairs zooms above it, and uses one LOD for all
complete zooms when the full-detail geometry is small.

### Example

For a building file with two carried attributes, under the `geo` key beside
the usual GeoParquet fields:

```json
"lod_columns": {
  "version": "0.1.0",
  "column": "lod",
  "order": "hilbert_webmercator",
  "zooms": [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16],
  "tile_size": 512,
  "prefix": { "zoom": 6, "row_group_end": 0 },
  "encoding": "packed",
  "pack_version": 1,
  "attributes": [{ "name": "height", "type": "float" }, { "name": "class", "type": "string" }],
  "thinning": { "rule": "combine(size_threshold(4), drop_rate(2.5))" },
  "lods": {
    "f8": { "zooms": [8], "geometry_type": "MultiPolygon", "scale": [6.8665e-4, 6.8665e-4], "offset": [-105.1, 39.6], "placeholder": 1 },
    "f13": { "zooms": [13, 14], "geometry_type": "MultiPolygon", "scale": [1.0729e-5, 1.0729e-5], "offset": [-105.1, 39.6], "placeholder": 1 }
  }
}
```

and the physical schema, with `geometry` and the source attributes as the
source had them:

```
geometry: binary (WKB)
height: float
class: string
bbox: struct<xmin: float, ymin: float, xmax: float, ymax: float> not null
lod: struct<
  f8: struct<
    geometry: list<list<list<list<int32 not null> not null> not null> not null> not null,
    height: float,
    class: string
  >,
  f13: struct<...same shape...>
> not null
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
                   vertex from the origin
  MultiLineString: n parts, per part: vertices, steps as above
  Point:           zigzag(x) zigzag(y)
attrs  := presence bitmap (one varint, bit i for attribute i in metadata
          order), then each present value: integers zigzag, float32 as its
          bit pattern, float64 as its bit pattern, bool 0/1, string as its
          byte length
spill  := count, per copy: tile (zoom << 48 | x * 2^zoom + y), file row
          index, geometry, attrs
```

`zigzag(v)` is `(v << 1) ^ (v >> 63)`. A decoded grid coordinate is
`offset + scale * integer` in degrees, with `scale` and `offset` per LOD:
the transform and grid of COGP's `quantized_geoarrow`, in one private byte
format a COGP reader cannot decode. Every ring is closed, every part has
at least two vertices, and a row shown only to host copies has zero
polygons; empty geometry is otherwise never written. Ring steps make a
building a handful of one-byte varints: a placeholder square packs to about
six bytes before compression, a footprint to about two bytes per vertex.
The reference writer uses a quarter of the LOD's finest zoom pixel as
`scale` and the dataset's minimum corner as the origin. Quantization rounds
half away from zero.

A family keeps its geometry type at every zoom. A shape too small for an
LOD's grid in both directions is a square on the grid cell holding its
centroid, `placeholder` cells wide and as many cells tall as draw square on
a Mercator screen at its latitude, half a pixel unless the thinning asks
for another size, so a thinned set of buildings zoomed out reads as a faint
texture of its density, as tippecanoe's tiny-polygon squares do. The grid
is a quarter pixel at the LOD's finest zoom, in nominal Web Mercator pixels
at the equator, close to tippecanoe's eighth-pixel tolerance; features a
pixel or two across keep their own shape.

Carried attributes keep their declared Arrow types and nulls. A thinning
that overrides an attribute writes the overridden value, cast to the
declared type, into that LOD's rows; copies carry the source row's values.

## Reader algorithm

1. Read the footer. The reference HTTP source first probes for the optional
   [spatial index profile](SPATIAL-INDEX.md), which can bypass this algorithm
   for LOD tiles. Otherwise it reads a 64 KiB tail and any missing footer bytes.
   Validate `lod_columns`. Start fetching the prefix row groups' key and
   LOD offset indexes.
2. For a tile at zoom z choose the LOD containing z, clamping to the
   coarsest LOD below `zooms`. Finer than the finest zoom, read the primary
   geometry instead: every row is shown at the finest LOD, so the bbox pages
   alone choose the rows, and their exact WKB is small at those zooms.
3. The tile's key interval, see Layout 1. Candidate row groups: those whose
   key statistics intersect it, plus the host group, the first main-section
   group whose maximum key is at or past the interval start. If the LOD's
   finest zoom is at or below `prefix.zoom`, only the prefix row groups.
4. In each group, the key pages whose statistics intersect the interval;
   read the first and last page of each run, and the host page, to find
   the exact rows, and the first row at or past the interval start as the
   host.
5. Read the LOD column's pages overlapping those rows, merging adjacent
   pages into one range. Decode each row: a row with geometry is a
   feature, and each hosted copy whose tile is this tile is a feature with
   the copy's row index as its id.
6. Points are clipped to the tile; polygons and lines are clipped by the
   tile encoder. The MVT feature id is the file row index.

No bbox statistics or pages are read for an LOD tile; the covering serves
analysis readers and the primary geometry fallback.

## Choosing zooms

The extension does not fix zooms. The reference writer derives them from the
data when not given: the coarsest zoom is the one at which the dataset extent
fits a tile; shape LODs start at the zoom where the median feature spans
one pixel; the finest zoom is where the median vertex spacing spans four
pixels, capped at 18. Writers SHOULD record the zooms in `zooms` and MAY use
any rule.

## Analysis readers

Nothing here changes the primary geometry or attribute columns. `SELECT
count(*)`, spatial joins and attribute queries are correct with no filter.
The `bbox` column is the standard 1.1 covering. The LOD column is a
struct of structs a reader can drop.

## Relation to other proposals

- GeoParquet PR 305 (COGP, Kanahiro) orders row groups coarse to fine and
  declares each level as a cumulative row-group prefix; geometry is stored
  once and an optional overview column may carry simplified shapes. Reading
  a fine level means reading every coarser level's row groups that touch the
  view, so requests grow with the level count. This layout keeps one such
  prefix and one global order, and moves zoom selection into nullable LOD
  structs, so a tile touches few row groups at every zoom: one or two on
  the building datasets measured, more where large geometries span groups.
  Our `prefix` has the shape of a COGP level but not its contract, since
  COGP's last level is the whole table and ours is one coarse slice. Our
  LOD geometry keeps COGP's `quantized_geoarrow` transform and grid, with
  `Point` added, but packs each row into one binary value, so a COGP reader
  cannot decode it and nothing is shared beyond the transform.
- GeoParquet PR 299 (LOD extension, mmgeorge) declares simplified geometry
  columns with a resolution each, nested paths allowed, with the primary
  geometry as the fallback. What this layout adds is row selection per LOD
  and per-LOD attribute state, which its relationships cannot express; the
  metadata could become a PR 299 extension once that is settled. Our LOD
  geometry is integer rather than a GeoParquet primary encoding for the
  same reason COGP's overviews are: integer grids compress several times
  better than float64 WKB and decode without a parser.
- Esri Spatially Optimized Parquet, as its repository describes it, clusters
  rows spatially, relies on page indexes, and adds multiscale LOD columns
  for complex geometries. We have not found a published rule for which rows
  appear at coarse scales.
- GeoParquet 2.0: orthogonal. The primary geometry is WKB in both 1.1 and
  2.0; 2.0 annotates it with Parquet's `GEOMETRY` type and gains row-group
  geospatial statistics. 2.0 allows no other primary encoding, which is why
  the GeoArrow-shaped layout appears only in the LOD columns. Page pruning
  still needs the bbox covering under either version.

## Open questions

- Whether `quantized_geoarrow` should admit a `Point` type, or point LODs
  should use a GeoArrow point struct in degrees at float64.
- Partitioned datasets: one manifest listing files and their extents.
