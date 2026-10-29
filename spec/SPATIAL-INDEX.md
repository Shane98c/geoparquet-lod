# GeoParquet LOD spatial page index, version 3

An optional physical profile of a packed LOD file. The logical
`geo.lod_columns` metadata and all table values are unchanged; the profile
decides where column chunks and pages sit and adds directories over the
overview pages and over the primary geometry's own pages, so a reader finds a
tile's pages from the first 16 KiB of the file without reading the footer,
at every zoom and past the finest overview. It is not a standardized
GeoParquet extension. Compatibility has been exercised with PyArrow, DuckDB,
and the hyparquet-based reader in this repository.

## File layout

```text
PAR1
LODIDX01 | root_length:u32le | gzip(root JSON) | padding to byte 16384
column chunks, physically grouped by column, then logical row group
standard Parquet offset indexes, then column indexes, each by row group
gzip directory nodes: leaves and optional branches
gzip row metadata
standard Parquet footer | footer_length:u32le | PAR1
```

The footer retains the original row groups, columns, schema, statistics, and
GeoParquet metadata, with relocated offsets and updated physical sizes.
`lod_spatial_index` in its key/value metadata advertises the profile version
and bootstrap location, `{"version":3,"offset":4,"length":16380}`. Primary
geometry and source attribute chunks are copied byte for byte. LOD chunks are
re-encoded in the original row order, with the same nullable binary schema and
zstd, with page breaks at the LOD's finest tile grid. Every LOD page is a v1
data page (a `DATA_PAGE` header) of PLAIN values with RLE/bit-packed hybrid
definition levels, and LOD chunks have no dictionary page, so each page decodes
on its own. Multiple pages may serve a dense tile, and a row-group boundary can
split a tile. The row cap scales with the chunk's LOD density to target 2,048
present values, up to 262,144 analytical rows per page, alongside an
uncompressed byte target (64 KiB by default). The target is advisory: write
batches and individual large values may exceed it. Runs of entirely null tiles
are encoded together and omitted from the spatial directory; standard offset
indexes still describe every page.

Every column chunk remains contiguous; pages from different chunks are not
interleaved. A tile's data is one contiguous range of its LOD column, so a
cold tile normally costs one directory read and one data read; row-group and
directory boundaries can add reads.

## Bootstrap

The compressed root is bounded by the first 16 KiB and contains:

- `version: 3`, `file_bytes`: format and full file length checks.
- `footer_bytes`: the length of the standard footer's metadata, so a reader
  fetches the footer and its trailer in one request.
- `row_metadata`: `[byte_offset, compressed_length]` of the row metadata, see
  below.
- `lod`, `primary`: copies of the logical LOD metadata and primary column name.
- `bounds`, `columns`: map extent and the scalar (non-nested) columns other
  than the primary geometry, including the key column, so a map can
  initialize without another reader open.
- `codecs`: LOD name to its Parquet compression codec.
- `directories`: LOD name to the top node of its directory, at level one or
  above.
