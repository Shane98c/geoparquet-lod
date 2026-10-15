# GeoParquet LOD

**One GeoParquet file for a zoomable map and the original analytical data.**

This experimental layout keeps original feature geometries and source attributes
alongside compact, thinned overviews. A spatial page index lets a browser
fetch the relevant Parquet pages without opening the full footer. Ordinary
GeoParquet tools query the original columns; the map uses a custom reader.

**Draft 0.1.0: a research prototype, not an adopted standard or a production
release.** The format can still change. Python and JavaScript packages are not
published to registries yet.

[Live comparison](https://geoparquet-lod-compare.pages.dev/) ·
[Format specification](spec/SPEC.md) ·
[Spatial index](spec/SPATIAL-INDEX.md) ·
[Writer and reader guide](docs/usage.md) ·
[Comparison with other formats](docs/comparison.md) ·
[Contributing](CONTRIBUTING.md)

## Why this exists

A map archive makes display fast; an analytical table preserves the data needed
for queries and new styles. This project explores whether one file can do both
with competitive map access. The original geometry appears once in the source
column. Display geometry, selected attributes, and tile-boundary copies add
storage, but there is no separate archive of pre-encoded MVT tiles.

The intended use is a static dataset that people need to both explore and query.
If map delivery is the only requirement, PMTiles is the simpler choice. Several
other GeoParquet overview designs exist: COGP, Esri's Spatially Optimized
Parquet, tylertoo's overviews, and the GeoParquet LOD extension draft. The
[specification](spec/SPEC.md#relation-to-other-proposals) describes how this
layout differs from each; [prior work and attribution](THIRD_PARTY.md) lists
what it builds on.

## Try it

The [hosted demo](https://geoparquet-lod-compare.pages.dev/) shows GeoParquet
LOD and PMTiles built from the same Japan building source. COGP and Esri panes
are optional: select their checkboxes and click **Apply readers**. Start over
Tokyo, zoom out to see the overviews, then zoom into a neighbourhood. Pan the
maps together and compare the request and byte counters. Switch from geometry
styling to an attribute style to exercise more than geometry delivery. The
browser reads ranges from the hosted files; nothing needs downloading. See the
[hosted comparison notes](docs/hosted-comparison.md) for what the counters
measure and where the files come from.

## Build your own file

Input is GeoParquet with WKB geometry in longitude/latitude (OGC:CRS84 or
EPSG:4326 in x/y order), one geometry family per file. Thinning budgets should
be tuned to your data.

With Python 3.12+ and uv installed, run `uv sync --locked` from a clone.
Save the following as `build.py` and run `uv run python build.py`. Supply your
own `buildings.parquet`; `height` and `class` here are example source columns
to carry into the fast map path. The first build may download DuckDB's spatial
extension.

```python
from geoparquet_lod import chain, drop_smallest, tiny_polygons, write

if __name__ == "__main__":
    write(
        "buildings.parquet", "buildings.lod.parquet",
        thinning=chain(drop_smallest(850_000), tiny_polygons()),
        carry=["height", "class"],
    )
```

The output still contains the original geometries and all source attributes,
including columns you did not choose for `carry`. For example, query the same
file with DuckDB from `uv run python`:

```python
import duckdb

duckdb.sql("""
    SELECT class, count(*) AS buildings, avg(height) AS mean_height
    FROM read_parquet('buildings.lod.parquet')
    GROUP BY class
""").show()
```

This query counts source features, not the thinned overviews.

To view your output locally, install Node 22+ and run:

```sh
npm --prefix js ci
npm --prefix js run demo
```

The demo opens `buildings.lod.parquet` from the repository root. Click a
building to inspect its source row. It loads MapLibre from a CDN. A tiny
synthetic fixture for checking installation is available in the
[contributor guide](CONTRIBUTING.md#development).

Host the final file at an immutable URL with HTTP Range support and CORS
exposing `Content-Range`. `write()` builds the overviews, tile-aligned pages,
and the spatial directory in one call and replaces the output only after the
build succeeds; allow temporary disk space beside the output for its
intermediate file. The CLI does the same with
`geoparquet-lod write source.parquet output.parquet`. A generic Parquet rewrite
loses the spatial access path; rebuild from the source GeoParquet instead.

In a MapLibre app, after creating the map and loading its style:

```js
import { registerProtocol, tileUrl } from 'geoparquet-lod/maplibre'

registerProtocol(maplibregl)
map.addSource('buildings', {
  type: 'vector',
  tiles: [tileUrl({ url, layer: 'buildings', properties: ['height'] })],
})
map.addLayer({
  id: 'buildings',
  type: 'fill',
  source: 'buildings',
  'source-layer': 'buildings',
})
```

Install the JS package from the built local `js/` directory. Its worker asset
must remain available to your bundler. The reader also exposes whole-feature
geometry batches with typed coordinate buffers, plus GeoJSON, GeoArrow IPC, and
MVT adapters; the [deck.gl example](https://geoparquet-lod-compare.pages.dev/deck/)
renders the hosted Japan file outside MapLibre. See the [guide](docs/usage.md)
for source attributes, nested fields,
custom thinning, validation, and worker configuration.

## What we have measured

Four readers against the hosted Japan archives, all derived from the same
53,742,978 Overture building footprints: this layout, released COGP 2.2.2,
PMTiles written by tippecanoe, and Esri's Spatially Optimized Parquet with its
reference reader. Each cell is a cold read on a fresh reader after one open,
geometry only, served from the same public bucket. Bytes are decimal.

| Tile                  | GeoParquet LOD               | COGP 2.2.2                  | PMTiles                      | Esri SOP                    |
| --------------------- | ---------------------------- | --------------------------- | ---------------------------- | --------------------------- |
| open                  | 16 KB, 1 req                 | 1.50 MB, 3 req              | 16 KB, 1 req                 | 2.62 MB, 3 req              |
| z8 metro, Tokyo       | 0.62 MB, 2 req, 93,775 feat. | 0.12 MB, 8 req, 1,152 feat. | 0.50 MB, 2 req, 95,774 feat. | 26.65 MB, 1,048 req, 28,000 |
| z11 city, Tokyo       | 0.32 MB, 2 req, 24,068       | 0.25 MB, 17 req, 860        | 0.31 MB, 2 req, 24,373       | 5.39 MB, 143 req, 177,679   |
| z13 downtown, Shibuya | 0.36 MB, 2 req, 29,834       | 0.68 MB, 42 req, 2,739      | 0.35 MB, 2 req, 28,908       | 2.61 MB, 92 req, 51,996     |
| z13 suburbs, Chiba    | 0.08 MB, 2 req, 4,955        | 0.44 MB, 34 req, 454        | 0.06 MB, 2 req, 4,929        | 1.30 MB, 55 req, 6,841      |
| z15 street, Tokyo     | 0.04 MB, 2 req, 229          | 0.86 MB, 82 req, 213        | 0.03 MB, 2 req, 1,441        | 1.30 MB, 62 req, 229        |
| z15 street, Osaka     | 0.07 MB, 2 req, 1,357        | 0.80 MB, 80 req, 1,287      | 0.11 MB, 2 req, 6,523        | 1.01 MB, 57 req, 1,362      |

The four files do not agree on what a tile should contain, so bytes only mean
something next to the feature counts. COGP's conversion keeps very few features
at coarse levels; PMTiles stops at z14 and serves the overzoomed parent at z15;
Esri's reader returns every feature in the extent, simplified. This layout's
thinning budgets were calibrated against PMTiles feature counts, as the
[Japan evaluation](docs/japan-evaluation.md) describes, so the two agree within
a few percent where both serve native tiles. [The comparison page](docs/comparison.md)
has definitions, provenance, and reproduction steps.

This supports competitive request counts and nearby transfer sizes on this
workload, **not a claim that this is universally faster than PMTiles, COGP, or
Esri**. We have not established broad device, dataset, or network parity.

## Main tradeoffs

- **Storage and construction.** The evaluated Japan file is 7.67 GB, versus the
  4.84 GB analytical source and 1.30 GB PMTiles archive. Those files provide
  different capabilities. Building overviews and indexing is an offline step;
  updates require rebuilding.
- **A custom display encoding and index.** Ordinary tools read original
  geometries and attributes, but cannot interpret the overview payloads. There
  is one implementation, a draft spec, and no independent conformance suite.
  Compatibility has been exercised with PyArrow and DuckDB, not every Parquet
  reader or rewriter. [Design alternatives](docs/design-alternatives.md)
  records what standard encodings cost in the same role.
- **Fast styles require choices.** Carried attributes are cheap to render but
  repeated across LODs. Other source columns, including nested fields, can be
  fetched on demand; their first read can be substantially more expensive.
- **Overviews are selective.** Thinning and quantization serve display; they
  are not an exact analytical summary. Whole-feature overview queries preserve
  thinning and use bbox overlap, not exact intersection. Use original geometry
  for exact work. The Japan density tuning is not yet a general policy.
- **Browser work and memory.** Pages must be decoded and converted to MVT, with
  caches and a worker pool. Low-memory devices and prolonged sessions need more
  testing.
- **Coverage and maturity.** Performance work focuses on buildings. Points and
  lines have correctness fixtures, but large overlapping polygons, antimeridian
  behaviour, unthinnable data, and broader CRS needs require further evaluation.
  Full validation checks overview payloads and index consistency, but does not
  prove polygon topology, source-data equality, or finest-LOD completeness.

## Help evaluate the idea

Useful contributions are independent reader implementations, reproducible
comparisons on other datasets and devices, adversarial geometry fixtures, and
ways to simplify the layout or integrate its access ideas into the other
overview proposals. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, tests,
and what to include in performance reports. The [logical format](spec/SPEC.md)
and [physical index profile](spec/SPATIAL-INDEX.md) are separate design
surfaces for discussion.

## License

Project code is MIT. The vendored Esri comparison reader is Apache-2.0 and
retains its license. External datasets have their own terms; see
[THIRD_PARTY.md](THIRD_PARTY.md).
