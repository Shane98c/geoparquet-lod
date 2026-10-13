# Attribute separation and whole-feature access

The useful result is **keep the packed rendering path, and restore flexibility
through an on-demand source projection and whole-feature query API**. Splitting
all carried attributes into ordinary LIST columns did not improve styled map
performance. It remains an opt-in ablation, not the writer default.

This does not establish PMTiles parity. It tests whether a more column-oriented
construction can recover flexibility without losing the gains already measured
on the parity branch.

## What works now

- `renderTile(..., {properties: [...]})` can include source scalar columns that
  were not carried at write time. Existing carried names keep their LOD values,
  including thinning overrides. Other names join original source values by
  global row ID. No file rewrite is required to try a new style.
- `reader.readColumns(rowIds, columns)` batches source projection by row group
  and page. It preserves input order, repeated IDs, nulls, and source types.
  Requested scalar values use an estimated 32 MiB bounded LRU; unselected rows
  decoded during a sparse join are not retained in that cache. This is a cache
  accounting budget, not a bound on total JS heap or transient decode memory.
- `readFeatures(reader, {bbox, resolution, columns})` returns whole quantized
  overview geometries, their LOD transform, and deduplicated source row IDs.
  The bbox can wrap the antimeridian. This is bbox overlap, not an exact
  geometry-intersection predicate. Overview thinning and quantization still
  apply. Columns follow the display-value semantics of the tile reader; when
  copies overlap, the first encountered display record wins deduplication.
- `geometry: 'primary'` returns original WKB and original source values instead.
  It does not apply overview thinning. Projection uses physical leaf names;
  nested leaves retain hyparquet's nested representation, not assembled
  top-level struct objects. Map styling is intended for scalar columns.

The overview query currently enumerates intersecting tiles in bounded batches,
not a new arbitrary-bbox index traversal. It rejects more than 256 tiles by
default; callers can choose a coarser resolution, primary geometry, or an
explicit larger `maxTiles`. Resolution is longitude degrees per pixel.

Exact source WKB, all original source columns, row order, source IDs, per-LOD
selection, and whole boundary-crossing geometries are preserved. The experiment
does not clip the stored overviews or store MVT payloads. It does not add
progressive refinement, analytical queries against every overview through SQL,
or compatibility with COGP/Esri's custom overview readers. Primary GeoParquet
analysis remains ordinary GeoParquet.

## Controlled Tokyo ablation

4,882,169 Japan building rows intersecting `[139.39,35.45,140.10,35.90]`, all
original source columns, zooms 8–14, no prefix, 262,144-row analytical groups,
64 KiB geometry-page target. The two outputs share identical row order, geometry,
and display values. The split copy stores each carried attribute (`height`,
`class`) in an independently addressable nullable LIST column. List slots retain
own-row and boundary-copy values, including their existing differences.

Five runs, 50 ms per range request, two actual Node CPU workers, shared fetch
coordinator. Cold tile timing follows a separately measured file open; opening
reads one 16 KiB range for either format. No browser drawing, real network
bandwidth cap, or PMTiles comparison is included in this ablation. A metro view
is 2×2 tiles at z8; city and neighborhood views are 3×3 at z11 and z14 around
139.76°E, 35.68°N. The metro view covers only this subset, not all Japan.

Median cold viewport results; bytes are decimal KB:

| View         | Geometry-only packed → split | Geometry KB packed → split | Styled packed → split | Styled KB packed → split | Styled requests packed → split |
| ------------ | ---------------------------: | -------------------------: | --------------------: | -----------------------: | -----------------------------: |
| Metro        |                 184 → 180 ms |              614.4 → 535.9 |          197 → 576 ms |            614.4 → 936.6 |                          2 → 3 |
| City         |                 207 → 207 ms |          2,043.1 → 1,962.3 |          221 → 396 ms |        2,043.1 → 2,420.7 |                          3 → 6 |
| Neighborhood |                 172 → 170 ms |              874.1 → 865.7 |          180 → 192 ms |            874.1 → 897.9 |                          5 → 9 |