- `primary_index`: the [primary directory](#primary-directory) and what
  decodes the pages it locates.

## Directory nodes

Each LOD's directory is a tree over its pages in key order. The root holds the
top node; the nodes below are gzip JSON. Every node is a JSON array of its
level and parallel columns with one value per entry:

```text
[level, cells, spans, gaps, lengths]                                level > 0
[0, cells, spans, gaps, lengths, rows, counts, fences, nulls]       a leaf
```

A leaf's entries are pages; the entries of a node at level `L > 0` are nodes
at level `L - 1`, and no level exceeds 32. Positions are delta coded against
the previous entry, and the first entry's against zero:

- `cells`: the entry's minimum cell minus the previous entry's.
- `spans`: its maximum cell minus its minimum cell.
- `gaps`: its byte offset minus the previous entry's end.
- `lengths`: its compressed length in bytes.
- `rows`: the page's first global row minus the previous page's end row.
- `counts`: the page's rows.
- `fences`: the page's fences after the implied first `[minimum cell, 0]`, a
  flat array of `[cell, row]` pairs, each minus the pair before. A fence marks
  the first row, counted within the page, of each run of rows in one cell.
- `nulls`: the page's trailing rows whose key is null.

Cells are Hilbert cells at `z = min(key.depth, finest zoom of the LOD)`: key
`k` lies in cell `floor(k / 4^(key.depth - z))`, and a null key counts as cell
`4^z`, past every other. Entries are in key order: each entry's minimum cell
is at least the previous entry's maximum. A node's minimum and maximum cell
are its first entry's minimum and its last entry's maximum, and every node
below the top is located by exactly one entry, which gives its byte range and
those two cells. An entry with cells `lo..hi` stands for the keys from
`lo * 4^(key.depth - z)` to `(hi + 1) * 4^(key.depth - z) - 1`. Every tile
served from the LOD, at its zooms or coarser, is a whole number of cells, so
these bounds select the same entries exact key bounds would.

Page fences enforce exact tile membership without fetching the key column;
tile-aligned pages have only the implied fence. Rows past `counts - nulls`
have null keys: they remain in the analytical table and have null LODs. The
reference writer puts 1,024 pages in a leaf and, while the root would exceed
the bootstrap, adds a level of branches of 64 entries to the tree whose top
node is largest, the primary directory included.

## Lookup

To read a tile, descend from the LOD's top node: in every node, take the
entries whose bounds intersect the tile's key interval, plus the first entry
whose maximum reaches the interval start, or the final entry if none does.
Among the pages, that first page contains the tile's copy host when any copies
exist. Read consecutive pages together. Decode each page with the root's codec
and the required-struct/nullable-binary schema, scanning its definition-level
runs to skip null rows. Use the fences to retain own rows inside the tile
interval, and the host row for its copies tagged with the requested tile.
Global row indices are the feature ids. The directory stores no geometry or
styling payload and is not an MVT archive.

## Primary directory

Past the finest LOD a map draws the primary geometry itself, which no
overview copies. Its pages, copied byte for byte and never re-paged, are
located by a directory routed by key, as the LOD directories are, whose leaves
list runs of geometry rows with the box of each. A feature reaching into a
tile from outside it is found by its box, so the primary geometry needs no
boundary copies. The root's `primary_index` holds:

- `attributes`: carried attributes that are flat source columns, whose pages
  each leaf also locates. The reference writer lists those whose chunks have
  offset indexes, no dictionary page and one codec.
- `codecs`: the Parquet codec of the primary geometry, then of each column in
  `attributes`, the same in every row group.
- `schema`: base64 of a Thrift `FileMetaData` whose schema holds the root
  element, then the elements of the primary geometry and of each column in
  `attributes`, with no row groups. With `codecs` it decodes their pages
  without the footer.
- `directory`: the top node, at level one or above.

Its cells are Hilbert cells at `z = min(key.depth, finest zoom)`, the finest
LOD's. The nodes above its leaves are directory nodes as above,
`[level, cells, spans, gaps, lengths]`, and the leaves' cells, in order, are
every cell at `z` from 0 to `4^z - 1`, each in one leaf. A leaf's _region_ is
its cells' tiles at zoom `z`.

Boxes are whole cells of 2^-12 degrees, about 27 m of latitude: a longitude
`x` lies in cell column `floor((x + 180) * 4096)` and a latitude `y` in cell
row `floor((y + 90) * 4096)`, computed in IEEE double precision, for a
covering value from its float32. A row's box is the cells of its covering's
minimum and maximum, inclusive, so it contains the covering. A row has an
extent when its four covering values are present and finite with each
minimum at most its maximum; null and empty geometries have none. A tile's
cells are those of its edges: tile column `t` at zoom `z` spans the cell
columns of the longitudes `t * 360 / 2^z - 180` to `(t + 1) * 360 / 2^z - 180`,
and tile row `t` the cell rows of the latitudes
`atan(sinh(pi * (1 - 2 * (t + 1) / 2^z)))` to
`atan(sinh(pi * (1 - 2 * t / 2^z)))` in degrees, the first row reaching
latitude 90 and the last -90. A box reaches a tile, or a region, when it
shares a cell with it.

A leaf is a JSON array of level 0 and parallel columns with one value per
entry, the first entry's values against zero:

```text
[0, wests, souths, widths, heights, gaps, lengths, rows, counts, sources]
```

Each entry is a run of one geometry page's rows, in row order; a page's runs
are consecutive entries holding all its rows.

- `wests`, `souths`: the run's box's minimum cell column and row minus the
  previous entry's.
- `widths`, `heights`: its maximum cell column and row minus its minimum.
- `gaps`, `lengths`: its page's byte offset minus the end of the previous
  entry's page, and its page's compressed length; both 0 when its page is
  the previous entry's.
- `rows`, `counts`: its first row minus the previous entry's end row, and its
  rows.
- `sources`: per column in `attributes`, its pages overlapping the rows of the
  leaf's runs, as `[gaps, lengths, rows, counts]` coded as the LOD
  directory's pages.

A run's box is the union of its rows' boxes, and it holds a row with an
extent. A leaf lists every page holding a row whose box reaches its region,
and a page's runs are the same in every leaf that lists it. When the Hilbert
curve leaves the data and re-enters it far away, one page can hold rows of
distant places; its runs keep the empty space between them out of the
lookup. The reference writer cuts a page in two at the row where the halves'
boxes cover the fewest tiles of 32 cells, when they cover at most 70% of those
one box over all its rows would, then each half once more the same way. It
ends a leaf after about 1,024 pages, before the coarsest aligned cell among
those of its last half of pages, so a leaf's region is a few large tiles: a
tile past the finest LOD lies in one leaf, and its buffer seldom reaches
another.

To read the rows whose bbox intersects a WGS84 query, a tile's buffered bounds
for an MVT tile, descend from the top node into every entry whose cells hold a
tile at zoom `z` sharing a point with the query; the reference reader takes
the tiles at the finest zoom up to `z` at which those holding the query's
corners, latitudes clamped to the Mercator limit, span at most two columns and
two rows. In each leaf reached, take the runs whose boxes meet the query's
box, the cells of its corners as above. A row whose covering intersects the
query shares a point with it; the row's box reaches the tile holding that
point, so the tile's leaf lists the row's page, and the row's run is taken.
Read each page once, adjacent pages together. Decode each page with the
primary's codec and schema element; a page is any data page but a
dictionary-encoded one, of exactly its runs' rows. Keep the rows of the taken
runs whose geometry is present and whose bbox intersects the query: the
reference reader takes it from the WKB, rounded outward to float32, which
[Layout](SPEC.md#layout) 2 makes the covering, so it keeps the rows the
covering would. Alongside the geometry, read the pages of each needed
attribute that the same leaves locate over the taken runs' rows. These
columns are flat, so a page's header counts one value, null or not, per row:
a v1 header's values and a v2 header's values and rows are the page's rows,
and a v2 header's nulls at most as many.

## Analytical access and publishing

Exact source-row reads open the normal Parquet footer lazily; the standard
page indexes describe every page, so ordinary Parquet readers see an
ordinary file. A row group's offset indexes are adjacent, so a row read
across columns fetches them in one request.

The row metadata is the footer's Thrift `FileMetaData`, gzip-compressed and
cut down to the flat (non-nested, non-repeated) top-level columns, the primary
geometry included: the schema root and those columns' elements; every row
group with only those columns' chunks, each keeping its file offset,
required column metadata, data and dictionary page offsets, and offset index
location, without statistics, encoding statistics, size statistics or column
index; and only the `geo` key/value. Row-group totals cover the kept chunks,
and `sorting_columns`, `column_orders` and `created_by` are omitted. A reader
reads rows of those columns from it without fetching the footer, and opens the
footer for any other column. It is derived from the final footer, which stays
authoritative; a validator checks the two agree.

Publish files immutably: the profile is
specific to one file write, and ordinary Parquet writers need not preserve it.
The reference `write()` builds the layout as its final step, writes to
temporary files, and publishes only after the completed footer is readable.
Its layout pass rejects encrypted or external chunks, bloom filters and legacy
index pages.

The reference reader's caching, worker pool, and cancellation behaviour are
described in the [usage guide](../docs/usage.md#reader-behaviour); none of it
changes the stored bytes.
