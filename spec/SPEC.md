# LOD columns

*Version 0.2.0. A layout and metadata contract for GeoParquet files that
serve a zoomable map directly. Implemented by the writer and reader in this
repository.*

## Goal

One GeoParquet file is both the dataset and the map source. Every feature is
stored exactly once with its lossless geometry. A renderer reads, per tile,
bytes comparable to a pre-cut vector tile. A reader that does not know this
extension sees ordinary GeoParquet with two extra columns.

## Terms

- **Zoom**: Web Mercator zoom for 512 px tiles. `tile_size` in metadata
  records the assumption.
- **LOD**: one stored display representation (row selection, carried
  attributes and geometry) serving a contiguous range of zooms. Zooms may
  share an LOD only when row selection and carried attributes agree, and an
  LOD uses its finest zoom's grid.
- **Payload**: one binary value per feature per LOD: carried attributes,
  then geometry on the LOD's grid.
- **Thinning**: the rule assigning each feature the coarsest zoom at which it
  is shown. A feature shown at zoom z is shown at every finer zoom. This
  extension records the rule's description; it does not define rules.

## Layout

A conforming file MUST satisfy:

1. **Order.** Rows sorted by a space-filling curve over the primary
   geometry, within each of the two sections below. `order` names the curve
   (`hilbert`).
2. **Prefix.** When `prefix` is present, every row whose coarsest zoom is at
   or below `prefix.zoom` is stored in row groups `0..prefix.row_group_end`,
   and no other row is. Those row groups are sorted on their own; the rest of
   the file is sorted on its own.
3. **Covering.** A `bbox` struct column of four float32 values with the
   GeoParquet 1.1 covering declaration, column statistics on every row
   group, and a column index (page statistics) and offset index on each of
   its four leaves. Values are rounded outward.
4. **Page indexes.** Every column has an offset index.
5. **Payload column.** A top-level non-nullable struct column named by
   `column` with one nullable `BINARY` child per LOD, named by the LOD. A
   child is non-null for a row if and only if the row is shown at the LOD's
   zooms; since zooms sharing an LOD agree on row selection, that is one
   condition per LOD.

The following SHOULD hold and are what the reference writer does:

- Row groups of about one million rows; data pages of about 4 KB
  uncompressed with v1 page headers, so definition levels compress with
  the values and sparse columns get long cheap pages.
- Compression that decodes in a browser (zstd); no dictionary encoding on
  payload or bbox columns.

## Metadata

Under the `geo` key, a `lod_columns` object:

| field | type | meaning |
| --- | --- | --- |
| `version` | string | `0.2.0`. Readers MUST reject other major or minor versions. |
| `column` | string | name of the payload struct column |
| `order` | string | `hilbert` |
| `zooms` | int[] | every zoom the file serves, ascending |
| `tile_size` | int | pixels per tile the zooms assume (512) |
| `prefix` | object or null | `{zoom, row_group_end}`, see Layout 2 |
| `attributes` | object[] | `{name, type}` in payload order, see Attributes |
| `thinning` | object | description of the rule used; informational |
| `lods` | object | LOD name to `{zooms, geometry, scale?, offset?}` |

Every zoom in `zooms` MUST appear in exactly one LOD. An LOD's zooms are
contiguous and ascending. Which zooms share an LOD is writer policy: the
reference writer gives each zoom below the shape zoom its own LOD, pairs
zooms above it, and uses one LOD for all shape zooms when the full-detail
payload is small.

## Payload encoding

A payload value is the attribute block followed by the geometry block, with
no padding and no trailing bytes.

### Attributes

Little-endian, in `attributes` order:

| type | bytes |
| --- | --- |
| `f32`, `f64`, `i32`, `u32`, `i16`, `u16`, `u8`, `i8` | fixed width |
| `utf8` | one length byte, then that many UTF-8 bytes (max 255) |
| `f32[]`, `i32[]`, `u8[]` | one count byte, then that many items |

A fixed-width float that is NaN means the value is unknown. Other nulls are
not representable; writers MUST choose a sentinel or omit the attribute.

### Geometry

- `point_f32`: two float32, x then y, in CRS units. The point is the
  feature's representative point (centroid for polygons).
- `polygon_varint`: the feature as a MultiPolygon on the LOD's grid.
  Unsigned LEB128 varints: polygon count; per polygon, ring count; per ring,
  vertex count, then for each vertex x and y as zigzag-encoded deltas from
  the previous vertex, the first vertex of each ring absolute. Rings are
  closed (first and last vertex equal). A feature too small for the grid is
  a two-cell square around its centroid. Decoded coordinates are
  `offset + scale * integer`, with `scale` and `offset` per LOD in CRS
  units, so `scale` is the LOD's finest zoom pixel size.
- `line_varint`: the same tree for a MultiLineString; each part is one
  unclosed ring.

## Reader algorithm

1. Read the footer. Validate `lod_columns`. Start fetching the page indexes
   of the prefix row groups' bbox leaves.
2. For a tile at zoom z choose the LOD containing z, clamping to the
   coarsest or finest LOD outside `zooms`.
3. Candidate row groups: those whose bbox column statistics intersect the
   tile. If the band's finest zoom is at or below `prefix.zoom`, only the
   prefix row groups.
4. Candidate pages: for each bbox leaf, the pages whose statistics admit
   the tile; intersect the four row ranges.
5. If the LOD column chunk is smaller than its page index would be, read
   the chunk whole and cache it. Otherwise read only the LOD pages
   overlapping the candidate rows, merging adjacent pages into one range.
6. Decode non-null values. Points are clipped to the tile; polygons are
   clipped by the tile encoder. The MVT feature id is the file row index.

Optional refinement: read the candidate bbox pages first and keep only rows
whose bbox intersects. Worth it when payloads are large relative to a bbox
page, such as region outlines.

## Choosing zooms

The extension does not fix zooms. The reference writer derives them from the
data when not given: the coarsest zoom is the one at which the dataset extent
fits a tile; shape LODs start at the zoom where the median feature spans
one pixel; the finest zoom is where the median vertex spacing reaches one
pixel, capped at 18. Writers SHOULD record the zooms in `zooms` and MAY use
any rule.

## Analysis readers

Nothing here changes the primary geometry or attribute columns. `SELECT
count(*)`, spatial joins and attribute queries are correct with no filter.
The `bbox` column is the standard 1.1 covering. The payload column is a
struct of binaries a reader can drop.

## Relation to other proposals

- GeoParquet PR 299 (LOD columns) defines per-resolution geometry columns
  and resolution selection. This extension is compatible in spirit and adds
  the two rules it lacks: null means not shown, and a coarse prefix. Its
  requirement that each LOD column be a standard geometry encoding is not
  met by `polygon_varint`; a future version may declare the LOD geometry
  as a GeoParquet native encoding instead.
- COGP (PR 305) stores each feature once in level blocks. It is cheap zoomed
  out and expensive zoomed in; this layout is cheap at both ends.
- Esri SOP uses one global order and per-scale columns with no thinning. It
  is cheap zoomed in and unbounded zoomed out.
- GeoParquet 2.0: orthogonal. The primary geometry may use the native
  `GEOMETRY` type; row group pruning may use native geospatial statistics.
  Page pruning still needs the bbox covering.

## Open questions

- A standard geometry encoding for LODs.
- Whether attributes should be a nested struct column per band instead of
  packed bytes, trading one range per tile for native typing.
- Partitioned datasets: one manifest listing files and their extents.
