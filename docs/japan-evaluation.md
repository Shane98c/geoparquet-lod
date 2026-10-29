# Japan evaluation: map access and analytical data

This evaluation asks whether one GeoParquet file can preserve the original
analytical data while approaching PMTiles map-transfer costs. It uses
53,742,978 Japan building footprints, packed overviews, and a spatial
page index. Original geometries and attributes remain available to ordinary
GeoParquet tools.

## Construction

The overview policy applies `drop_smallest` followed by `tiny_polygons`, with
height and class carried for styling. It uses one Hilbert order, full detail
from z14 in one LOD serving z14 and z15, which a map overzooms past, 16 KiB
staging pages and 64 KiB final overview pages. The indexed file is
**6.32 GB**, compared with **4.84 GB** for the analytical source and
**1.30 GB** for PMTiles. These totals represent different capabilities.

Estimated-byte thinning budgets were calibrated against PMTiles feature counts:
1,080,000 at z8, 730,000 at z11, and 750,000 at z12. Other zooms use 1,300,000
through z8 and 850,000 above it. These are Japan-specific construction choices,
not format requirements or general library defaults. They tune area-based
selection rather than reproduce Tippecanoe's compressed-tile feedback algorithm.

## Display density

The profiler compares matching XYZ tiles, with no properties requested from our
reader. Counts include tile-boundary copies, which the two archives draw
differently: a GeoParquet tile carries neighbouring features within two pixels
of its edge, a PMTiles tile within tippecanoe's default buffer of ten.
Calibration uses eight locations across Japan. Validation uses separate tiles
around Hiroshima, Sendai, Kyoto, Naha, Narita and rural Niigata; coarse tiles
shared with calibration are excluded, leaving four validation tiles at z8.

| Sample      | Zoom | GeoParquet features | PMTiles features | Difference |
| ----------- | ---: | ------------------: | ---------------: | ---------: |
| Calibration |    8 |             242,477 |          249,738 |     -2.91% |
| Calibration |   11 |              85,050 |           89,253 |     -4.71% |
| Calibration |   12 |             133,715 |          138,574 |     -3.51% |
| Validation  |    8 |              37,889 |           39,060 |     -3.00% |
| Validation  |   11 |              43,254 |           44,244 |     -2.24% |
| Validation  |   12 |              65,245 |           67,332 |     -3.10% |

Counting only features wholly inside each tile, GeoParquet has 1.4% to 2.2%
more than PMTiles in every row. These aggregate counts do not guarantee
identical feature selection or visual coverage. At z8, Chiba and Sapporo have
about 12% fewer features than PMTiles in all, but 1.2% and 1.4% more inside the
tile. The raw profiles also record summed polygon area and interior polygon
area quantiles; summed area is not raster union coverage.

## Map transfer

Cold single-tile reads exclude each reader's 16,384-byte opening request.
Every row uses two tile-read requests. Measurements use one local run without
simulated latency; elapsed times are not a CDN performance benchmark.

| Tokyo tile | GeoParquet bytes | PMTiles bytes | Requests, each |
| ---------- | ---------------: | ------------: | -------------: |
| z8         |          623,663 |       499,425 |              2 |
| z11        |          274,054 |       306,211 |              2 |
| z12        |          328,860 |       363,153 |              2 |

Request counts match. At z11 and z12 GeoParquet transfers 10% and 9% fewer
bytes than PMTiles. Its tiles hold 4% and 3% fewer features, all of them
boundary copies; inside the tile it has 2% more. At z8 it transfers 25% more
bytes, with 1% more features inside the tile: there, page layout and geometry
encoding cost more than PMTiles' pre-cut tiles. These tile measurements do not
establish equal whole-session performance across datasets or devices.

The [hosted demo](hosted-comparison.md) serves this build; the
[four-reader comparison](comparison.md) measures it beside COGP, PMTiles and
Esri SOP over the public internet.

## Integrity and reproduction

Source versus writer output: all 53,742,978 rows pass count, XOR and sum checks
of whole-row hashes across all 23 source analytical columns, including IDs and
original geometry. This is a multiset checksum, not an exact equality proof.
Writer output versus indexed output: every column of every row compares equal,
including schema metadata and bbox values.

The build recipe is [`benchmarks/write-japan-tippecanoe.py`](../benchmarks/write-japan-tippecanoe.py).
It takes source, staging-output, and indexed-output paths. It uses private build
phases to check intermediate integrity; the public `write()` API performs the
complete build in one call. See the [usage guide](usage.md) for that workflow.

Recorded measurements and integrity checks are in
[`benchmarks/dropping/`](../benchmarks/dropping/). `demo-density*.json` profile
the uncalibrated baseline build the budgets were calibrated from against PMTiles
and `calibrated-density*.json` this build, each on the calibration and
held-out tile sets. In `ranges.json`, `prefix` labels the baseline build,
`single` this build's GeoParquet file and `pmtiles` the archive.

## What remains to improve

A spatially adaptive thinning policy could replace the Japan-specific budgets.
Further evaluation should cover additional datasets, coarse views, prolonged
navigation, and the remaining transfer overhead. Analytical rows are preserved
regardless of the display thinning policy.
