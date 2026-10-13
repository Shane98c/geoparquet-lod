// Profile display density before changing the writer's thinning policy.
// node test/dropping-profile.mjs lod.parquet baseline.pmtiles > profile.json
import { open } from 'node:fs/promises'
import { createHash } from 'node:crypto'
import { VectorTile } from '@mapbox/vector-tile'
import { PbfReader } from 'pbf'
import { PMTiles } from 'pmtiles'
import { openReader, coalescing, renderTile } from '../dist/index.js'

const [lodPath, pmPath, sampleSet = 'calibration'] = process.argv.slice(2)
if (!lodPath || !pmPath) throw new Error('Supply LOD Parquet and PMTiles paths')
if (!['calibration', 'held-out'].includes(sampleSet)) throw new Error('Unknown sample set')
const handles = []
async function source(path) {
  const file = await open(path)
  handles.push(file)
  const size = (await file.stat()).size
  return {
    byteLength: size,
    getKey: () => path,
    async slice(start, end = size) {
      const bytes = new Uint8Array(end - start)
      const read = await file.read(bytes, 0, bytes.length, start)
      if (read.bytesRead !== bytes.length) throw new Error('Short read')
      return bytes.buffer
    },
    async getBytes(start, length) {
      return { data: await this.slice(start, start + length) }
    },
  }
}
function profile(data) {
  if (!data) return { features: 0, areaPixels: 0, interior: 0, areaQuantiles: [] }
  const tile = new VectorTile(new PbfReader(new Uint8Array(data)))
  let features = 0,
    areaPixels = 0
  const interiorAreas = []
  for (const layer of Object.values(tile.layers))
    for (let i = 0; i < layer.length; i++) {
      const feature = layer.feature(i)
      if (feature.type !== 3) continue
      features++
      let area = 0,
        interior = true
      for (const ring of feature.loadGeometry()) {
        for (let j = 0; j < ring.length; j++) {
          const a = ring[j],
            b = ring[(j + 1) % ring.length]
          area += a.x * b.y - b.x * a.y
          if (a.x <= 0 || a.y <= 0 || a.x >= layer.extent || a.y >= layer.extent) interior = false
        }
      }
      area = (Math.abs(area) / 2) * (512 / layer.extent) ** 2
      areaPixels += area
      if (interior) interiorAreas.push(area)
    }
  interiorAreas.sort((a, b) => a - b)
  return {
    sha256: createHash('sha256').update(new Uint8Array(data)).digest('hex'),
    features,
    areaPixels,
    interior: interiorAreas.length,
    areaQuantiles: [0, 0.01, 0.05, 0.1, 0.25, 0.5].map(
      (q) => interiorAreas[Math.floor(q * (interiorAreas.length - 1))] ?? 0,
    ),
  }
}
const calibrationLocations = [
  ['Tokyo', 139.76, 35.68],
  ['Osaka', 135.5, 34.69],
  ['Chiba', 139.95, 35.45],
  ['Sapporo', 141.35, 43.06],
  ['Fukuoka', 130.4, 33.59],
  ['Nagoya', 136.91, 35.18],
  ['rural Shikoku', 133.45, 33.78],
  ['rural Tohoku', 140.52, 39.5],
]
const heldOutLocations = [
  ['Hiroshima', 132.46, 34.39],
  ['Sendai', 140.87, 38.27],
  ['Kyoto', 135.77, 35.01],
  ['Naha', 127.68, 26.21],
  ['Narita', 140.32, 35.78],
  ['rural Niigata', 138.76, 37.1],
]
const locations = sampleSet === 'held-out' ? heldOutLocations : calibrationLocations
const xy = (z, lon, lat) => [
  Math.floor(((lon + 180) / 360) * 2 ** z),
  Math.floor(((1 - Math.asinh(Math.tan((lat * Math.PI) / 180)) / Math.PI) / 2) * 2 ** z),
]
const output = []
try {
  const lod = await openReader(coalescing(await source(lodPath)))
  const pm = new PMTiles(await source(pmPath))
  for (const z of [6, 7, 8, 9, 10, 11, 12, 13, 14]) {
    // Nearby cities can share a coarse tile. Exclude all calibration XYZs
    // from validation, even when the place names differ.
    const seen = new Set(
      sampleSet === 'held-out'
        ? calibrationLocations.map(([, lon, lat]) => xy(z, lon, lat).join(','))
        : [],
    )
    for (const [name, lon, lat] of locations) {
      const [x, y] = xy(z, lon, lat)
      if (seen.has(`${x},${y}`)) continue
      seen.add(`${x},${y}`)
      const a = await renderTile(lod, z, x, y, { properties: [] })
      const b = (await pm.getZxy(z, x, y))?.data
      const result = { name, z, x, y, lod: profile(a), pmtiles: profile(b) }
      output.push(result)
      console.error(`${name} z${z}: ${result.lod.features} / ${result.pmtiles.features}`)
    }
  }
  console.log(JSON.stringify(output, null, 2))
} finally {
  await Promise.all(handles.map((f) => f.close()))
}
