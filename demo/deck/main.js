// Run `cd js && npm run demo:bundle`, then serve the repository root over HTTP.
import { openReader, httpSource, readGeometryBatch, batchToGeoJSON } from '../dist/index.js'
const { Deck, MapView, WebMercatorViewport, GeoJsonLayer } = globalThis.deck
const status = document.querySelector('#status'),
  url = document.querySelector('#url')
url.value = new URL(
  new URLSearchParams(location.search).get('url') ?? '/buildings.lod.parquet',
  location.href,
).href
let reader,
  request,
  timer,
  state = { longitude: -105, latitude: 40, zoom: 10, pitch: 0, bearing: 0 }
const map = new Deck({
  parent: document.querySelector('#map'),
  views: new MapView({ repeat: true }),
  controller: true,
  initialViewState: state,
  onViewStateChange: ({ viewState }) => {
    state = viewState
    schedule()
  },
  onResize: () => schedule(),
})
function schedule() {
  clearTimeout(timer)
  timer = setTimeout(draw, 120)
}
async function draw() {
  if (!reader) return
  request?.abort()
  request = new AbortController()
  const signal = request.signal
  try {
    const viewport = new WebMercatorViewport({ ...state, width: innerWidth, height: innerHeight })
    let [west, south, east, north] = viewport.getBounds()
    const wrap = (lon) => ((((lon + 180) % 360) + 360) % 360) - 180
    const bbox =
      east - west >= 360 ? [-180, south, 180, north] : [wrap(west), south, wrap(east), north]
    bbox[1] = Math.max(-85, bbox[1])
    bbox[3] = Math.min(85, bbox[3])
    const batch = await readGeometryBatch(reader, {
      bbox,
      resolution: 360 / (512 * 2 ** state.zoom),
      signal,
      geometry: document.querySelector('#primary').checked ? 'primary' : 'overview',
    })
    signal.throwIfAborted()
    // GeoJSON is a small-data adapter. Large applications can consume the typed
    // geometry batch directly or batchToGeoArrow through a GeoArrow deck layer.
    map.setProps({
      layers: [
        new GeoJsonLayer({
          id: 'features',
          data: batchToGeoJSON(batch),
          filled: true,
          stroked: true,
          getFillColor: [52, 185, 163, 140],
          getLineColor: [113, 237, 209],
          lineWidthMinPixels: 1,
          pointRadiusMinPixels: 3,
          pickable: true,
        }),
      ],
      getTooltip: ({ object }) => (object ? `Source row ${object.id}` : null),
    })
    status.textContent = `${batch.rowIndex.length.toLocaleString()} ${batch.source} features · zoom ${state.zoom.toFixed(1)}`
  } catch (error) {
    if (!signal.aborted) status.textContent = error.message
  }
}
async function open() {
  request?.abort()
  status.textContent = 'Opening…'
  try {
    reader = await openReader(await httpSource([url.value]))
    const [w, s, e, n] = reader.bounds
    state = {
      longitude: (w + e) / 2,
      latitude: (s + n) / 2,
      zoom: Math.max(0, Math.min(16, Math.log2(270 / Math.max(e - w, (n - s) * 1.5, 0.001)))),
      pitch: 0,
      bearing: 0,
    }
    map.setProps({ initialViewState: state })
    schedule()
  } catch (error) {
    status.textContent = error.message
  }
}
document.querySelector('form').addEventListener('submit', (event) => {
  event.preventDefault()
  open()
})
document.querySelector('#primary').addEventListener('change', schedule)
open()
