# Hosted comparison

The public comparison is at <https://geoparquet-lod-compare.pages.dev/>.
It serves a GeoParquet LOD build of the Japan buildings, tagged `6c3a4f9`,
against the matching PMTiles archive. Its thinning budgets differ from the
[calibrated evaluation](japan-evaluation.md); the
[four-reader comparison](comparison.md) is measured on these hosted files.

The page defaults to Tokyo at zoom 11, geometry styling, and no basemap. Both
readers request data directly from the same R2 public host with no simulated
latency or bandwidth cap. The files use identical immutable cache settings.
The page supports the same view and style controls as the local comparison.

Use the reader checkboxes and **Apply readers** to choose any combination of
GeoParquet LOD, PMTiles, released COGP, and Esri SOP. Unselected panes are removed
and do not initialize their readers or request their datasets. The selection is
stored in the URL (`panes=lod,pmtiles,cogp,esri`); the default remains `lod,pmtiles`.
Applying a selection reloads the readers while preserving the current view and
style. One, two, or three maps fill the available width; four use a two-column
grid. Hidden panes leave no empty slots. At least one reader must be selected to apply.

All four archives derive from the same 53,742,978 Japan building footprints.
COGP uses the unmodified released `@cogp/maplibre` / `@cogp/reader` 2.1.4 and a
fresh conversion with the released COGP writer 2.2.2 (`cogp-rs-v2.2.2`, commit
`44f29be34d303fbb1febb154413a5aa7aa568a91`). The conversion uses
`--webmerc-minzoom 10` and otherwise the release's defaults.

Esri uses its original Japan SOP file and the reference MapLibre reader from
upstream commit `10ff683874db8b2d99c0f03b98354b5ea5f8199c`. Its ten implementation
files match the upstream Git blob hashes; only the dataset-list type is reduced
to the fields used by this demo. The reference reader returns geometry without
source attributes, so this pane stays in geometry styling. Its feature limit
and ready status are shown explicitly; initial timing waits for data rendering.

## Data provenance

Objects live under `geoparquet-lod/releases/6c3a4f9/` in the
`live-classify-data` bucket.

| Dataset    |         Bytes | MD5                                |
| ---------- | ------------: | ---------------------------------- |
| GeoParquet | 7,688,092,256 | `21416b9683faa7cfc4a8920123fce60f` |
| PMTiles    | 1,297,122,745 | `0b733b7511dda51102c1b58a4983706b` |
| COGP 2.2.2 | 9,081,139,765 | `f9a986b09224256534b530ed80dd245a` |
| Esri SOP   | 6,177,950,661 | `6224bdf08e1915e367baa61ee566f650` |

- [GeoParquet](https://pub-a9c45d804ff74bd19e8f9b7a52d9a90b.r2.dev/geoparquet-lod/releases/6c3a4f9/japan_buildings.parquet)
- [PMTiles](https://pub-a9c45d804ff74bd19e8f9b7a52d9a90b.r2.dev/geoparquet-lod/releases/6c3a4f9/japan_buildings.pmtiles)
- [Released COGP](https://pub-a9c45d804ff74bd19e8f9b7a52d9a90b.r2.dev/geoparquet-lod/releases/6c3a4f9/japan_buildings.cogp-2.2.2.parquet)
- [Esri SOP](https://pub-a9c45d804ff74bd19e8f9b7a52d9a90b.r2.dev/geoparquet-lod/releases/6c3a4f9/japan_buildings.sop.parquet)
- [Deployment manifest](https://geoparquet-lod-compare.pages.dev/release.json)

## Interpreting the comparison

The counters report response body bytes requested by each reader, including
responses served from the browser HTTP cache; they are not wire-transfer totals.
“Fresh load” clears reader caches by reloading the page, but the browser HTTP
cache can survive. Use a fresh browser session for a first-visit comparison.
“Reset counters” retains reader caches for continued navigation comparisons.

The archives still have different thinning and overzoom rules. Subtype is
stored in the PMTiles payload and fetched from original source columns by the
GeoParquet reader. Full archive sizes include different capabilities and should
not be confused with viewport transfer sizes.

## Rebuilding the hosted page

From the repository root, bundle the reader and build a separate static folder:

```sh
npm --prefix js run demo:bundle
node demo/build-share.mjs \
  --out /private/tmp/geoparquet-lod-share \
  --url https://pub-a9c45d804ff74bd19e8f9b7a52d9a90b.r2.dev/geoparquet-lod/releases/6c3a4f9/japan_buildings.parquet \
  --pmtiles https://pub-a9c45d804ff74bd19e8f9b7a52d9a90b.r2.dev/geoparquet-lod/releases/6c3a4f9/japan_buildings.pmtiles \
  --cogp https://pub-a9c45d804ff74bd19e8f9b7a52d9a90b.r2.dev/geoparquet-lod/releases/6c3a4f9/japan_buildings.cogp-2.2.2.parquet \
  --esri https://pub-a9c45d804ff74bd19e8f9b7a52d9a90b.r2.dev/geoparquet-lod/releases/6c3a4f9/japan_buildings.sop.parquet \
  --release 6c3a4f9
wrangler pages deploy /private/tmp/geoparquet-lod-share \
  --project-name geoparquet-lod-compare --branch main
```

The builder supplies public defaults only when query parameters are absent, so
links can override the initial view, style, or datasets. It leaves the local demo
configuration unchanged. Upload new datasets under a new release prefix instead
of replacing immutable objects.
