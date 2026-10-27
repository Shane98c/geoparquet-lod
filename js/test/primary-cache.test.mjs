// Tiles past the finest LOD draw exact geometry; zooming in over one large
// feature reuses the geometry the first zoom fetched instead of fetching it again.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { PbfReader } from 'pbf'
import { coalescing, openReader, renderTile } from '../dist/index.js'

const [path] = process.argv.slice(2)
if (!path) throw new Error('primary-cache.test.mjs needs a file with one large feature')
const data = await readFile(path)
const reads = []
const reader = await openReader(
  coalescing({
    byteLength: data.length,
    async slice(a, b) {
      reads.push([a, b])
      return Uint8Array.from(data.subarray(a, b)).buffer
    },
  }),
)

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
const tileRange = (z, [xmin, ymin, xmax, ymax]) => {
  const x = (lon) => Math.floor(((lon + 180) / 360) * 2 ** z)
  const y = (lat) =>
    Math.floor(((1 - Math.asinh(Math.tan((lat * Math.PI) / 180)) / Math.PI) / 2) * 2 ** z)
  const out = []
  for (let tx = x(xmin); tx <= x(xmax); tx++)
    for (let ty = y(ymax); ty <= y(ymin); ty++) out.push([tx, ty])
  return out
}

const bytesRead = () => reads.reduce((n, [a, b]) => n + b - a, 0)
const perZoom = []
for (let z = reader.finestZoom + 1; z <= reader.finestZoom + 3; z++) {
  const before = bytesRead()
  const tiles = tileRange(z, reader.bounds)
  let drawn = 0
  for (const [x, y] of tiles)
    drawn += features(await renderTile(reader, z, x, y, { properties: [] }))
  assert.ok(drawn >= tiles.length / 2, `z${z} draws the feature in its tiles`)
  perZoom.push({ z, tiles: tiles.length, bytes: bytesRead() - before })
}
assert.ok(perZoom[0].bytes > 0, 'the first zoom past the finest LOD reads the exact geometry')
for (const { z, bytes } of perZoom.slice(1))
  assert.equal(bytes, 0, `z${z} reuses the exact geometry already fetched`)
console.log('primary cache:', JSON.stringify(perZoom))
