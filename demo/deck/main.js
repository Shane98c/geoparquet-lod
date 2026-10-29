// Bundled by `npm --prefix js run demo:bundle` into demo/dist/deck.js.
import { openReader, httpSource, readFeatures, batchToGeoArrow } from '../../js/src/index.ts'
import { Deck, MapView } from '@deck.gl/core'
import { TileLayer } from '@deck.gl/geo-layers'
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
  generation = 0,
  state = { longitude: -105, latitude: 40, zoom: 10, pitch: 0, bearing: 0 }
const map = new Deck({
  parent: document.querySelector('#map'),
  views: new MapView({ repeat: true }),
  controller: true,
  initialViewState: state,
  onViewStateChange: ({ viewState }) => {
    state = viewState
  },
  getTooltip: ({ index, tile }) =>
    tile?.content?.rows && index >= 0 ? `Source row ${tile.content.rows[index]}` : null,
})
// Whole features straddling tile edges are drawn by both tiles; an almost opaque
// fill keeps that from showing as brighter overlaps.
const fill = [52, 185, 163, 225],
  line = [113, 237, 209]
// The GeoArrow layers read coordinates from the Arrow buffers directly; polygon
// triangulation runs in a worker pool served from the bundle rather than a CDN.
const earcutWorkerUrl = new URL('../dist/earcut-worker.min.js', import.meta.url).href
function family(table) {
  return table.schema.fields
    .map((f) => f.metadata.get('ARROW:extension:name'))
    .find((name) => name?.startsWith('geoarrow.'))
}
function featureLayer(id, table, zoom) {
  const shared = { id, data: table, pickable: true, _validate: false }
  const kind = family(table)
  if (kind === 'geoarrow.multipoint' || kind === 'geoarrow.point')
    return new GeoArrowScatterplotLayer({ ...shared, getFillColor: line, radiusMinPixels: 3 })
  if (kind === 'geoarrow.multilinestring' || kind === 'geoarrow.linestring')
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
// One tile per LOD tile: deck's TileLayer caches loaded tiles and their GPU
// buffers, cancels abandoned loads, and overzooms the finest level.
function tileLayer() {
  const zooms = reader.lod.zooms
  return new TileLayer({
    id: `tiles-${generation}`,
    tileSize: reader.lod.tile_size,
    minZoom: Math.min(...zooms),
    maxZoom: Math.max(...zooms),
    extent: reader.bounds,
    getTileData: async ({ index, bbox, signal }) => {
      const batch = await readFeatures(reader, {
        bbox: [bbox.west, bbox.south, bbox.east, bbox.north],
        resolution: 360 / (reader.lod.tile_size * 2 ** index.z),
        signal,
        geometry: 'overview',
        maxTiles: 16,
      })
      return {
        table: tableFromIPC(batchToGeoArrow(batch, { crs: 'OGC:CRS84' })),
        rows: batch.rowIndex,
      }
    },
    renderSubLayers: ({ id, tile, data }) =>
      data?.rows.length ? featureLayer(id, data.table, tile.index.z) : null,
    onViewportLoad: (tiles) => {
      const loaded = tiles.filter((t) => t.content)
      const features = loaded.reduce((n, t) => n + t.content.rows.length, 0)
      status.textContent = `${features.toLocaleString()} overview features in ${loaded.length} tiles · zoom ${state.zoom.toFixed(1)}`
    },
    onTileError: (error) => {
      if (error?.name !== 'AbortError') status.textContent = error.message
    },
  })
}
async function open() {
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
    generation++
    map.setProps({ initialViewState: state, layers: [tileLayer()] })
  } catch (error) {
    status.textContent = error.message
  }
}
document.querySelector('form').addEventListener('submit', (event) => {
  event.preventDefault()
  open()
})
open()
