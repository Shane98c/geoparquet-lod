# GeoParquet LOD spatial page index, version 2

An optional physical profile of a packed LOD file. The logical
`geo.lod_columns` metadata and all table values are unchanged; the profile
decides where column chunks and pages sit and adds a directory over the
overview pages so a reader finds a tile's pages from the first 16 KiB of the
file without reading the footer. It is not a standardized GeoParquet
extension. Compatibility has been exercised with PyArrow, DuckDB, and the
hyparquet-based reader in this repository.

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
and bootstrap location, `{"version":2,"offset":4,"length":16380}`. Primary
geometry and source attribute chunks are copied byte for byte. LOD chunks are
re-encoded in the original row order, with the same nullable binary schema and
zstd, without dictionary encoding, with page breaks at the LOD's finest tile
grid. Multiple pages may serve a dense tile, and a row-group boundary can split
a tile. The row cap scales with the chunk's LOD density to target 2,048
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

- `version: 2`, `file_bytes`: format and full file length checks.
- `footer_bytes`: the length of the standard footer's metadata, so a reader
  fetches the footer and its trailer in one request.
- `row_metadata`: `[byte_offset, compressed_length]` of the row metadata, see
  below.
- `lod`, `primary`: copies of the logical LOD metadata and primary column name.
- `bounds`, `columns`: map extent and the scalar (non-nested) columns other
  than the primary geometry, including the key column, so a map can
  initialize without another reader open.
- `codecs`: LOD name to its Parquet compression codec.
- `streams`: LOD name to a list of directory series, each a directory node at
  level one or above. The reference writer emits one series per LOD; readers
  search every series for a tile's own pages and take its copy host from the
  last non-empty one.

## Directory nodes

Each series is the top node of a tree over its LOD's pages in key order. The
root holds the series; the nodes below are gzip JSON. Every node is a JSON
array of its level and parallel columns with one value per entry:

```text
[level, cells, spans, gaps, lengths]                                level > 0
[0, cells, spans, gaps, lengths, rows, counts, fences, nulls]       a leaf
```

A leaf's entries are pages; the entries of a node at level `L > 0` are nodes
at level `L - 1`. Positions are delta coded against the previous entry, and
the first entry's against zero:

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
`4^z`, past every other. A node's minimum and maximum cell are its first
entry's minimum and its last entry's maximum. An entry with cells `lo..hi`
stands for the keys from `lo * 4^(key.depth - z)` to
`(hi + 1) * 4^(key.depth - z) - 1`. Every tile served from the LOD, at its
zooms or coarser, is a whole number of cells, so these bounds select the same
entries exact key bounds would.

Page fences enforce exact tile membership without fetching the key column;
tile-aligned pages have only the implied fence. Rows past `counts - nulls`
have null keys: they remain in the analytical table and have null LODs. The
reference writer puts 1,024 pages in a leaf and, while the root would exceed
the bootstrap, adds a level of branches of 64 entries to each series.

## Lookup

To read a tile, descend from each series: in every node, take the entries
whose bounds intersect the tile's key interval, plus the first entry whose
maximum reaches the interval start, or the final entry if none does. Among the
pages, that first page contains the tile's copy host when any copies exist.
Read consecutive pages together. Decode the standard Parquet page with the
root's codec and the required-struct/nullable-binary schema. Use the fences to
retain own rows inside the tile interval, and the host row for its copies
tagged with the requested tile. Global row indices are the feature ids. A reader can scan
definition-level runs in v1 PLAIN pages directly and skip null rows, falling
back to a general decoder for other encodings. The directory stores no
geometry or styling payload and is not an MVT archive.

## Analytical access and publishing

Exact source-row reads and tiles beyond the finest LOD open the normal Parquet
footer lazily; the standard page indexes describe every page, so ordinary
Parquet readers see an ordinary file. A row group's offset indexes are
adjacent, so a row read across columns fetches them in one request.

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
