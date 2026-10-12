// Bytes and requests per tile for four readers of the same buildings, served from one place:
// this layout, COGP (@cogp/reader), PMTiles and Esri's SOP reference reader. Expects
// japan_buildings.{lod6.parquet,cogp.parquet,pmtiles,sop.parquet} under BASE (default
// http://localhost:8099/), and the Esri reader bundled for Node first. COGP names another
// COGP file to measure, such as the one behind COGP's own demo, and LOD another file in this layout:
//   npx esbuild vendor/esri-sop/index.ts --bundle --platform=node --format=esm --outfile=test/esri-node.mjs
//   node test/compare4.mjs
import { LodReader, httpSource, coalescing, renderTile, tileBounds } from '../dist/index.js'
import { CogpReader, MVT_BUFFER, MVT_EXTENT, toMvt } from '@cogp/reader'
import { PMTiles } from 'pmtiles'
import { Query } from './esri-node.mjs'
import { PbfReader } from 'pbf'

// feature count of an MVT: layers are field 3 of the tile, features field 2 of a layer
const features = (buf) => {
  let n = 0
  const layer = (tag, _, pbf) => {
    if (tag === 2) n++
    pbf.skip(pbf.type)
  }
  new PbfReader(new Uint8Array(buf)).readFields((tag, _, pbf) => {
    if (tag === 3) pbf.readMessage(layer, {})
    else pbf.skip(pbf.type)
  }, {})
  return n
}

const base = process.env.BASE ?? 'http://localhost:8099/'
const cogpFile = process.env.COGP ?? base + 'japan_buildings.cogp.parquet'
const lodFile = process.env.LOD ?? base + 'japan_buildings.lod14.parquet'
const pmFile = process.env.PMTILES ?? 'japan_buildings_z0.pmtiles'
const stats = {}
const real = globalThis.fetch
globalThis.fetch = async (input, init) => {
  const r = await real(input, init)
  const key = String(input instanceof Request ? input.url : input).split('?')[0]
  const e = (stats[key] ??= { requests: 0, bytes: 0 })
  e.requests++
  if ((init?.method ?? 'GET') !== 'HEAD') e.bytes += Number(r.headers.get('content-length') ?? 0)
  return r
}
const snap = (key) => ({
  ...(stats[key.startsWith('http') ? key : base + key] ?? { requests: 0, bytes: 0 }),
})
const delta = (a, b) => ({ requests: b.requests - a.requests, bytes: b.bytes - a.bytes })
const fmt = (d, extra = '') => `${(d.bytes / 1e6).toFixed(2)} MB, ${d.requests} req${extra}`

const tiles = [
  [8, 139.76, 35.68, 'metro, Tokyo'],
  [11, 139.76, 35.68, 'city, Tokyo'],
  [13, 139.7, 35.66, 'downtown, Shibuya'],
  [13, 139.95, 35.45, 'suburbs, Chiba'],
  [15, 139.76, 35.68, 'street, Tokyo station'],
  [15, 135.5, 34.69, 'street, Osaka'],
]
const xy = (z, lon, lat) => [
  Math.floor(((lon + 180) / 360) * 2 ** z),
  Math.floor(((1 - Math.asinh(Math.tan((lat * Math.PI) / 180)) / Math.PI) / 2) * 2 ** z),
]

// open each reader once; per-tile cost is then the cold read on a fresh reader
const open = async (name, fn) => {
  const a = snap(name)
  const r = await fn()
  // let what opening starts in the background (index prefetch) land before counting it as open
  await new Promise((resolve) => setTimeout(resolve, 1500))
  return { r, open: delta(a, snap(name)) }
}
const lodOpen = async () => LodReader.open(coalescing(await httpSource([lodFile])))
const cogpOpen = () => CogpReader.open(cogpFile)
const pmOpen = async () => {
  const p = new PMTiles(base + pmFile)
  await p.getHeader()
  return p
}
const esriOpen = async () => {
  const h = await fetch(base + 'japan_buildings.sop.parquet', { method: 'HEAD' })
  const q = new Query(base + 'japan_buildings.sop.parquet', Number(h.headers.get('content-length')))
  await q.loadMetadata(new AbortController().signal)
  return q
}
const opens = {}
for (const [name, fn] of [
  ['lod', lodOpen],
  ['cogp', cogpOpen],
  ['pmtiles', pmOpen],
  ['esri', esriOpen],
])
  opens[name] = (
    await open(
      name.startsWith('lod')
        ? lodFile
        : name === 'cogp'
          ? cogpFile
          : name === 'pmtiles'
            ? pmFile
            : 'japan_buildings.sop.parquet',
      fn,
    )
  ).open
