# Experimental spatial page index, version 1

This is an optional physical profile of a packed LOD file. The logical
`geo.lod_columns` metadata and all table values remain unchanged. It is not
a standardized GeoParquet extension. Compatibility has been exercised with
PyArrow, DuckDB, and the existing hyparquet-based reader.

## File layout

```text
PAR1
LODIDX01 | root_length:u32le | gzip(root JSON) | padding to byte 16384
column chunks, physically grouped by column, then logical row group
standard Parquet offset/column indexes
gzip spatial directory leaves and optional branches
standard Parquet footer | footer_length:u32le | PAR1
```

The footer retains the original row groups, columns, schema, statistics, and
GeoParquet metadata, with relocated offsets and updated physical sizes.
`lod_spatial_index` in its key/value metadata advertises the bootstrap location.
Primary geometry and source attribute chunks are copied byte for byte. LOD
chunks are optionally re-encoded using the original row order, nullable binary
schema, and zstd, with page breaks at the LOD's finest tile grid. Multiple
pages may serve a dense tile; a row-group boundary can also split a tile.
The row cap scales with the chunk's LOD density to target 2,048 present values,
up to 262,144 analytical rows per page, alongside a configurable uncompressed
byte target (64 KiB by default; `--page-bytes 16384` restores the earlier target).
The target is advisory: write batches and individual large values may exceed it.
The bootstrap remains 16 KiB, independent of the compression page size.
`--fixed-pages` keeps the previous 2,048-row cap. Neither changes table values.
Runs of entirely null tiles are encoded together and omitted from the spatial
directory. Standard offset indexes still describe every page.

Every column chunk remains contiguous. Interleaving pages from different
chunks is not part of this profile. A tile may need both the coarse-prefix
section and the main section, so two data requests can remain necessary.
A file written with `prefix_zoom=None` has only the main stream and can serve
contiguous tile data in one range; row-group and directory boundaries can still
introduce extra reads. Rewriting the logical order changes row IDs, unlike
optimizing an existing file.

## Bootstrap

The compressed root is bounded by the first 16 KiB and contains:

- `version: 1`, `file_bytes`: format and full file length checks.
- `lod`, `primary`: copies of the logical LOD metadata and primary column name.
- `bounds`, `columns`: map extent and inspectable scalar source columns, so
  the demo can initialize through the tile worker without another reader open.
- `codecs`: LOD name to its Parquet compression codec.
- `directory_encoding: "delta"` and `streams`: directory series for each LOD,
  ordered `[prefix, main]`. With no prefix, the first series is empty.

Each root series is `[initial_byte_offset, depth, triples]`. Triples are a
flat array of `[delta_min_cell, max_cell_minus_min_cell, compressed_length]`.
The first minimum is relative to zero; subsequent minima are relative to the
previous one. Shards are contiguous, so their byte offsets follow from their
lengths. Cells are Hilbert cells at `min(key.depth, finest_LOD_zoom)`.
Directory bounds reconstructed from cells conservatively include whole cells.

A depth-zero shard contains page records. A deeper shard contains ordered
descriptors `[min_key, max_key, byte_offset, compressed_length, child_depth]`.
Only the root uses delta-coded descriptors. Branch levels are added only when
needed to keep the bootstrap bounded. Both branches and leaves are gzip JSON.

## Page records and lookup

A page record is:

```text
[min_key, max_key, byte_offset, compressed_length,
 first_global_row, row_count, fences, valid_key_rows]
```

`fences` is a flat array of `[cell, relative_first_row]` pairs, starting at row
zero, at the same finest-LOD grid. Tile-aligned pages normally have one pair.
Keep-pages mode can have several. These fences enforce exact tile membership
without fetching the key column. `valid_key_rows` excludes trailing null-key
source rows; those rows remain in the analytical table and have null LODs.

For each section, look up pages whose key bounds intersect the tile interval.
At a coarse-prefix LOD, skip ordinary main-section matches. Additionally,
retrieve the main section's first page whose maximum key reaches the interval
start, or its final page if none does. That page contains the tile's copy host
when any copies exist. If the main section is absent, hosts are in the prefix.

