// Bundled by `npm --prefix js run demo:bundle` into demo/dist/deck.js.
import { openReader, httpSource, readGeometryBatch, batchToGeoArrow } from '../../js/src/index.ts'
import { Deck, MapView, WebMercatorViewport } from '@deck.gl/core'
import {
  GeoArrowPathLayer,
  GeoArrowPolygonLayer,
  GeoArrowScatterplotLayer,
} from '@geoarrow/deck.gl-layers'
import { tableFromIPC } from 'apache-arrow'

const status = document.querySelector('#status'),
  url = document.querySelector('#url'),
  params = new URLSearchParams(location.search)
url.value = new URL(params.get('url') ?? url.value, location.href).href
let reader,
  request,
  timer,
  rows = [],
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
  getTooltip: ({ index }) =>
    index >= 0 && index < rows.length ? `Source row ${rows[index]}` : null,
})
function schedule() {
  clearTimeout(timer)
  timer = setTimeout(draw, 120)
}
const fill = [52, 185, 163, 140],
  line = [113, 237, 209]
// The GeoArrow layers read coordinates from the Arrow buffers directly; polygon
// triangulation runs in a worker pool, served from the bundle rather than a CDN.
const earcutWorkerUrl = new URL('../dist/earcut-worker.min.js', import.meta.url).href
function layerFor(table, zoom) {
  const family = table.schema.fields
    .map((f) => f.metadata.get('ARROW:extension:name'))
    .find((name) => name?.startsWith('geoarrow.'))
  const shared = { id: 'features', data: table, pickable: true, _validate: false }
  if (family === 'geoarrow.multipoint' || family === 'geoarrow.point')
    return new GeoArrowScatterplotLayer({
      ...shared,
      getFillColor: line,
      radiusMinPixels: 3,
    })
  if (family === 'geoarrow.multilinestring' || family === 'geoarrow.linestring')
    return new GeoArrowPathLayer({ ...shared, getColor: line, widthMinPixels: 1 })
  return new GeoArrowPolygonLayer({
    ...shared,
    filled: true,
    stroked: zoom >= 14, // placeholder squares at coarse zooms read as texture, not outlines
    getFillColor: fill,
    getLineColor: line,
    lineWidthMinPixels: 1,
    earcutWorkerUrl,
    earcutWorkerPoolSize: 4,
  })
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
    const table = tableFromIPC(batchToGeoArrow(batch, { crs: 'OGC:CRS84' }))
    rows = batch.rowIndex
    map.setProps({ layers: [layerFor(table, state.zoom)] })
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
    // ?view=lon,lat,zoom opens at a place; otherwise fit the dataset extent.
    const view = (params.get('view') ?? '').split(',').map(Number)
    state = {
      longitude: (w + e) / 2,
      latitude: (s + n) / 2,
      zoom: Math.max(0, Math.min(16, Math.log2(270 / Math.max(e - w, (n - s) * 1.5, 0.001)))),
      ...(view.length === 3 && view.every(Number.isFinite)
        ? { longitude: view[0], latitude: view[1], zoom: view[2] }
        : {}),
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
