# GeoParquet LOD JavaScript reader

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

Project code is MIT; the MVT encoder and geometry builder include code adapted
from `@cogp/reader` with its MIT copyright notice retained in source and
compiled output. See
`src/COGP-LICENSE.txt`. The optional vendored Esri comparison reader is not
included in this package.

Use `readFeatures(reader, { bbox, resolution, columns, geometry })` for a
renderer-neutral batch with typed coordinate buffers, hierarchy offsets, source
row IDs and attributes. `batchToGeoJSON`, `batchToGeoArrow` (Arrow IPC), and
`batchToMvt` consume it. GeoArrow conversion uses the `@cogp/reader` dependency; nested or
mixed attributes become display strings. GeoJSON is intended for small results.
The deck.gl example is `demo/deck/index.html` after `npm run demo:bundle`,
served with Range support; it feeds `batchToGeoArrow` output to the GeoArrow
deck.gl layers.

`readColumns` and query `columns` accept struct paths (`details.height`), list
indexes (`floors[0].name`), and map keys (`tags.name`). Null/missing values become
`null`. Struct scalars project only selected leaf pages; collections reconstruct
all selected row-group pages before extracting elements. Flat scalars use the
sparse page projection. See [usage documentation](../docs/usage.md) for
path ambiguity, collection representation, validation scope and adapter limits.
