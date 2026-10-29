// straddler copies reach the tiles they belong to through the key interval
// read and, given the file laid out with a spatial directory, through that
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { LodReader, SpatialReader, coalescing, openReader, renderTile } from '../dist/index.js'
import { nested } from './columnar.mjs'

const [path, casesPath, laidOutPath] = process.argv.slice(2)
if (!path) {
  console.log('spill test skipped: no fixture given')
  process.exit(0)
}
const open = async (path) => {
  const parquet = await readFile(path)
  return {
    byteLength: parquet.length,
    slice: async (a, b) => Uint8Array.from(parquet.subarray(a, b)).buffer,
  }
}
const source = await open(path)
const reader = await LodReader.open(coalescing(source))
assert.ok(reader.lod.key, 'fixture stores a key')
const spatial = laidOutPath && (await openReader(coalescing(await open(laidOutPath))))
if (spatial) assert.ok(spatial instanceof SpatialReader, 'the laid-out file has a directory')
const cases = JSON.parse(await readFile(casesPath, 'utf8'))
let copies = 0
for (const { z, x, y, id, attribute = 'h' } of cases) {
  const lod = reader.lodForZoom(z)
  const found = await reader.readLodByTile(lod, z, x, y, [attribute])
  const { rows, columns, geometry } = found
  const at = rows.indexOf(id)
  assert.ok(at >= 0, `row ${id} reaches tile ${z}/${x}/${y} but was not read`)
  assert.ok(nested(geometry, at, lod.geometry_type).length, 'the copy carries geometry')
  assert.ok(columns[attribute][at] !== undefined, 'the copy carries its attributes')
  if (attribute === 'zone') assert.match(columns[attribute][at], /^zone\d$/)
  copies++
  const mvt = await renderTile(reader, z, x, y)
  assert.ok(mvt.byteLength > 0)
  if (spatial) {
    assert.deepEqual(
      await spatial.readLodByTile(spatial.lodForZoom(z), z, x, y, [attribute]),
      found,
      `the directory reads the same rows and copies for ${z}/${x}/${y}`,
    )
    assert.deepEqual(new Uint8Array(await renderTile(spatial, z, x, y)), new Uint8Array(mvt))
  }
}
// rows with a key, which come first, have geometry; the rest have a null covering,
// which no bbox matches, so the primary path reads exactly the keyed rows
const keyed = reader.metadata.row_groups.reduce((n, group) => {
  const key = group.columns.find(
    (c) => c.meta_data.path_in_schema.join('.') === reader.lod.key.column,
  )
  return n + Number(group.num_rows) - Number(key.meta_data.statistics.null_count)
}, 0)
const world = [-180, -90, 180, 90]
for (const r of spatial ? [reader, spatial] : [reader]) {
  const { rows } = await r.readPrimary(world)
  assert.deepEqual(rows, [...Array(keyed).keys()], 'a world query reads every row with geometry')
}
// the layout requires key statistics: a row group without them fails the read
const stripped = await LodReader.open(coalescing(source))
const keyChunk = stripped.metadata.row_groups[0].columns.find(
  (c) => c.meta_data.path_in_schema.join('.') === stripped.lod.key.column,
)
delete keyChunk.meta_data.statistics
const [first] = cases
await assert.rejects(
  stripped.readLodByTile(stripped.lodForZoom(first.z), first.z, first.x, first.y),
  /no statistics for key column/,
)
console.log(`spill tests passed, ${copies} copies found`)
