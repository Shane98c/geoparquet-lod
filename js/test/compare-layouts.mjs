// Compare rendered geometry/properties, ignoring row ids that a rewrite changes.
import assert from 'node:assert/strict'
import { open } from 'node:fs/promises'
import { createHash } from 'node:crypto'
import { VectorTile } from '@mapbox/vector-tile'
import { PbfReader } from 'pbf'
import { openReader, coalescing, renderTile } from '../dist/index.js'
const handles = []
const readers = await Promise.all(
  process.argv.slice(2).map(async (path) => {
    const file = await open(path)
    handles.push(file)
    return openReader(
      coalescing({
        byteLength: (await file.stat()).size,
        async slice(a, b) {
          const bytes = new Uint8Array(b - a),
            result = await file.read(bytes, 0, bytes.length, a)
          assert.equal(result.bytesRead, bytes.length)
          return bytes.buffer
        },
      }),
    )
  }),
)
assert.equal(readers.length, 2, 'expected prefix.parquet single.parquet')
const canonical = (buffer) => {
  const tile = new VectorTile(new PbfReader(new Uint8Array(buffer))),
    out = []
  for (const layer of Object.values(tile.layers))
    for (let i = 0; i < layer.length; i++) {
      const feature = layer.feature(i)
      out.push(JSON.stringify([feature.type, feature.properties, feature.loadGeometry()]))
    }
  return out.sort()
}
const results = []
for (const [z, lon, lat] of [
  [6, 137, 36],
  [8, 139.76, 35.68],
  [11, 139.76, 35.68],
  [13, 139.7, 35.66],
  [13, 139.95, 35.45],
  [15, 139.76, 35.68],
  [15, 135.5, 34.69],
]) {
  const x = Math.floor(((lon + 180) / 360) * 2 ** z),
    y = Math.floor(((1 - Math.asinh(Math.tan((lat * Math.PI) / 180)) / Math.PI) / 2) * 2 ** z)
  for (const dx of [0, 1]) {
    const [a, b] = await Promise.all(
      readers.map(async (reader) => canonical(await renderTile(reader, z, x + dx, y))),
    )
    assert.deepEqual(b, a, `same rendered geometry and attributes ${z}/${x + dx}/${y}`)
    results.push({
      z,
      x: x + dx,
      y,
      features: a.length,
      sha256: createHash('sha256').update(JSON.stringify(a)).digest('hex'),
    })
  }
}
console.log(JSON.stringify(results, null, 2))
await Promise.all(handles.map((h) => h.close()))
