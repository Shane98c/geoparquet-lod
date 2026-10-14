# geoparquet-lod JavaScript reader

Experimental draft 0.1.0. Reads the GeoParquet LOD layout and spatial page
index emitted by the writer; generates vector tiles for MapLibre. Not yet published to
npm. See the repository root README, `spec/`, and `docs/usage.md` for format
status, construction, limitations, and measurements.

Build from this directory with `npm ci && npm run build`. Install the built
package by local path. Use `geoparquet-lod` for direct reads and
`geoparquet-lod/maplibre` for the worker-backed protocol:

```js
import { openReader, coalescing, httpSource, renderTile } from 'geoparquet-lod'

const reader = await openReader(coalescing(await httpSource([url])))
const mvt = await renderTile(reader, z, x, y, { properties: ['height'] })
```

Serve immutable files with HTTP Range support and CORS exposing `Content-Range`.
MapLibre integration requires the distributed `worker.js` and its imports to
remain available to your bundler. Other dependencies remain external in `dist`.

Project code is MIT; the MVT encoder includes code adapted from `@cogp/reader`
with its MIT copyright notice retained in source and compiled output. See
`src/COGP-LICENSE.txt`. The optional vendored Esri comparison reader is not
included in this package.
