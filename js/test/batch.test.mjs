import assert from 'node:assert/strict'
import { toGeometryBatch, batchToGeoJSON, batchToGeoArrow, batchToMvt } from '../dist/index.js'
const lod = { geometry_type: 'Point', scale: [0.5, 0.25], offset: [-100, 40] }
const batch = toGeometryBatch({
  geometry: 'overview',
  lod,
  rows: [7, 2],
  coords: [
    [4, 8],
    [0, 0],
  ],
  columns: { name: ['a', null] },
})
assert.ok(batch.geometry.x instanceof Int32Array)
assert.deepEqual([...batch.geometry.geometryOffsets], [0, 1, 2])
const features = batchToGeoJSON(batch).features
assert.deepEqual(
  features.map((f) => f.geometry.coordinates),
  [
    [-98, 42],
    [-100, 40],
  ],
)
assert.deepEqual(
  features.map((f) => f.id),
  [7, 2],
)
assert.equal(features[1].properties.name, null)
assert.ok(batchToGeoArrow(batch).byteLength > 0)
const point = new Uint8Array(21)
const view = new DataView(point.buffer)
view.setUint8(0, 1)
view.setUint32(1, 1, true)
view.setFloat64(5, 12.5, true)
view.setFloat64(13, 35.5, true)
const primary = toGeometryBatch({ geometry: 'primary', rows: [9], wkb: [point], columns: {} })
assert.ok(primary.geometry.x instanceof Float64Array)
assert.deepEqual(batchToGeoJSON(primary).features[0].geometry.coordinates, [12.5, 35.5])
for (const [type, coords, expected] of [
  [
    'MultiLineString',
    [
      [
        [0, 0],
        [2, 3],
      ],
    ],
    [
      [
        [-100, 40],
        [-99, 40.75],
      ],
    ],
  ],
  [
    'MultiPolygon',
    [
      [
        [
          [0, 0],
          [2, 0],
          [0, 0],
        ],
        [
          [1, 1],
          [1, 2],
          [1, 1],
        ],
      ],
    ],
    [
      [
        [
          [-100, 40],
          [-99, 40],
          [-100, 40],
        ],
        [
          [-99.5, 40.25],
          [-99.5, 40.5],
          [-99.5, 40.25],
        ],
      ],
    ],
  ],
]) {
  const b = toGeometryBatch({
    geometry: 'overview',
    lod: { ...lod, geometry_type: type },
    rows: [3],
    coords: [coords],
    columns: {},
  })
  assert.deepEqual(batchToGeoJSON(b).features[0].geometry.coordinates, expected)
}
const empty = toGeometryBatch({ geometry: 'overview', lod, rows: [], coords: [], columns: {} })
assert.equal(batchToGeoJSON(empty).features.length, 0)
assert.ok(batchToGeoArrow(empty).byteLength > 0)
assert.throws(
  () => toGeometryBatch({ geometry: 'primary', rows: [1], wkb: [], columns: {} }),
  /length/,
)
console.log(
  'geometry batch: overview/primary, typed coordinates, offsets, IDs, holes, empty, GeoJSON and GeoArrow',
)

const { toMvt } = await import('../dist/mvt.js')
assert.deepEqual(
  batchToMvt(batch, { z: 0, x: 0, y: 0 }),
  toMvt(batch, { z: 0, x: 0, y: 0, layer: 'shapes' }),
)
const nullPrimary = toGeometryBatch({ geometry: 'primary', rows: [1], wkb: [null], columns: {} })
assert.equal(batchToGeoJSON(nullPrimary).features[0].geometry, null)
const { readFeatures } = await import('../dist/query.js')
const calledZooms = []
const sparseReader = {
  lod: { zooms: [8, 11, 14], tile_size: 512 },
  finestZoom: 14,
  lodForZoom(z) {
    assert.ok([8, 11, 14].includes(z))
    return { ...lod, scale: [1e-7, 1e-7], zooms: [z] }
  },
  async readLodByTile(lod, z, x, y, columns) {
    calledZooms.push(z)
    return { rows: [], coords: [], columns: Object.fromEntries(columns.map((n) => [n, []])) }
  },
}
await readFeatures(sparseReader, {
  bbox: [-105, 39.7, -104.999, 39.701],
  resolution: 360 / (512 * 2 ** 13),
})
assert.ok(
  calledZooms.length && calledZooms.every((z) => z === 14),
  'sparse query uses declared zoom for geometry and spill tags',
)
