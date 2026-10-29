// A feature's properties in the finest overview tile and in the exact tile
// one zoom in, which reads carried attributes from their source columns.
//   node test/exact-carried.test.mjs file.parquet cases.json
// cases.json: [{ row, bbox, tiles: [[z, x, y], [z + 1, x, y]], source: {...}, carried: {...} }]
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { VectorTile } from '@mapbox/vector-tile'
import { PbfReader } from 'pbf'
import { coalescing, openReader, renderTile } from '../dist/index.js'

const [path, casesPath] = process.argv.slice(2)
if (!path) throw new Error('exact-carried.test.mjs needs a file and cases')
const data = await readFile(path)
const reader = await openReader(
  coalescing({
    byteLength: data.length,
    slice: async (a, b) => Uint8Array.from(data.subarray(a, b)).buffer,
  }),
)
const properties = async ([z, x, y], row) => {
  const layer = new VectorTile(new PbfReader(new Uint8Array(await renderTile(reader, z, x, y))))
    .layers.features
  for (let i = 0; i < layer.length; i++)
    if (layer.feature(i).id === row) return layer.feature(i).properties
}
const cases = JSON.parse(await readFile(casesPath, 'utf8'))
for (const { row, bbox, tiles, source, carried } of cases) {
  const [finest, exact] = await Promise.all(tiles.map((tile) => properties(tile, row)))
  assert.ok(finest && exact, `row ${row} is in both tiles`)
  assert.deepEqual(exact, finest, `row ${row}'s properties past the finest zoom`)
  assert.deepEqual({ ...exact }, carried, `row ${row}'s properties are its carried values`)
  const names = Object.keys(source)
  const primary = await reader.readPrimary(bbox, names)
  const at = primary.rows.indexOf(row)
  for (const name of names)
    assert.equal(primary.columns[name][at], source[name], `row ${row}'s exact ${name}`)
}
console.log(`exact tiles: ${cases.length} features carry their finest LOD's values`)
