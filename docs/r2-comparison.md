# Shared R2 comparison

The public comparison is at <https://geoparquet-lod-compare.pages.dev/>.
It serves the preserved `6c3a4f9` GeoParquet layout against the matching Japan
PMTiles baseline. The later COGP checkout experiment is separate from this demo.

The page defaults to Tokyo at zoom 11, geometry styling, and no basemap. Both
readers request data directly from the same R2 public host with no simulated
latency or bandwidth cap. The files use identical immutable cache settings.
The page supports the same view and style controls as the local comparison.

## Data provenance

Objects are retained under `geoparquet-lod/releases/6c3a4f9/` in the existing
`live-classify-data` bucket; the previous unversioned objects are preserved.

| Dataset    |         Bytes | MD5                                |
| ---------- | ------------: | ---------------------------------- |
| GeoParquet | 7,688,092,256 | `21416b9683faa7cfc4a8920123fce60f` |
| PMTiles    | 1,297,122,745 | `0b733b7511dda51102c1b58a4983706b` |

- [GeoParquet](https://pub-a9c45d804ff74bd19e8f9b7a52d9a90b.r2.dev/geoparquet-lod/releases/6c3a4f9/japan_buildings.parquet)
- [PMTiles](https://pub-a9c45d804ff74bd19e8f9b7a52d9a90b.r2.dev/geoparquet-lod/releases/6c3a4f9/japan_buildings.pmtiles)
- [Deployment manifest](https://geoparquet-lod-compare.pages.dev/release.json)

## Interpreting the comparison

The counters report response body bytes requested by each reader, including
responses served from the browser HTTP cache; they are not wire-transfer totals.
“Fresh load” clears reader caches by reloading the page, but the browser HTTP
cache can survive. Use a fresh browser session for a first-visit comparison.
“Reset counters” retains reader caches for continued navigation comparisons.

The two archives still have different thinning and overzoom rules. Subtype is
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
  --release 6c3a4f9
wrangler pages deploy /private/tmp/geoparquet-lod-share \
  --project-name geoparquet-lod-compare --branch main
```

The builder supplies public defaults only when query parameters are absent, so
links can override the initial view, style, or datasets. It leaves the local demo
configuration unchanged. Upload new datasets under a new release prefix instead
of replacing immutable objects.
