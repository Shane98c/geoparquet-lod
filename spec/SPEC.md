# LOD columns

_A layout and metadata contract for GeoParquet files that serve a zoomable
map directly. Implemented by the writer and reader in this repository. Draft 0.1.0._

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
   geometry, within each of the two sections below. `order` names the curve
   (`hilbert`). The per-row uniform `u` the reference writer hands thinnings is
   a hash of the geometry bytes, so it does not depend on sort order.
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
   per LOD. Each child has a required `geometry` field in the encoding
   below and one nullable field per carried attribute, in `attributes`
   order, each a scalar Arrow type stored as one Parquet leaf. A row whose
   primary geometry is null or empty is shown at no LOD.

The validator checks the metadata, the schema shapes, covering statistics,
page indexes, that prefix row groups hold only prefix rows, and a sample
decode of every LOD from the first row group. It does not verify the sort
order, that every row is shown at the finest LOD, or that membership nests
across LODs; those are what the reference writer does, and a conformance
suite with fixtures for them is open work.

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

| field         | type           | meaning                                                                                                                                   |
| ------------- | -------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `version`     | string         | `0.1.0`; readers reject a different major version, and while the major is 0 a different minor as well, since minors may change the layout |
| `column`      | string         | name of the LOD struct column                                                                                                             |
| `order`       | string         | `hilbert`                                                                                                                                 |
| `zooms`       | int[]          | every zoom the file serves, ascending                                                                                                     |
| `tile_size`   | int            | pixels per tile the zooms assume (512)                                                                                                    |
| `prefix`      | object or null | `{zoom, row_group_end}`, see Layout 2                                                                                                     |
| `encoding`    | string         | `quantized_geoarrow`, see Geometry                                                                                                        |
| `coordinates` | string         | `interleaved`: each vertex is one list of two int32, x then y                                                                             |
| `attributes`  | object[]       | `{name, type}` of the carried attributes; `type` is the Arrow type name, informational since the schema carries it                        |
| `thinning`    | object         | description of the rule used; informational                                                                                               |
| `lods`        | object         | LOD name to `{zooms, geometry_type, scale, offset}`; `scale` and `offset` are `[x, y]` pairs in degrees                                   |

Every zoom in `zooms` MUST appear in exactly one LOD. An LOD's zooms are
contiguous and ascending. Which zooms share an LOD is writer policy: the
reference writer gives each zoom below the complete zoom its own LOD, pairs
zooms above it, and uses one LOD for all complete zooms when the full-detail
geometry is small.

## Geometry

The `geometry` field of every LOD child is COGP's `quantized_geoarrow`
encoding in GeoArrow's interleaved coordinate layout: nested Parquet lists
whose innermost element is a required list of exactly two int32, x then y;
`MultiPolygon` three lists deep above that (polygons, rings, vertices) and
`MultiLineString` two (lines, vertices), with every nested list non-null and
non-empty and every ring closed. COGP stores a `struct<x, y>` instead; the
interleaved pair makes a geometry one Parquet leaf, so a tile reads one page
run and one offset index for it rather than two, at about 6 percent more
bytes on buildings since the pair cannot be delta coded (the leaf uses
`BYTE_STREAM_SPLIT`). A decoded coordinate is `offset + scale *
integer` in degrees, with `scale` and `offset` per LOD; the reference
writer uses a quarter of the LOD's finest zoom pixel as `scale` and the
dataset's minimum corner as the origin. Quantization rounds half away from
zero.

`Point` is the bare coordinate pair with the same transform, used only for
point datasets. COGP does not define a point family; this is our extension,
to be proposed there. A family keeps its geometry type at every zoom. A shape
too small for an LOD's grid is a square around its centroid, half a pixel
wide unless the thinning asks for another size, so a thinned set of
buildings zoomed out reads as a faint texture of its density, as tippecanoe's
tiny-polygon squares do. The grid is a quarter pixel at the LOD's finest
zoom, in nominal Web Mercator pixels at the equator, close
to tippecanoe's eighth-pixel tolerance; features a pixel or two across keep
their own shape. On buildings that costs about twice the geometry bytes of a
whole-pixel grid at the zooms where shapes are barely visible.

Carried attribute fields keep their Arrow types and nulls. A thinning that
overrides an attribute writes the overridden value, cast to the declared
type, into that LOD's field.

## Reader algorithm

1. Read the footer. Validate `lod_columns`. Start fetching the page indexes
   of the prefix row groups' bbox leaves.
2. For a tile at zoom z choose the LOD containing z, clamping to the
   coarsest LOD below `zooms`. Finer than the finest zoom, read the primary
   geometry instead: every row is shown at the finest LOD, so the bbox pages
   alone choose the rows, and their exact WKB is small at those zooms.
3. Candidate row groups: those whose bbox column statistics intersect the
   tile. If the LOD's finest zoom is at or below `prefix.zoom`, only the
   prefix row groups.
4. Candidate pages: for each bbox leaf, the pages whose statistics admit
   the tile; intersect the four row ranges.
5. The leaves to read are the LOD's coordinate leaf and one leaf per wanted
   attribute. If their chunks together are smaller than
   their page indexes would be, read them whole and cache them. Otherwise
   read only the pages overlapping the candidate rows, merging adjacent
   pages into one range per leaf.
6. Rows present in the coordinate leaf are the shown rows. Points are
   clipped to the tile; polygons are clipped by the tile encoder. The MVT
   feature id is the file row index.

Refinement, on by default in the reference reader: read the candidate bbox
pages and keep only rows whose bbox intersects before fetching LOD pages. A
bbox page holds thousands of rows and a fine tile wants a few hundred, so
this roughly halves the bytes at street zooms for a few more requests. The
tile bounds are padded by one grid cell first, since a placeholder square is
centred on the feature and can reach one cell past its bbox.

A reader may precompute from the page indexes which pages each tile needs
at each zoom and keep that beside the file as its own cache. This
repository's tooling writes such a table as `<file>.tiledir.parquet`, and its
reader uses one when pointed at it. Nothing in the file refers to it, and the
file is complete without it.

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
  LOD geometry reuses COGP's `quantized_geoarrow` coordinate layout and
  transform rules inside an attribute-bearing struct, with `Point` added;
  it is a profile of that encoding, not the encoding itself.
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