Geometry-only requests are unchanged: 2, 3, and 5. The split saves 12.8%, 4.0%,
and 1.0% of geometry-only transfer, with little latency gain. Fetching both
attributes loses those savings. Ordinary nullable LIST decoding also does
substantially more work on sparse overviews than the packed geometry decoder,
which skips long absent-row runs. This finding is about this concrete LIST
representation, not a proof that every possible separated encoding must lose.

After geometry is cached, adding `height` then `class` to the city view costs
85/82 ms and zero additional bytes with packed data. The split costs 219/293 ms,
199.0/249.7 KB and nine requests per step. Geometry pages are reused, but the
new attribute columns still have to be fetched and decoded.

Adding an uncarried `version` column now works for both formats:

| View         | First add, packed → split | Repeat, packed → split |       Repeat network |
| ------------ | ------------------------: | ---------------------: | -------------------: |
| Metro        |            712 → 1,087 ms |           116 → 490 ms | 0 requests / 0 bytes |
| City         |            787 → 1,063 ms |           228 → 484 ms | 0 requests / 0 bytes |
| Neighborhood |              344 → 343 ms |             84 → 93 ms | 0 requests / 0 bytes |

That first read must open the ordinary footer and read potentially sparse source
pages. It is **not yet a production-speed substitute for carrying a frequently
used style column**. Dynamic source-attribute rendering currently runs on the
coordinator; carried-attribute rendering uses the CPU worker pool. The requested
value cache eliminates repeat transfer within its budget, not repeated geometry
and MVT work. Source projection and MVT construction are the next optimization
target if arbitrary style changes become a primary workload.

Whole-file size is 568,617,362 bytes packed versus 577,517,716 split: **1.6% larger**.

## Validation

- Every original source column and bbox value compared equal across all
  4,882,169 rows after separation and optimization.
- DuckDB source aggregates matched, and ordinary SQL could project the new
  companion LIST columns.
- All columns, including new companion columns, compared equal between the
  split writer output and optimized copy across those same rows.
- Every benchmark output was SHA-256 compared across formats and runs for each
  scenario. Geometry-only, carried styles, new source styles, and cached repeats
  match byte for byte after fixing the float64 decoder described below.
- Browser smoke check passed for source-attribute styling, cached repeats,
  source-row inspection, and a whole-feature bbox query.
- Python/JS integration cases cover points, lines, polygons, nulls, a coarse
  prefix, explicit LOD overrides, whole large features crossing tile boundaries,
  source joins, wrapped bbox queries, footer fallback, and actual CPU workers.
- The comparison exposed and fixed two pre-existing reader bugs: packed float64
  bit patterns passed through a JS Number and lost low mantissa bits; the footer
  tile reader could return an out-of-tile host row as an owning feature.

The recommended construction remains a small packed set of frequently used
display attributes, independently accessible exact source columns, and stable
row IDs connecting the two. That retains the current map access path while
making new styles and whole-feature reads possible. It avoids making every
normal pan pay the cost of the most flexible read.

## Reproduce

```bash
npm --prefix js run build
.venv/bin/python benchmarks/build-separate.py /path/to/japan.parquet /tmp/lod-separate
node js/test/separate-benchmark.mjs /tmp/lod-separate/packed.parquet \
  /tmp/lod-separate/separate.parquet 50 > docs/parity-separate-benchmark.json
.venv/bin/python benchmarks/check-table.py /tmp/lod-separate/separate.raw.parquet \
  /tmp/lod-separate/separate.parquet
.venv/bin/pytest -q
npm --prefix js test
```

The builder needs the source's `bbox` covering. Raw samples and open/pan/cache
results are in [parity-separate-benchmark.json](parity-separate-benchmark.json).
