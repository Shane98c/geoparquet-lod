# Sharing the complete detail levels

This experiment reuses one z16-precision display representation at z14, z15,
and z16. It compares against the [density-calibrated Japan file](tippecanoe-dropping.md),
not the older public R2 file. The analytical source contains 53,742,978 buildings.

## Construction

No format or reader change is needed. The writer already supports sharing a
single representation across complete zooms when its full-detail size estimate
is below `single_lod_bytes`. For this experiment the threshold is raised to
`2**60`, deliberately forcing sharing on a large dataset. All other settings
match the density experiment, with the existing z0–16 ladder made explicit.

The old `f14` payload serves z14–15 on a z15 grid, and `f16` serves z16 on a
z16 grid. The new `f14` payload serves all three zooms on the z16 grid. It must
be rebuilt: simply relabeling the old `f16` payload would omit boundary copies
for z14 and z15. The shared payload includes copies tagged for all served zooms;
each tile reader selects the copies for its requested zoom.

Expected tradeoffs are less duplicate display storage, more detailed geometry
and finer page boundaries at z14–15, and some extra boundary-copy data at z16.
Source rows and low-zoom selection are intended to remain unchanged. The
experiment does not alter the public or existing local comparison datasets.

## Results

The indexed file shrank from **7,666,520,593 to 6,849,218,809 bytes**: an
**817,301,784-byte (10.66%) saving**. The two old detail columns occupy
2,072,386,077 compressed bytes; the new shared column occupies 1,264,076,444.
It is about 159 MB larger than the old z16 column alone because it also hosts
boundary copies for z14 and z15. Smaller indexes account for the remaining
saving. Build time including indexing was about 19.5 minutes on this machine.

Five-run median cold 3×3 viewport results, after opening, with 50 ms per
range request and two CPU workers:

| View              | Before bytes | Shared bytes | Requests, each | Before ms | Shared ms |
| ----------------- | -----------: | -----------: | -------------: | --------: | --------: |
| Tokyo z11 control |    1,872,103 |    1,872,103 |              4 |     213.5 |     217.6 |
| Tokyo z14         |    1,153,113 |    1,534,348 |              5 |     178.6 |     178.9 |
| Osaka z14         |    1,536,387 |    1,984,742 |              4 |     181.6 |     193.1 |
| Rural Niigata z14 |       86,685 |      129,567 |              5 |     119.4 |     119.0 |
| Tokyo z15         |      210,247 |      269,652 |              4 |     129.8 |     132.2 |
| Osaka z15         |      365,018 |      459,478 |              3 |     138.9 |     139.6 |
| Rural Niigata z15 |       40,844 |       44,313 |              3 |     112.6 |     112.4 |
| Tokyo z16         |       43,345 |       50,466 |              4 |     114.3 |     113.7 |
| Osaka z16         |      108,869 |      135,223 |              5 |     122.0 |     121.2 |
| Rural Niigata z16 |       29,049 |       29,837 |              3 |     113.0 |     112.1 |

Both open with one 16 KiB range. Cold and pan request counts were unchanged in
these samples. City z14–15 cold payloads grow 26–33%; Niigata z14 grows 49% but
by only 43 KB. City z14–15 east pans grow 27–34% in bytes. Even cold z16 can
read more bytes because the shared column includes copies for other zooms.
With no bandwidth cap, similar timings do not imply equal transfer costs on a
slower connection. Cached revisits and adding carried height/class need no
requests in either construction; source-attribute projection still works.

### Zooming into a loaded view

A separate test keeps the same center and visits z14 → z15 → z16 → z15 → z14
with one reader. Both reuse z14 pages for z15; only the shared construction also
reuses them at z16. The return visits are cached in both. Opening is excluded.

| Location      | Whole-route bytes before → shared | z16 new requests before → shared | z16 ms before → shared |
| ------------- | --------------------------------: | -------------------------------: | ---------------------: |
| Tokyo         |             1,196,458 → 1,534,348 |                            4 → 0 |            108.9 → 1.2 |
| Osaka         |             1,645,256 → 1,984,742 |                            5 → 0 |            110.4 → 3.3 |
| Rural Niigata |                 115,734 → 129,567 |                            3 → 0 |            108.3 → 1.3 |

The zoom transition is faster, but total route bytes are still higher. Sharing
moves work into the first view; it does not make the extra geometry free.

### Integrity and recommendation

All 53,742,978 analytical rows, their positions, bbox values, and every z0–13
payload compare exactly equal to the density baseline. Every column of the raw
and indexed candidate compares equal. The validator passes. Tokyo z11 and all
sampled z16 outputs, including styled outputs, have matching MVT SHA-256 hashes
across constructions. At z14–15 the finer grid changes output; cold viewport
feature counts differ by at most eight in these samples, with no change to the
stored full-detail row membership.

Keep the current density file as the default for the goal of minimizing map
transfer. This is a useful storage-saving option when fewer stored bytes and
fast drill-down matter more than cold-detail bandwidth. It does not justify
replacing the public comparison automatically. More datasets and real bandwidth
limits would be needed before generalizing the result.

Measurements are in `benchmarks/shared-detail/`: column sizes, exact table
checks, viewport samples, and zoom-route samples. The candidate is available
locally at <http://127.0.0.1:8098/>; the preserved density comparison remains
at <http://127.0.0.1:8097/>. Neither replaces the R2 file.

## Reproduce

Set `SOURCE` to the cleaned Japan source, `BASELINE` to the indexed density
experiment, `RAW` to a new writer output, and `SHARED` to a new indexed output.
The writer uses 12 workers and a 40 GB DuckDB memory limit.

```sh
uv run python benchmarks/write-japan-tippecanoe.py "$SOURCE" "$RAW" "$SHARED" --share-detail
uv run python benchmarks/check-shared-detail.py "$BASELINE" "$SHARED"
uv run python benchmarks/check-table.py "$RAW" "$SHARED"
uv run python benchmarks/column-sizes.py "$BASELINE" "$SHARED"
npm --prefix js run build
RUNS=5 node js/test/shared-detail-benchmark.mjs "$BASELINE" "$SHARED" 50
RUNS=5 node js/test/shared-detail-zoom.mjs "$BASELINE" "$SHARED" 50
```

The worker benchmark alternates construction order between runs, uses the same
reader with two CPU workers, and delays each range request by 50 ms. It opens
a fresh reader for every sample, loads a 3×3 viewport, pans one column east,
revisits the viewport, adds carried height/class and then source version, and
repeats the styled view. Browser rendering, real CDN behavior, and bandwidth
limits are not modeled. File opening is reported separately from cold tiles.

Tokyo z11 is an unchanged control; Tokyo, Osaka, and rural Niigata are measured
at z14–16. SHA-256 checks require identical control and z16 MVT output across
constructions, deterministic output across runs, and identical cached repeats.
The z14–15 geometry can differ because its quantization is finer. Whole-table
checks compare analytical values, row positions, bbox values, and every z0–13
LOD payload, plus every column of the writer and indexed copies.
