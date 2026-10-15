// Bytes and requests for one tile at several zooms, against a local or remote file.
// Every tile uses a fresh reader, so each line is a cold read; `open` is reported once.
//   node test/measure.mjs http://localhost:8083/county.parquet 5,9,13,16 -122.27 37.80
import { openReader, httpSource, coalescing, renderTile } from '../dist/index.js'
const [url, zoomArg = '5,9,13,16', lonArg = '-122.27', latArg = '37.80'] = process.argv.slice(2)
const zooms = zoomArg.split(',').map(Number),
  lon = Number(lonArg),
  lat = Number(latArg)
let reqs = 0,
  bytes = 0
const real = globalThis.fetch
globalThis.fetch = async (u, i) => {
  const r = await real(u, i)
  if (r.status === 206) {
    reqs++
    bytes += Number(r.headers.get('content-length') || 0)
  }
  return r
}
const t0 = performance.now()
const first = await openReader(coalescing(await httpSource([url])))
console.log(
  `open ${Math.round(performance.now() - t0)} ms, ${reqs} req, ${(bytes / 1e6).toFixed(2)} MB; lods ${Object.keys(first.lods).join(',')}; attributes ${first.lod.attributes.map((a) => a.name).join(',') || 'none'}`,
)
for (const z of zooms) {
  const x = Math.floor(((lon + 180) / 360) * 2 ** z)
  const y = Math.floor(
    ((1 -
      Math.log(Math.tan((lat * Math.PI) / 180) + 1 / Math.cos((lat * Math.PI) / 180)) / Math.PI) /
      2) *
      2 ** z,
  )
  const reader = await openReader(coalescing(await httpSource([url])))
  reqs = 0
  bytes = 0
  const t = performance.now()
  const tile = await renderTile(reader, z, x, y, { layer: 'features' })
  console.log(
    `z${z} (${z > reader.finestZoom ? 'primary' : reader.lodForZoom(z).name}): mvt ${(tile.byteLength / 1e3).toFixed(0)} KB, ${reqs} req, ${(bytes / 1e6).toFixed(2)} MB, ${Math.round(performance.now() - t)} ms`,
  )
}
