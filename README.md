# geoparquet-lod

**One GeoParquet file for a zoomable map and the original analytical data.**

This experimental layout keeps original feature geometries and source attributes
alongside compact, thinned display overviews. A spatial page index lets a browser
fetch the relevant Parquet pages without opening the full footer. Ordinary
GeoParquet tools can query the original columns; the map uses a custom reader.

**Draft 0.1.0: a research prototype, not an adopted standard or a production
release.** The format can still change. Python and JavaScript packages are not
published to registries yet.

[Live comparison](https://geoparquet-lod-compare.pages.dev/) ·
[Format specification](spec/SPEC.md) ·
[Spatial index](spec/SPATIAL-INDEX.md) ·
[Writer and reader guide](docs/usage.md) ·
[Contributing](CONTRIBUTING.md)

## Why this exists

A map archive makes display fast; an analytical table preserves the data needed
for queries and new styles. This project explores whether one file can do both
with competitive map access. The original geometry appears once in the source
column. Display geometry, selected attributes, and tile-boundary copies add
storage, but there is no separate archive of pre-encoded MVT tiles.

The intended use is a static dataset that people need to both explore and query.
If map delivery is the only requirement, PMTiles is the simpler choice. This
project builds on COGP, Esri's Spatially Optimized Parquet, and Tippecanoe;
see [prior work and attribution](THIRD_PARTY.md).

## Try it

The [hosted demo](https://geoparquet-lod-compare.pages.dev/) defaults to GeoParquet
LOD and PMTiles using the same Japan building source. COGP and Esri are optional:
select their checkboxes and click **Apply readers**. Disabled readers do not load.
Esri can make coarse views very expensive, so the four-pane view is best used
as a diagnostic comparison. All panes share the browser's resources.

The public GeoParquet file preserves the `6c3a4f9` dataset. The newer
[density experiment](docs/tippecanoe-dropping.md) is reproducible from this repo
but has not replaced that hosted file. [Dataset versions and demo measurement
semantics](docs/r2-comparison.md) explain exactly what is being compared.

For a small local example, install Python 3.12+, Node 22+, and uv, then from a clone:

```sh
uv sync --locked --group dev
npm --prefix js ci
npm --prefix js run build
uv run python examples/quickstart.py
uv run geoparquet-lod validate buildings.lod.parquet
npm --prefix js run demo
```

The example creates 5,000 synthetic building footprints, writes LODs, and adds
the spatial index. No external dataset is needed. The first writer run may
fetch DuckDB's spatial extension; the demo loads MapLibre from a CDN. Open
`http://localhost:8091/demo/?url=/buildings.lod.parquet` if it does not open
automatically. Click a building to inspect its source row.

## Build your own file

Input is GeoParquet with WKB geometry in longitude/latitude (OGC:CRS84 or
EPSG:4326 in x/y order), one geometry family per file. This example uses the
current indexed access path; thinning budgets should be tuned to your data.

```python
from geoparquet_lod import chain, drop_smallest, tiny_polygons, write
from geoparquet_lod.optimize import optimize

write(
    "buildings.parquet", "buildings.lod.parquet",
    thinning=chain(drop_smallest(850_000), tiny_polygons()),
    carry=["height", "class"],
    prefix_zoom=None,
)
optimize("buildings.lod.parquet", "buildings.indexed.parquet")
```

Put writer calls in an `if __name__ == "__main__":` guard when running scripts
with multiprocessing. Host the final file at an immutable URL with HTTP Range
support and CORS exposing `Content-Range`. The optimizer preserves table values
and row order while changing page layout and adding the directory. A generic
Parquet rewrite loses this access path; optimize the rewritten LOD file again.

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
must remain available to your bundler. See the [guide](docs/usage.md) for source
attributes, whole-feature reads, custom thinning, and worker configuration.

## What we have measured

The latest density experiment uses 53,742,978 Japan building rows. These are
cold, same-XYZ tile reads after opening, with geometry requested from our
reader. They exclude each reader's separate 16 KiB opening request.

| Tokyo tile | GeoParquet LOD bytes | PMTiles bytes | Requests, each |
| ---------- | -------------------: | ------------: | -------------: |
| z8         |              619,573 |       499,425 |              2 |
| z11        |              322,650 |       306,211 |              2 |
| z12        |              411,748 |       363,153 |              2 |

On held-out sample tiles, aggregate feature counts differ by -3.03% at z8,
-0.22% at z11, and +0.37% at z12. Individual z8 locations differ more. Counts
and visual coverage matter: different thinning, properties, tile buffers, and
overzoom rules can make smaller responses do less work.

This supports competitive request counts and nearby transfer sizes on this
workload, **not a claim that this is universally faster than PMTiles, COGP, or
Esri**. We have not established broad device, dataset, or network parity.
[Current results](docs/tippecanoe-dropping.md) include raw measurements and
integrity checks; [the experiment index](docs/README.md) separates them from
older layouts and modified-upstream experiments.

## Main tradeoffs

- **Storage and construction.** The latest Japan file is 7.67 GB, versus the
  4.84 GB cleaned analytical source and 1.30 GB PMTiles archive. Those files
  provide different capabilities. Building overviews and then indexing is an
  additional offline step; updates currently require rebuilding.
- **A custom display encoding and index.** Ordinary tools read original
  geometries and attributes, but cannot interpret these display payloads
  automatically. There is one implementation, a draft spec, and no independent
  conformance suite. Compatibility has been exercised with PyArrow and DuckDB,
  not every Parquet reader or rewriter.
- **Fast styles require choices.** Packed attributes are cheap to render but
  repeated across LODs. Other scalar source columns can be fetched on demand;
  their first read can be substantially more expensive. Nested source values
  remain analytical data, not arbitrary packed styling values.
- **Overviews are selective.** Thinning and quantization serve display; they
  are not an exact analytical summary. Whole-feature overview queries preserve
  thinning and use bbox overlap, not exact intersection. Use original geometry
  for exact work. The Japan density tuning is not yet a general policy.
- **Browser work and memory.** Pages must be decoded and converted to MVT, with
  caches and a worker pool. Cancellation helps navigation, but low-memory
  devices and prolonged sessions need more testing.
- **Coverage and maturity.** Performance work focuses on buildings. Points and
  lines have correctness fixtures, but large overlapping polygons, antimeridian
  behavior, unthinnable data, and broader CRS needs require further evaluation.
  Validation samples payloads; it does not prove every spatial-index entry,
  sort order, or finest-LOD membership correct.

## Help evaluate the idea

Useful contributions are independent reader implementations, reproducible
comparisons on other datasets and devices, adversarial geometry fixtures, and
ways to simplify the layout or integrate its access ideas into existing work.
See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, tests, and what to include in
performance reports. The [logical format](spec/SPEC.md) and [physical index
profile](spec/SPATIAL-INDEX.md) are separate design surfaces for discussion.

## License

Project code is MIT. The vendored Esri comparison reader is Apache-2.0 and
retains its license. External datasets have their own terms; see
[THIRD_PARTY.md](THIRD_PARTY.md).
