# Shared-cache workers and compression page sizes

This follows the [single-order and CPU experiment](parity-followup.md). All
variants here use the same 53,742,978-row single-order writer output, including
the same packed values and attribute dictionaries. Only compression page size
and reader worker count change.

The selected defaults are **64 KiB pages and two CPU workers**, with serial
rendering on devices reporting at most two logical cores. Four workers remain
an option for dense views. Tokyo city's cold viewport improves from **256 to
209 ms** with the defaults, or **195 ms** with four workers. Its cached repeat
falls from **125 to 69 ms**, or **41 ms** with four. This is progress, not PMTiles
parity: the previous PMTiles viewport reference was about 116 ms.

## Shared I/O, parallel CPU

A coordinator owns one reader per URL, the spatial directories, coalescing,
pending fetches, and the 16 MiB compressed-page cache per reader. After selection
and fetching, a CPU worker decompresses pages, decodes packed geometry and
attributes, and builds MVT. It receives compressed copies only at dispatch;
queued jobs retain references. Cached originals are never detached. There is
one active job per CPU worker. Cancellation removes queued jobs or discards an
active result without cancelling shared fetches or other tiles. Active synchronous
CPU work finishes before that slot can serve another job.

Workers reuse the coordinator's `worker.js` URL. There is no additional JS asset;
browser cache headers still govern whether repeated worker loads need HTTP
validation. The bundled worker grows from 117,692 to about 119,200 gzip bytes.
Each CPU worker still parses/initializes its own copy and owns its own projection
cache. Worker failures reject active and queued promises instead of hanging.

The direct `renderTile(reader, ...)` API stays serial. MapLibre uses the pool only
for indexed LOD tiles. Ordinary files, exact-primary fallback and analytical row
reads remain on the coordinator.

## Viewports, milliseconds

Medians of five runs, rotating variant order, with 50 ms delay per data range.
These execute the actual coordinator and CPU worker modules through Node worker
threads. Each run creates fresh workers/readers, opens the source, loads a cold
viewport, pans east one column, then renders the original viewport from cached
compressed pages. No artificial bandwidth cap or MapLibre layout/drawing is
included. Opening and JS/thread startup are separate from the tables. A cached
repeat is a second render, not a fully warmed steady-state benchmark.

### Cold viewport

| Viewport       | 16 KiB, serial | 64 KiB, serial | 64 KiB, 2 workers | 64 KiB, 4 workers |
| -------------- | -------------: | -------------: | ----------------: | ----------------: |
| Japan overview |            171 |            170 |               157 |               156 |
| Tokyo metro    |            218 |            215 |               205 |               196 |
| Tokyo city     |            256 |            258 |               209 |               195 |
| Chiba          |            151 |            150 |               140 |               137 |
| Osaka          |            142 |            145 |               136 |               136 |

### Newly exposed pan column

| Viewport       | 16 KiB, serial | 64 KiB, serial | 64 KiB, 2 workers | 64 KiB, 4 workers |
| -------------- | -------------: | -------------: | ----------------: | ----------------: |
| Japan overview |             57 |             57 |                67 |                64 |
| Tokyo metro    |             59 |             60 |                56 |                62 |
| Tokyo city     |             72 |             70 |                62 |                61 |
| Chiba          |             67 |             67 |                62 |                63 |
| Osaka          |             60 |             60 |                58 |                58 |

### Cached repeat of original viewport

| Viewport       | 16 KiB, serial | 64 KiB, serial | 64 KiB, 2 workers | 64 KiB, 4 workers |
| -------------- | -------------: | -------------: | ----------------: | ----------------: |
| Japan overview |             47 |             47 |                39 |                43 |
| Tokyo metro    |             91 |             90 |                70 |                83 |
| Tokyo city     |            125 |            126 |                69 |                41 |
| Chiba          |             26 |             25 |                16 |                20 |
| Osaka          |             21 |             21 |                14 |                12 |

Four workers help the dense city most; they do not consistently help smaller
views, and can be slower on the metro cached repeat. Even two workers regress
the small Japan pan in this run. This is why four is opt-in, and serial mode is
still available. CPU parallelism does not accelerate an individual tile.

Changing only worker count produced **identical data requests and bytes in every
sample**. Each viewport/pan's MVT SHA-256 matched across all page sizes and worker
counts. Cached repeats fetched zero bytes. The assertions are part of the benchmark.

## Compression pages, decimal KB per cold viewport

The 16/32/64/128 KiB settings are uncompressed targets. All variants retain the
same tile boundaries, row groups, 2,048-present-value target and 262,144-source-row
cap. Therefore a larger byte target often cannot grow a page, and is not expected
to improve every view.

| Viewport       |  16 KiB |  32 KiB |  64 KiB | 128 KiB |
| -------------- | ------: | ------: | ------: | ------: |
| Japan overview |   559.0 |   559.0 |   559.0 |   559.0 |
| Tokyo metro    | 1,126.7 | 1,083.3 | 1,066.7 | 1,066.1 |
| Tokyo city     | 2,208.5 | 2,134.9 | 2,113.4 | 2,107.5 |
| Chiba          |   389.1 |   386.1 |   381.1 |   382.2 |
| Osaka          |   394.6 |   366.8 |   365.0 |   363.2 |

