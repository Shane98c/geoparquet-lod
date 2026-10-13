# Matching PMTiles display density

This separate Japan experiment reduces overview density at z8, z11 and z12.
It keeps the preserved public dataset and library defaults unchanged. It is a
calibration of our existing area-based dropping, not an implementation of
Tippecanoe's compressed-tile feedback algorithm.

## Construction

`benchmarks/write-japan-tippecanoe.py` scales the existing estimated-byte budgets
by the ratio of PMTiles to GeoParquet feature counts in the calibration sample,
rounded to 10,000 bytes:

| Zoom | Previous budget | Experimental budget |
| ---- | --------------: | ------------------: |
| 8    |       1,300,000 |           1,080,000 |
| 11   |         850,000 |             730,000 |
| 12   |         850,000 |             750,000 |

Everything else retains the preserved configuration: `drop_smallest` followed
by `tiny_polygons`, carried height/class, no prefix, full detail from z14,
16 KiB writer pages and 64 KiB optimized pages. Analytical rows are not dropped.
The indexed file is 7,666,520,593 bytes versus 7,688,092,256 bytes before.

## Density results

The profiler compares the same XYZ tile from each format, with no properties
requested from our reader. Counts include tile-boundary copies. Calibration
uses eight locations across Japan. Validation uses separate XYZ tiles around
Hiroshima, Sendai, Kyoto, Naha, Narita and rural Niigata; coarse tiles shared with
calibration are excluded, leaving four validation tiles at z8.

| Sample      | Zoom | Previous features | New features | PMTiles features | New difference |
| ----------- | ---: | ----------------: | -----------: | ---------------: | -------------: |
| Calibration |    8 |           299,935 |      242,433 |          249,738 |         -2.93% |
| Calibration |   11 |           104,072 |       87,959 |           89,253 |         -1.45% |
| Calibration |   12 |           157,485 |      138,778 |          138,574 |         +0.15% |
| Validation  |    8 |            49,301 |       37,875 |           39,060 |         -3.03% |
| Validation  |   11 |            52,325 |       44,148 |           44,244 |         -0.22% |
| Validation  |   12 |            78,195 |       67,581 |           67,332 |         +0.37% |

These are aggregate counts, not guarantees for every location. At z8, Chiba
and Sapporo have about 12% fewer features than PMTiles. Matching counts also
does not establish identical feature selection. The profiler records summed
polygon area and interior polygon area quantiles as additional diagnostics;
summed area is not raster union coverage. All 72 sampled tiles outside the
three changed zooms have identical before/after MVT hashes.

## Range reads

These are cold single-tile reads, excluding each reader's 16,384-byte opening
request. Every row uses two tile-read requests. Measurements use one local run
without simulated latency; elapsed times are not a CDN performance benchmark.

| Tokyo tile | Previous bytes | New bytes | PMTiles bytes | Reduction |
| ---------- | -------------: | --------: | ------------: | --------: |
| z8         |        731,983 |   619,573 |       499,425 |     15.4% |
| z11        |        359,088 |   322,650 |       306,211 |     10.1% |
| z12        |        413,987 |   411,748 |       363,153 |      0.5% |

The z12 result is a useful limit: matching feature counts does not automatically
match transferred bytes with our page layout. The range report also includes
unchanged higher zooms. Its historical `prefix` label means the preserved
indexed file here, and `single` means the new indexed file. PMTiles z15 results
overzoom a native z14 tile, so those feature counts are not comparable directly.

## Integrity and reproduction

Source versus writer output: all 53,742,978 rows pass count, XOR and sum checks
of whole-row hashes across all 23 source analytical columns, including IDs and
original geometry. This is a multiset checksum, not an exact equality proof.
Writer output versus indexed output: every column of every row compares equal,
including schema metadata and bbox values.

From the repository root, with paths supplied for the original Japan source,
preserved indexed file and PMTiles baseline:

```sh
node js/test/dropping-profile.mjs "$OLD" "$PM" > benchmarks/dropping/before.json
.venv/bin/python benchmarks/write-japan-tippecanoe.py "$SOURCE" "$RAW" "$NEW"
node js/test/dropping-profile.mjs "$NEW" "$PM" > benchmarks/dropping/after.json
node js/test/dropping-profile.mjs "$OLD" "$PM" held-out > benchmarks/dropping/before-held-out.json
node js/test/dropping-profile.mjs "$NEW" "$PM" held-out > benchmarks/dropping/after-held-out.json
.venv/bin/python benchmarks/check-analysis.py "$SOURCE" "$RAW"
.venv/bin/python benchmarks/check-table.py "$RAW" "$NEW"
LAYOUTS=1 RUNS=1 node js/test/parity.mjs "$OLD" "$NEW" "$PM" 0 > benchmarks/dropping/ranges.json
node demo/serve.mjs --lod "$NEW" --pmtiles "$PM" --port 8097
```

Raw profiles and checksums are in `benchmarks/dropping/`. The local experimental
file is `/private/tmp/japan.tippecanoe-density.spatial.parquet`; it has not
replaced the R2 dataset. The local comparison is <http://127.0.0.1:8097/>.

The city-scale result supports tuning display thinning independently of
analytical storage. Before making this a general default, replace these
Japan-specific budgets with a spatially adaptive policy and test additional
datasets, especially coarse views and the remaining z12 byte overhead.