console.log('| reader | open |\n|---|---|')
for (const [k, v] of Object.entries(opens)) console.log(`| ${k} | ${fmt(v)} |`)

console.log('\n| tile | lod | cogp | pmtiles | esri |\n|---|---|---|---|---|')
for (const [z, lon, lat, label] of tiles) {
  const [x, y] = xy(z, lon, lat)
  const bbox = tileBounds(z, x, y)
  const row = []
  // ours: fresh reader, geometry only
  {
    const key = lodFile
    const r = await lodOpen()
    await new Promise((resolve) => setTimeout(resolve, 1500))
    const a = snap(key)
    const mvt = await renderTile(r, z, x, y, { properties: [] })
    const cold = delta(a, snap(key))
    // the neighbouring tile on the same reader: what a tile costs once the indexes are read
    const b = snap(key)
    await renderTile(r, z, x + 1, y, { properties: [] })
    const warm = delta(b, snap(key))
    row.push(
      fmt(
        cold,
        `, ${(mvt.byteLength / 1e3).toFixed(0)} KB mvt, ${features(mvt)} features; next tile ${fmt(warm)}`,
      ),
    )
  }
  // cogp: fresh reader, the tile bounds, resolution and read call of @cogp/maplibre's tile.js
  {
    const key = cogpFile
    const r = await cogpOpen()
    const a = snap(key)
    const n = 2 ** z
    const latAt = (yy) => (Math.atan(Math.sinh(Math.PI * (1 - (2 * yy) / n))) * 180) / Math.PI
    const pad = MVT_BUFFER / MVT_EXTENT
    const cogpBbox = [
      ((x - pad) / n) * 360 - 180,
      latAt(y + 1 + pad),
      ((x + 1 + pad) / n) * 360 - 180,
      latAt(y - pad),
    ]
    const res = (360 * Math.cos((latAt(y + 0.5) * Math.PI) / 180)) / (512 * n)
    const level = r.selectLevel(res)
    const batch = await r.read({
      bbox: cogpBbox,
      maxLevel: level,
      columns: [r.primaryGeometryColumn],
      useOverview: true,
    })
    const mvt = toMvt(batch, { z, x, y })
    row.push(
      fmt(
        delta(a, snap(key)),
        `, ${(mvt.byteLength / 1e3).toFixed(0)} KB mvt, level ${level}, ${batch.length} features`,
      ),
    )
  }
  // pmtiles: fresh archive handle; parent tile beyond maxzoom
  {
    const key = pmFile
    const p = await pmOpen()
    const a = snap(key)
    const zz = Math.min(z, 14)
    const [px, py] = xy(zz, lon, lat)
    const t = await p.getZxy(zz, px, py)
    const cold = delta(a, snap(key))
    const b = snap(key)
    await p.getZxy(zz, px + 1, py)
    const warm = delta(b, snap(key))
    row.push(
      fmt(
        cold,
        (t
          ? `, ${(t.data.byteLength / 1e3).toFixed(0)} KB tile, ${features(t.data)} features`
          : ', empty') + `; next tile ${fmt(warm)}`,
      ),
    )
  }
  // esri: fresh query, the reference reader's view query for the tile extent
  {
    const key = 'japan_buildings.sop.parquet'
    const q = await esriOpen()
    const a = snap(key)
    const res = await q.execute({
      queryExtents: [{ xmin: bbox[0], ymin: bbox[1], xmax: bbox[2], ymax: bbox[3] }],
      sourceResolution: 360 / (512 * 2 ** z),
      signal: new AbortController().signal,
    })
    row.push(
      fmt(
        delta(a, snap(key)),
        `, level ${res.lod}, ${res.featureCollection.features.length} features${res.featureLimitReached ? ' (limit)' : ''}`,
      ),
    )
  }
  console.log(`| z${z} ${label} | ${row.join(' | ')} |`)
}
