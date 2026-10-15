// Cold tile reads from a local packed file through the reader's own coalescer.
// node benchmarks/standard-overviews/measure-packed.mjs packed.parquet tiles.json > packed.json
import { open } from 'node:fs/promises'
import { readFile } from 'node:fs/promises'
import { openReader, coalescing } from '../../js/dist/index.js'

const [path, tilesPath] = process.argv.slice(2)
const tiles = JSON.parse(await readFile(tilesPath, 'utf8'))
const file = await open(path)
const size = (await file.stat()).size
let requests = 0,
  bytes = 0
const source = () =>
  coalescing({
    byteLength: size,
    async slice(a, b = size) {
      requests++
      bytes += b - a
      const buf = Buffer.alloc(b - a)
      await file.read(buf, 0, b - a, a)
      return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.length)
    },
  })
const results = []
for (const tile of tiles) {
  const { z, x, y } = tile
  const row = { ...tile }
  for (const properties of [[], ['height', 'class']]) {
    requests = 0
    bytes = 0
    const reader = await openReader(source())
    const open = { requests, bytes }
    requests = 0
    bytes = 0
    const lod = reader.lodForZoom(z)
    const rows = await reader.readLodByTile(lod, z, x, y, properties)
    const cold = { requests, bytes, features: rows.rows.length }
    requests = 0
    bytes = 0
    await reader.readLodByTile(lod, z, x + 1, y, properties)
    const next = { requests, bytes }
    row[properties.length ? 'styled' : 'geometry'] = { open, cold, next }
    if (!properties.length) row.rows = [...new Set(rows.rows)].sort((a, b) => a - b)
  }
  results.push(row)
}
await file.close()
console.log(JSON.stringify(results))
