// Warm-cache CPU profile of tile reads and MVT encoding. Run after the writer/optimizer has finished.
// node [--cpu-prof --cpu-prof-dir=/private/tmp] test/profile-tile.mjs spatial.parquet
import { open } from 'node:fs/promises'
import { toMvt } from '../dist/mvt.js'
import { openReader, coalescing } from '../dist/index.js'

const [path] = process.argv.slice(2)
const file = await open(path)
const source = coalescing({
  byteLength: (await file.stat()).size,
  async slice(start, end) {
    const bytes = new Uint8Array(end - start)
    const { bytesRead } = await file.read(bytes, 0, bytes.length, start)
    if (bytesRead !== bytes.length) throw new Error('truncated read')
    return bytes.buffer
  },
})
const reader = await openReader(source)
const median = (values) => {
  const sorted = [...values].sort((a, b) => a - b)
  return (sorted[Math.floor((sorted.length - 1) / 2)] + sorted[Math.floor(sorted.length / 2)]) / 2
}
const results = []
for (const [z, lon, lat] of [
  [8, 139.76, 35.68],
  [11, 139.76, 35.68],
  [13, 139.7, 35.66],
]) {
  const x = Math.floor(((lon + 180) / 360) * 2 ** z)
  const y = Math.floor(((1 - Math.asinh(Math.tan((lat * Math.PI) / 180)) / Math.PI) / 2) * 2 ** z)
  const lod = reader.lodForZoom(z)
  for (const properties of [[], ['height', 'class']]) {
    const samples = []
    let bytes = 0
    // The first three rounds are warmup.
    for (let round = 0; round < 15; round++) {
      const a = performance.now()
      const data = await reader.readLodByTile(lod, z, x, y, properties)
      const b = performance.now()
      const out = toMvt(
        { geometry: data.geometry, rowIndex: data.rows, columns: data.columns },
        { z, x, y, layer: 'features' },
      )
      const c = performance.now()
      bytes = out.byteLength
      if (round >= 3) samples.push({ readMs: b - a, mvtMs: c - b, totalMs: c - a })
    }
    const entry = { z, properties, bytes }
    for (const key of ['readMs', 'mvtMs', 'totalMs'])
      entry[key] = median(samples.map((sample) => sample[key]))
    results.push(entry)
  }
}
console.log(JSON.stringify({ path, runs: 12, results }, null, 2))
await file.close()
