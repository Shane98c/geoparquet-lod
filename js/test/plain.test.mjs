// A GeoParquet file without LODs: every zoom's tile draws the primary geometry
// of the rows whose covering meets it, with the requested source attributes.
//   node test/plain.test.mjs file.parquet cases.json
// cases.json: { rows, extent, cases: [{ row, bbox, tiles: [[z, x, y], ...], properties: {...}, matches }] }
// where `extent` bounds every row and `matches` counts the rows whose bbox meets the case's.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { VectorTile } from '@mapbox/vector-tile'
import { PbfReader } from 'pbf'
import {
  coalescing,
  GeoParquetReader,
  openReader,
  readFeatures,
  renderTile,
} from '../dist/index.js'

const [path, casesPath] = process.argv.slice(2)
if (!path) throw new Error('plain.test.mjs needs a file and cases')
const data = await readFile(path)
const source = coalescing({
  byteLength: data.length,
  slice: async (a, b) => Uint8Array.from(data.subarray(a, b)).buffer,
})
const reader = await openReader(source)
assert.equal(reader.lod, undefined)
assert.equal(reader.finestZoom, -Infinity)
const { rows, extent, cases } = JSON.parse(await readFile(casesPath, 'utf8'))
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
const contains = (outer, inner) =>
  outer[0] <= inner[0] && outer[1] <= inner[1] && outer[2] >= inner[2] && outer[3] >= inner[3]
assert.ok(contains(reader.bounds, extent), `bounds ${reader.bounds} hold every row`)

// The first row group's extent unknown, then wrapping the antimeridian: the
// dataset bounds still reach every longitude.
const unknown = structuredClone(reader.metadata)
for (const { meta_data } of unknown.row_groups[0].columns) {
  delete meta_data.statistics
  delete meta_data.geospatial_statistics
}
const blind = GeoParquetReader.fromMetadata(source, unknown).bounds
assert.deepEqual([blind[0], blind[2]], [-180, 180], 'an unknown group spans every longitude')
assert.ok(blind[1] < -85 && blind[3] > 85, 'and every Web Mercator latitude')
const geometry = (metadata) =>
  metadata.row_groups[0].columns.find((c) => c.meta_data.path_in_schema[0] === 'geometry')
if (geometry(reader.metadata).meta_data.geospatial_statistics?.bbox) {
  const wrapped = structuredClone(reader.metadata)
  const box = geometry(wrapped).meta_data.geospatial_statistics.bbox
  ;[box.xmin, box.xmax] = [170, -170]
  const bounds = GeoParquetReader.fromMetadata(source, wrapped).bounds
  assert.deepEqual(bounds, [-180, reader.bounds[1], 180, reader.bounds[3]], 'a wrapped group')
}
console.log(`plain GeoParquet: ${cases.length} features at every zoom`)
