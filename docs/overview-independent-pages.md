# Independent overview pages: browser follow-up

**Independent page boundaries and contiguous column streams remove most of the prototype's request penalty.** Nested geometry remains slower and larger than the equally improved packed control, but now approaches its cold/coarse view latency in this fixture. This supports further testing; it does not establish Japan-scale or PMTiles parity.

[Interactive experiment](http://localhost:8104/?layout=grouped&width=765). The previous prototype and production Japan datasets remain intact.

## Construction

Both files retain the same 91,067 Tokyo source rows, original WKB and properties, and whole quantized overview geometries as the [previous browser experiment](overview-browser-prototype.md). Each is a valid standard Parquet file with one analytical row group. Geometry columns have independent spatial page spans: 45/47/56/236 pages at z8/11/14/16, instead of forcing every level into 236 finest-level spans. Source height/class use 23 independently compressed pages of up to 4,096 rows and remain stored once.

Each physical leaf is contiguous, as Parquet requires: all X pages, then all Y pages, with explicit locations in the embedded compact directory. Source attribute columns are adjacent streams. The reader coalesces nearby requested pages using the same bounded gap policy for both variants (8 KiB, 25% overhead, 4 MiB maximum range), without incidental prefetch. For nested styling it filters decoded geometry, then fetches attribute pages containing visible row IDs. This adds a dependent fetch but avoids requesting attributes for every candidate geometry page. Original WKB/ID ranges remain excluded.

Both encodings get the layout improvements. Packed still carries attributes in its overview blobs; nested joins ordinary source columns. Nested coordinates remain normal lists/structs with standard integer delta encoding, but **the quantization transform and spatial directory still require experimental metadata**. They are not automatically recognized as standard GeoArrow geometry by spatial tools.

## Matched browser results

Fresh runs of both the prior layout and new layout: Chrome 154, 765×540 map, 50 ms server delay per range, no bandwidth cap, three alternating runs per layout. Same nine settled navigation/style phases, cache limits, zero style transitions, and MVT renderer. Each layout completed 54 samples with zero map errors. Every visible feature count matched across variants and layouts. Values below are medians; timings measure MapLibre idle plus two animation frames, not GPU instrumentation.

| Measurement             | Previous nested | New nested | New packed |
| ----------------------- | --------------: | ---------: | ---------: |
| Cold city z14           |          882 ms |     249 ms |     224 ms |
| Geometry zoom z16       |          197 ms |     148 ms |      90 ms |
| Add height/class z16    |          200 ms |      99 ms |      41 ms |
| Styled city z14 revisit |          607 ms |      99 ms |      41 ms |
| Styled peripheral pan   |        1,589 ms |     206 ms |     114 ms |
| Styled z11              |        1,171 ms |      87 ms |      87 ms |
| Styled z8               |        1,182 ms |      91 ms |      82 ms |
| Route transfer          |       647,042 B |  567,206 B |  431,249 B |
| Route requests          |             619 |         26 |         10 |

Route totals sum phase medians. Previous packed used 456,391 B and 407 requests. New nested reduces its requests by 96% and bytes by 12%; it still uses 32% more bytes and 2.6× the requests of new packed. It retains the dense geometry byte benefit (75,920 versus 85,598 B at z16), but separate coordinate leaves, attribute joins, and slower nested decoding still cost time. New route decode CPU sums are 93 ms nested versus 21 ms packed. Both fully cached styled repeats take about 47 ms with zero transfer.

Complete files are 8,809,654 B nested and 8,787,677 B packed. Those totals include original source data; their near equality must not be confused with equal overview/network costs. Larger source column chunks also improve compression independently of the overview representation. New bootstrap/directory reads total 36,490 B nested and 33,110 B packed; full footers are not fetched by the browser.

## Validation and limits

Full PyArrow readback, including metadata, equals the prior files for every source value and overview coordinate. DuckDB row and source-null counts pass. All 16 sampled tiles have identical source IDs and byte-identical MVT output across encodings **and previous layouts**. Independent bbox scans include 390 boundary-crossing instances; 33,520 MVT property checks match source height/class. Captured browser ranges were audited to exclude original WKB/IDs.

This fixture has two small windows and loads its complete directory. It does not test a national dataset, sustained cancellation, bounded decoded heap, or HTTP bandwidth constraints. Coarse features can still require attributes from many source pages at national scale. The current production Japan renderer has a different access layout and has not been replaced or benchmarked by this experiment.

The next useful test is the independent-page design on a substantially larger dataset with a bounded directory, comparing styled routes and transfer against the existing production baseline. This result does not justify another custom coordinate representation merely to recover request efficiency.

## Reproduction and evidence

After the preceding experiment has created its fixtures:

```bash
.venv/bin/python benchmarks/overview-alternatives/build-browser-grouped.py
js/node_modules/.bin/esbuild js/experiments/overview-alternatives/worker.ts --bundle --format=esm --outfile=/private/tmp/overview-alternatives/worker.js
js/node_modules/.bin/esbuild js/experiments/overview-alternatives/reader.ts --bundle --format=esm --platform=node --outfile=/private/tmp/overview-alternatives/reader.mjs
node benchmarks/overview-alternatives/browser-check.mjs --grouped > /private/tmp/overview-alternatives/browser-check-grouped.json
.venv/bin/python benchmarks/overview-alternatives/validate-browser.py --input /private/tmp/overview-alternatives/browser-check-grouped.json --output benchmarks/overview-alternatives/browser-grouped-validation.json
node benchmarks/overview-alternatives/browser-serve.mjs 8104
```

Run the browser benchmark at `http://localhost:8104/?layout=grouped&width=765`, then `.venv/bin/python benchmarks/overview-alternatives/summarize-browser.py --grouped`. Run the previous-layout control at `http://localhost:8104/?width=765`, then summarize with `--output benchmarks/overview-alternatives/browser-results-grouped-control.json`. Inspect the recorded viewport; a narrower browser panel may constrain it. All generated Parquet files and bundles stay in scratch.

Evidence: [new layout](../benchmarks/overview-alternatives/browser-results-grouped.json), [fresh previous-layout control](../benchmarks/overview-alternatives/browser-results-grouped-control.json), [geometry/property validation](../benchmarks/overview-alternatives/browser-grouped-validation.json).
