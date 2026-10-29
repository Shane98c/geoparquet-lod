import assert from 'node:assert/strict'
import { geometryColumnFromWkb } from '@cogp/reader'
import { batchToGeoJSON, batchToGeoArrow, batchToMvt, readFeatures } from '../dist/index.js'
import { latitudeOf, longitudeOf, mercatorX, mercatorY } from '../dist/web-mercator.js'
import { columnar, nested } from './columnar.mjs'
// LOD coordinates are EPSG:3857 metres; the GeoJSON and GeoArrow adapters return degrees
const lod = { geometry_type: 'Point', scale: [1000, 500], offset: [mercatorX(-100), mercatorY(40)] }
const degrees = (value) =>
  typeof value[0] === 'number'
    ? [
        longitudeOf(lod.offset[0] + lod.scale[0] * value[0]),
        latitudeOf(lod.offset[1] + lod.scale[1] * value[1]),
      ]
    : value.map(degrees)
const overview = (type, features, columns = {}) => ({
  geometry: columnar(type, features, lod.scale, lod.offset),
  rowIndex: features.map((_, i) => 7 - i * 5),
  columns,
  source: 'overview',
})
const batch = overview(
  'Point',
  [
    [4, 8],
    [0, 0],
  ],
  { name: ['a', null] },
)
assert.ok(batch.geometry.x instanceof Int32Array)
assert.deepEqual([...batch.geometry.geometryOffsets], [0, 1, 2])
const features = batchToGeoJSON(batch).features
assert.deepEqual(
  features.map((f) => f.geometry.coordinates),
  [degrees([4, 8]), degrees([0, 0])],
)
const [lon, lat] = features[1].geometry.coordinates
assert.ok(Math.abs(lon + 100) < 1e-9 && Math.abs(lat - 40) < 1e-9, 'the origin is the LOD offset')
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
const primary = {
  geometry: geometryColumnFromWkb([point]),
  rowIndex: [9],
  columns: {},
  source: 'primary',
}
assert.ok(primary.geometry.x instanceof Float64Array)
assert.deepEqual(batchToGeoJSON(primary).features[0].geometry.coordinates, [12.5, 35.5])
for (const [type, coords] of [
  [
    'MultiLineString',
    [
      [
        [0, 0],
        [2, 3],
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
  ],
]) {
  const b = overview(type, [coords])
  assert.deepEqual(batchToGeoJSON(b).features[0].geometry.coordinates, degrees(coords))
}
const empty = overview('Point', [])
assert.equal(batchToGeoJSON(empty).features.length, 0)
assert.ok(batchToGeoArrow(empty).byteLength > 0)
console.log(
  'geometry batch: overview/primary, typed coordinates, offsets, IDs, holes, empty, GeoJSON and GeoArrow',
)

const { toMvt } = await import('../dist/mvt.js')
assert.deepEqual(
  batchToMvt(batch, { z: 0, x: 0, y: 0 }),
  toMvt(batch, { z: 0, x: 0, y: 0, layer: 'shapes' }),
)
const nullPrimary = { ...primary, geometry: geometryColumnFromWkb([null]), rowIndex: [1] }
assert.equal(batchToGeoJSON(nullPrimary).features[0].geometry, null)

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
    return {
      rows: [],
      columns: Object.fromEntries(columns.map((n) => [n, []])),
      geometry: columnar(lod.geometry_type, [], lod.scale, lod.offset),
    }
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

// Overview features come whole from every tile they reach: each is kept once,
// and only when its bounds, in metres, meet the query's.
const square = (x, y, size) => [
  [
    [
      [x, y],
      [x + size, y],
      [x + size, y + size],
      [x, y],
    ],
  ],
]
const metres = { geometry_type: 'MultiPolygon', scale: [1, 1], offset: [0, 0], zooms: [10] }
const tiles = []
const crossing = {
  lod: { zooms: [10], tile_size: 512 },
  finestZoom: 10,
  lodForZoom: () => metres,
  async readLodByTile(lod, z, x, y, columns) {
    tiles.push([z, x, y])
    const features = [[1, square(-100, -100, 200), 10]]
    if (x === 512 && y === 511) features.push([2, square(5000, 5000, 100), 20])
    if (x === 511 && y === 512) features.push([3, square(-1000, -1000, 100), 30])
    return {
      rows: features.map(([id]) => id),
      columns: Object.fromEntries(columns.map((n) => [n, features.map(([, , h]) => h)])),
      geometry: columnar(
        lod.geometry_type,
        features.map(([, coords]) => coords),
        lod.scale,
        lod.offset,
      ),
    }
  },
}
const read = await readFeatures(crossing, {
  bbox: [-0.01, -0.01, 0.01, 0.01],
  resolution: 360 / (512 * 2 ** 10),
  columns: ['h'],
})
assert.equal(tiles.length, 4, 'the query reads the four tiles around the origin')
assert.equal(read.source, 'overview')
assert.deepEqual(read.rowIndex, [1, 3])
assert.deepEqual(read.columns, { h: [10, 30] })
assert.deepEqual(
  read.rowIndex.map((_, i) => nested(read.geometry, i, 'MultiPolygon')),
  [square(-100, -100, 200), square(-1000, -1000, 100)],
)
assert.equal(read.geometry.crs, 'EPSG:3857')

const wkbRows = new Map([
  [4, point],
  [6, point],
])
const exact = await readFeatures(
  {
    async readPrimary() {
      return { rows: [...wkbRows.keys()], wkb: [...wkbRows.values()], columns: {} }
    },
    async readColumns(rows, columns) {
      return Object.fromEntries(columns.map((n) => [n, rows.map((r) => `${n}${r}`)]))
    },
  },
  { bbox: [179, 35, -179, 36], resolution: 1e-5, columns: ['name'], geometry: 'primary' },
)
assert.deepEqual(exact.rowIndex, [4, 6], 'both halves of a wrapped query keep a row once')
assert.deepEqual(exact.columns, { name: ['name4', 'name6'] })
assert.equal(exact.source, 'primary')
assert.deepEqual([...exact.geometry.x], [12.5, 12.5])
console.log('whole-feature reads: declared zooms, deduplication, bounds filter and primary rows')