Read consecutive pages together. Decode the standard Parquet page with the
root's codec and the existing required-struct/nullable-binary schema. Use the
fences to retain own rows inside the tile interval, and the host row for its
copies tagged with the requested tile. Keep global row indices as feature ids.
The reader can scan definition-level runs in v1 PLAIN pages directly and
skip null rows, falling back to the general decoder for other encodings.
Selected geometries decode into typed coordinate buffers; MVT generation uses
reusable scratch buffers and retains the reference clipping behavior. The directory stores
no geometry or styling payload and is not an MVT archive.

## Caching and analytical access

The reader caches up to 16 MiB of compressed data pages and 128 directory
shards, including pending requests. A tile cancellation does not cancel a
request shared with another tile. Exact source-row reads and tiles beyond the
finest LOD lazily open the normal Parquet reader.

Publish files immutably. This physical profile is specific to one file write;
ordinary Parquet writers need not preserve it. Optimizing again reconstructs
it. The reference optimizer writes to a temporary file and publishes only
after the completed footer is readable. It requires a distinct output path
and rejects encrypted/external chunks, bloom filters, and legacy index pages.
Keep-pages mode additionally rejects dictionary-encoded LOD chunks.

## Parallel rendering

The MapLibre adapter keeps directory lookup, range coalescing, pending-fetch
sharing, and the compressed-page LRU in one coordinator. It sends selected
compressed page copies and their row/key fences to a bounded set of CPU workers.
Those workers decompress, decode packed values, and encode MVT without fetching
anything. Cached originals are never transferred/detached. Queued jobs hold
references; copies are made only when a worker is available. Cancellation drops
queued work or discards an active result, leaving shared fetches intact.

The default uses two CPU workers, or local rendering on devices reporting at
most two logical cores. The public `setWorkerCount` accepts 1–4 before the first
worker is created; one retains the serial path. Exact-primary fallback and
analytical reads remain on the coordinator. Each CPU worker has its own decoder
and bounded projection cache, so more workers consume more memory. They reuse the coordinator’s `worker.js` URL
to avoid an additional JavaScript asset. This is a reader change, with no file-format change.

## Optional separated-attribute ablation

`separate` converts a logical packed file to experimental
`geo.lod_columns.encoding = "packed_geometry"` with `pack_version: 1`.
Older packed-only readers must reject this encoding. It adds
`attribute_columns: {LOD_name: {attribute_name: physical_column_name}}`.
`attributes` continues to describe all carried display values in their original
order. Geometry blobs use the existing packing with **zero attribute fields**:
every own/copy attribute presence bitmap is zero, and there are no trailing
attribute strings. Geometry bytes and copy tile/source IDs are otherwise
unchanged. Original source columns and row order are preserved.

Every companion is a standard nullable Parquet three-level
`LIST<nullable original_scalar_type>`, with path `column.list.element`.
A null geometry row has a null list. A present row has exactly `1 + spill_count`
slots: own display value first, then copy values in blob order. An empty own
geometry still has slot zero. Null values, per-LOD overrides, and existing copy
values are preserved; looking up the source row alone cannot reproduce all
of those values. Dictionary-coded string attributes become ordinary string
lists. The lists are accessible to standard Parquet readers.

Optimization requires tile pages for this encoding. Each companion has exactly
one v1 PLAIN, ZSTD-compressed data page per geometry-page row span, without a
Parquet dictionary. Nullable LIST levels use the ordinary Parquet encoding.
Standard footer column chunks and offset indexes include every row. Pages for
null geometry spans remain in the table even when absent from the spatial
lookup directory. A single very large record may exceed the page byte target.

Separated directory page records append a ninth field:

```text
[..., valid_key_rows, [[attr0_offset, attr0_length], [attr1_offset, attr1_length], ...]]
```

The companion references follow metadata `attributes` order. Each inherits the
geometry page's `first_global_row` and `row_count`. Readers fetch only requested
companions, alongside geometry; CPU workers receive their compressed page
copies. Companion columns remain physically clustered by column, so fetching
more columns may require more ranges. The standard packed profile has no ninth
field and retains its original access path.

This ablation is not the default. See [measured tradeoffs](../docs/parity-separate.md):
modest geometry-only byte savings did not outweigh companion decode and request
costs for styled views. Arbitrary source-column projection and whole-feature
queries work with either profile. Source projection loads the ordinary footer
and indexes lazily; it does not require companions.