64 KiB saves about 4.3% for Tokyo city, 5.3% for Tokyo metro and 7.5% for Osaka.
128 KiB has little extra benefit, and slightly increases Chiba's reads. On this
worker harness, 64 KiB also reduces city data requests from five to four and
Osaka from four to three. Other viewport counts are unchanged. Opening remains
one 16 KiB data request. These are data requests, excluding JavaScript assets.

The separate single-tile range benchmark exposes the tradeoff:

| Tile          | 16 KiB pages, KB | 64 KiB pages, KB | PMTiles, KB |
| ------------- | ---------------: | ---------------: | ----------: |
| Tokyo metro   |            778.9 |            732.0 |       499.4 |
| Tokyo city    |            369.2 |            359.1 |       306.2 |
| Shibuya       |            368.7 |            362.7 |       349.8 |
| Chiba         |             81.7 |             81.4 |        60.3 |
| Tokyo station |             35.1 |             38.2 |        32.4 |
| Osaka         |             63.2 |             66.3 |       111.8 |

All six single-tile cases still use two cold requests and one neighbour request.
Larger pages increase the two z15 single-tile reads by roughly 3 KB: selection
also reads boundary-copy host pages, and page boundaries change which neighbouring
values accompany that host. Tile alignment alone does not eliminate this
cost. PMTiles stops at z14 here, so its z15 comparison reads larger parent tiles;
its coarse feature counts also differ. These are not equal-content encoding tests.
The page-size benchmark uses the serial reader to isolate the physical layout.

| Page target |    File bytes | Directory bytes | Gzipped root bytes |
| ----------- | ------------: | --------------: | -----------------: |
| 16 KiB      | 7,727,188,331 |      26,413,120 |              9,028 |
| 32 KiB      | 7,700,550,286 |      24,427,875 |              8,553 |
| 64 KiB      | 7,688,092,256 |      23,648,141 |              8,319 |
| 128 KiB     | 7,687,161,213 |      23,584,363 |              8,326 |

## Startup and memory limits

The Node coordinator starts in roughly 26–31 ms, then source opening and pool
initialization take another 63–66 ms with the simulated 50 ms data latency. The
CPU workers initialize alongside the bootstrap read, hiding most of their startup
in this particular test. This does not establish startup parity on slow devices.

The raw results include sampled process-RSS growth. Typical medians in this run
are about 5–10 MB for serial, 11–17 MB for two workers, and 18–27 MB for four.
These are approximate process deltas, affected by the allocator and previous
runs; they are not isolated heap measurements or mobile memory guarantees.
No mobile test was performed. `setWorkerCount(1)` keeps the serial option.

A real Chromium browser smoke test exercised nested workers, metadata, nine
Tokyo tiles, repeated tile hashes, and exact-row reads. All 1/2/4-worker outputs
matched and repeated reads made no additional data requests. The final shared-
bundle two-worker path was checked again. Localhost timing from that smoke test
is not used in the performance tables.

## Validation

- All 53,742,978 rows and every column in the 64 KiB output compare exactly with
  the writer output, including NaN-aware bbox equality and GeoParquet metadata.
- Python/JS integration covers points, lines, polygons, boundary copies, null
  geometry, prefix/no-prefix layouts, all four page sizes, and standard V2 pages.
- The 1/2/4-worker integration checks exact MVT bytes, feature IDs, properties,
  concurrent requests, cancellation, shared-cache reuse, and exact-primary fallback.
- Pool transport tests cover queued/active cancellation, buffer ownership,
  construction/load errors, and rejection of queued work on worker failure.
- Twenty Python tests, JS tests, TypeScript checks, and the demo build pass.

## Reproduce

```sh
# Run in the repository, with the existing single-order packed writer output.
for page_bytes in 16384 32768 65536 131072; do
  geoparquet-lod optimize /tmp/japan.single.parquet "/tmp/japan-${page_bytes}.parquet" \
    --page-bytes "$page_bytes"
done
npm run build --prefix js
RUNS=5 node js/test/viewport-workers.mjs /tmp/japan-65536.parquet 50 1,2,4
```

For a paired page/worker matrix, pass a JSON file instead of a Parquet path:

```json
[
  { "variant": "16 KiB / 1", "path": "/tmp/japan-16384.parquet", "workers": 1 },
  { "variant": "64 KiB / 1", "path": "/tmp/japan-65536.parquet", "workers": 1 },
  { "variant": "64 KiB / 2", "path": "/tmp/japan-65536.parquet", "workers": 2 },
  { "variant": "64 KiB / 4", "path": "/tmp/japan-65536.parquet", "workers": 4 }
]
```

```sh
RUNS=5 node js/test/viewport-workers.mjs /tmp/matrix.json 50
LAYOUTS=1 RUNS=3 node js/test/parity.mjs \
  /tmp/japan-16384.parquet /tmp/japan-65536.parquet /tmp/japan.pmtiles 50
python benchmarks/check-table.py /tmp/japan.single.parquet /tmp/japan-65536.parquet
```

The old range benchmark's `prefix`/`single` labels mean first/second input here,
so they correspond to 16/64 KiB in the saved page benchmark, not different row
orders. Raw results: [worker/page matrix](parity-workers-benchmark.json) and
[single-tile page comparison](parity-pages-benchmark.json).

```js
import { setWorkerCount, prefetch } from 'geoparquet-lod/maplibre'
setWorkerCount(4) // optional; call before opening a source or registering the protocol
prefetch(url)
```
