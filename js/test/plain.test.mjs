// A GeoParquet file without LODs: every zoom's tile draws the primary geometry
// of the rows whose covering meets it, with the requested source attributes.
//   node test/plain.test.mjs file.parquet cases.json
// cases.json: { rows, cases: [{ row, bbox, tiles: [[z, x, y], ...], properties: {...}, matches }] }
// where `matches` counts the rows whose bbox meets the case's.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { VectorTile } from '@mapbox/vector-tile'
import { PbfReader } from 'pbf'
import { coalescing, openReader, readFeatures, renderTile } from '../dist/index.js'

const [path, casesPath] = process.argv.slice(2)
if (!path) throw new Error('plain.test.mjs needs a file and cases')
const data = await readFile(path)
const reader = await openReader(
  coalescing({
    byteLength: data.length,
    slice: async (a, b) => Uint8Array.from(data.subarray(a, b)).buffer,
  }),
)
assert.equal(reader.lod, undefined)
assert.equal(reader.finestZoom, -Infinity)
const { rows, cases } = JSON.parse(await readFile(casesPath, 'utf8'))
const names = Object.keys(cases[0].properties)
const features = async ([z, x, y], properties) => {
  const tile = await renderTile(reader, z, x, y, { properties })
  const layer = new VectorTile(new PbfReader(new Uint8Array(tile))).layers.features
  return Array.from({ length: layer?.length ?? 0 }, (_, i) => layer.feature(i))
}

assert.equal((await features([0, 0, 0], [])).length, rows, 'z0 draws every row')
for (const { row, bbox, tiles, properties, matches } of cases) {
  for (const tile of tiles) {
    const found = (await features(tile, names)).find((f) => f.id === row)
    assert.ok(found, `row ${row} is in tile ${tile}`)
    assert.deepEqual({ ...found.properties }, properties, `row ${row}'s properties in ${tile}`)
  }
  const bare = (await features(tiles[0], undefined)).find((f) => f.id === row)
  assert.deepEqual({ ...bare.properties }, {}, 'no properties unless requested')
  const batch = await readFeatures(reader, { bbox, resolution: 1, columns: names })
  assert.equal(batch.source, 'primary')
  assert.equal(batch.rowIndex.length, matches, `rows meeting row ${row}'s bbox`)
  const at = batch.rowIndex.indexOf(row)
  assert.ok(at >= 0, `row ${row} is selected by its bbox`)
  for (const name of names) assert.equal(batch.columns[name][at], properties[name])
}
console.log(`plain GeoParquet: ${cases.length} features at every zoom`)
