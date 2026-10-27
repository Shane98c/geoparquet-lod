# GeoParquet LOD spatial page index, version 1

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
gzip spatial directory leaves and optional branches
standard Parquet footer | footer_length:u32le | PAR1
```

The footer retains the original row groups, columns, schema, statistics, and
GeoParquet metadata, with relocated offsets and updated physical sizes.
`lod_spatial_index` in its key/value metadata advertises the bootstrap
location. Primary geometry and source attribute chunks are copied byte for
byte. LOD chunks are re-encoded in the original row order, with the same
nullable binary schema and zstd, with page breaks at the LOD's finest tile
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

- `version: 1`, `file_bytes`: format and full file length checks.
- `footer_bytes`: the length of the standard footer's metadata, so a reader
  fetches the footer and its trailer in one request.
- `lod`, `primary`: copies of the logical LOD metadata and primary column name.
- `bounds`, `columns`: map extent and the scalar (non-nested) columns other
  than the primary geometry, including the key column, so a map can
  initialize without another reader open.
- `codecs`: LOD name to its Parquet compression codec.
- `directory_encoding: "delta"` and `streams`: LOD name to a list of directory
  series. The reference writer emits one series per LOD; readers search every
  series for a tile's own pages and take its copy host from the last non-empty
  one.

Each series is `[initial_byte_offset, depth, triples]`. Triples are a flat
array of `[delta_min_cell, max_cell_minus_min_cell, compressed_length]`. The
first minimum is relative to zero; subsequent minima are relative to the
previous one. Shards are contiguous, so their byte offsets follow from their
lengths. Cells are Hilbert cells at `min(key.depth, finest_LOD_zoom)`.
Directory bounds reconstructed from cells conservatively include whole cells.

A depth-zero shard contains page records. A deeper shard contains ordered
descriptors `[min_key, max_key, byte_offset, compressed_length, child_depth]`;
`child_depth` is omitted when the children are leaf shards.
Only the root uses delta-coded descriptors. Branch levels are added only when
needed to keep the bootstrap bounded. Both branches and leaves are gzip JSON.

## Page records and lookup

A page record is:

```text
[min_key, max_key, byte_offset, compressed_length,
 first_global_row, row_count, fences, valid_key_rows]
```

`fences` is a flat array of `[cell, relative_first_row]` pairs, starting at
row zero, at the same finest-LOD grid. Tile-aligned pages normally have one
pair. These fences enforce exact tile membership without fetching the key
column. `valid_key_rows` excludes trailing null-key source rows; those rows
remain in the analytical table and have null LODs.

To read a tile: look up the pages whose key bounds intersect the tile's key
interval, plus the first page whose maximum key reaches the interval start, or
the final page if none does. That page contains the tile's copy host when any
copies exist. Read consecutive pages together. Decode the standard Parquet
page with the root's codec and the required-struct/nullable-binary schema.
Use the fences to retain own rows inside the tile interval, and the host row
for its copies tagged with the requested tile. Global row indices are the
feature ids. A reader can scan definition-level runs in v1 PLAIN pages
directly and skip null rows, falling back to a general decoder for other
encodings. The directory stores no geometry or styling payload and is not an
MVT archive.

## Analytical access and publishing

Exact source-row reads and tiles beyond the finest LOD open the normal Parquet
footer lazily; the standard page indexes describe every page, so ordinary
Parquet readers see an ordinary file. A row group's offset indexes are
adjacent, so a row read across columns fetches them in one request. Publish files immutably: the profile is
specific to one file write, and ordinary Parquet writers need not preserve it.
The reference `write()` builds the layout as its final step, writes to
temporary files, and publishes only after the completed footer is readable.
Its layout pass rejects encrypted or external chunks, bloom filters, legacy
index pages, and, when keeping LOD pages verbatim, dictionary-encoded LOD
chunks; the default tile-aligned build re-encodes them.

The reference reader's caching, worker pool, and cancellation behaviour are
described in the [usage guide](../docs/usage.md#reader-behaviour); none of it
changes the stored bytes.
