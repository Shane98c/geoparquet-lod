// straddler copies reach the tiles they belong to through the key interval read
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { LodReader, coalescing, renderTile } from '../dist/index.js'

const [path, casesPath] = process.argv.slice(2)
if (!path) {
  console.log('spill test skipped: no fixture given')
  process.exit(0)
}
const parquet = await readFile(path)
const source = {
  byteLength: parquet.length,
  slice: async (a, b) => Uint8Array.from(parquet.subarray(a, b)).buffer,
}
const reader = await LodReader.open(coalescing(source))
assert.ok(reader.lod.key, 'fixture stores a key')
const cases = JSON.parse(await readFile(casesPath, 'utf8'))
let copies = 0
for (const { z, x, y, id, attribute = 'h' } of cases) {
  const lod = reader.lodForZoom(z)
  const { rows, coords, columns } = await reader.readLodByTile(lod, z, x, y, [attribute])
  const at = rows.indexOf(id)
  assert.ok(at >= 0, `row ${id} reaches tile ${z}/${x}/${y} but was not read`)
  assert.ok(Array.isArray(coords[at]) && coords[at].length, 'the copy carries geometry')
  assert.ok(columns[attribute][at] !== undefined, 'the copy carries its attributes')
  if (attribute === 'zone') assert.match(columns[attribute][at], /^zone\d$/)
  copies++
  const mvt = await renderTile(reader, z, x, y)
  assert.ok(mvt.byteLength > 0)
}
console.log(`spill tests passed, ${copies} copies found`)
