import assert from 'node:assert/strict'
import { toMvt as reference } from '@cogp/reader'
import { toMvt } from '../dist/mvt.js'
import { mercatorX, mercatorY } from '../dist/web-mercator.js'
import { columnar } from './columnar.mjs'

let seed = 71
const random = () => (seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0) / 2 ** 32
// Include paths crossing tile boundaries, holes, disjoint parts, collapsed
// rings, duplicate vertices, poles and antimeridian wrapping. Compare the
// complete protobuf, including ids, string attributes and empty tiles, with
// COGP's encoder, which writes every value as a string; typed values are
// checked separately below.
for (const type of ['Point', 'MultiLineString', 'MultiPolygon']) {
  for (const scale of [
    [1, 1],
    [-0.1, 0.1],
    [0.00001, 0.00001],
  ]) {
    const features = [],
      rows = [],
      values = []
    for (let i = 0; i < 2200; i++) {
      const x = Math.floor(random() * 800 - 400)
      const y = Math.floor(random() * 200 - 100)
      const dx = Math.floor(random() * 30),
        dy = Math.floor(random() * 30)
      let ring = [
        [x, y],
        [x + dx, y],
        [x + dx, y + dy],
        [x + dx, y + dy],
        [x, y + dy],
        [x, y],
      ]
      if (i === 2199)
        ring = Array.from({ length: 601 }, (_, j) => [
          Math.round(60 * Math.cos((j * Math.PI) / 300)),
          Math.round(60 * Math.sin((j * Math.PI) / 300)),
        ])
      const hole = [
        [x + 1, y + 1],
        [x + 2, y + 1],
        [x + 1, y + 2],
        [x + 1, y + 1],
      ]
      features.push(
        type === 'Point'
          ? [x, y]
          : type === 'MultiLineString'
            ? [ring, hole]
            : [[ring, hole], [ring]],
      )
      rows.push(2 ** 40 + i)
      values.push(
        [
          null,
          '雪',
          3.14,
          true,
          12n,
          new Date('2020-01-01'),
          new Uint8Array(5),
          { a: 1 },
          new Map([['b', 2n]]),
        ][i % 9],
      )
    }
    // COGP's encoder reads lon/lat, so these coordinates are degrees
    const { crs: _, ...geometry } = columnar(type, features, scale, [0, 0])
    for (const [z, x, y] of [
      [0, 0, 0],
      [1, 0, 0],
      [1, 1, 1],
      [4, 0, 8],
      [4, 15, 8],
      [4, 16, 8],
      [4, -1, 8],
      [13, 4096, 4096],
    ]) {
      const strings = values.map((v) =>
        ['number', 'boolean', 'bigint'].includes(typeof v) ? String(v) : v,
      )
      for (const columns of [{}, { value: strings }]) {
        const source = { geometry, rowIndex: rows, columns }
        const options = { z, x, y, layer: 'test' }
        assert.deepEqual(
          new Uint8Array(toMvt(source, options)),
          new Uint8Array(reference(source, options)),
          `${type}/${scale}/${z}/${x}/${y}`,
        )
      }
    }
    const signal = AbortSignal.abort()
    assert.throws(() => toMvt({ geometry }, { z: 0, x: 0, y: 0, signal }), { name: 'AbortError' })
  }
}
console.log(
  'streaming MVT matches reference bytes for clipping, wrapping, winding, properties and cancellation',
)

// LOD geometry is EPSG:3857 on a grid: the same rings in metres encode to the
// same tile as the reference encoder makes of them in degrees
{
  const n = 2000
  const lon = new Float64Array(n * 5),
    lat = new Float64Array(n * 5)
  for (let i = 0; i < n; i++) {
    const x = random() * 40 - 20,
      y = random() * 160 - 80,
      d = random() * 3
    const ring = [
      [x, y],
      [x + d, y],
      [x + d, y + d],
      [x, y + d],
      [x, y],
    ]
    ring.forEach(([a, b], j) => {
      lon[i * 5 + j] = a
      lat[i * 5 + j] = b
    })
  }
  const offsets = (step) => Uint32Array.from({ length: n + 1 }, (_, i) => i * step)
  const column = (x, y, scale, offset, crs) => ({
    length: n,
    types: new Uint8Array(n).fill(6),
    x,
    y,
    geometryOffsets: offsets(1),
    polygonOffsets: offsets(1),
    ringOffsets: offsets(5),
    scale,
    offset,
    ...(crs && { crs }),
  })
  const origin = [-1e6, 2e6]
  const metres = column(
    lon.map((v) => (mercatorX(v) - origin[0]) / 0.5),
    lat.map((v) => (mercatorY(v) - origin[1]) / 0.5),
    [0.5, 0.5],
    origin,
    'EPSG:3857',
  )
  const degrees = column(lon, lat, [1, 1], [0, 0])
  for (const [z, x, y] of [
    [0, 0, 0],
    [3, 4, 2],
    [3, 4, 3],
    [6, 32, 20],
    [2, -1, 1],
  ]) {
    const options = { z, x, y, layer: 'test' }
    assert.deepEqual(
      new Uint8Array(toMvt({ geometry: metres }, options)),
      new Uint8Array(reference({ geometry: degrees }, options)),
      `EPSG:3857 ${z}/${x}/${y}`,
    )
  }
}
console.log('EPSG:3857 LOD coordinates encode as their lon/lat equivalents do')

// Numbers, booleans and safe bigints are typed MVT values, deduplicated by type
// and value; float32-exact numbers are floats, other numbers doubles.
{
  const fields = (bytes) => {
    const out = []
    let i = 0
    const varint = () => {
      let value = 0,
        scale = 1,
        byte
      do {
        byte = bytes[i++]
        value += (byte & 0x7f) * scale
        scale *= 128
      } while (byte & 0x80)
      return value
    }
    while (i < bytes.length) {
      const tag = varint()
      const field = Math.floor(tag / 8),
        wire = tag % 8
      const view = new DataView(bytes.buffer, bytes.byteOffset + i)
      if (wire === 0) out.push([field, varint()])
      else if (wire === 1) (out.push([field, view.getFloat64(0, true)]), (i += 8))
      else if (wire === 5) (out.push([field, view.getFloat32(0, true)]), (i += 4))
      else {
        const length = varint()
        out.push([field, bytes.subarray(i, i + length)])
        i += length
      }
    }
    return out
  }
  const values = [
    Math.fround(0.2863907),
    0.1,
    3,
    -3,
    12n,
    true,
    'x',
    Math.fround(0.2863907),
    null,
    2n ** 52n + 1n,
    2 ** 52 + 1,
    2n ** 60n,
  ]
  const geometry = columnar(
    'Point',
    values.map((_, i) => [i, i]),
  )
  const tile = new Uint8Array(
    toMvt({ geometry, columns: { value: values } }, { z: 0, x: 0, y: 0, layer: 'typed' }),
  )
  const [[, layerBytes]] = fields(tile)
  const layer = fields(layerBytes)
  const decoded = layer
    .filter(([f]) => f === 4)
    .map(([, b]) => fields(b)[0])
    .map(([f, v]) =>
      f === 1
        ? new TextDecoder().decode(v)
        : f === 6
          ? v % 2
            ? -(v + 1) / 2
            : v / 2
          : f === 7
            ? v === 1
            : v,
    )
  const types = layer.filter(([f]) => f === 4).map(([, b]) => fields(b)[0][0])
  // 2^52 + 1 as a bigint and as a number share one double value; past safe
  // integers a bigint stays its exact string
  assert.deepEqual(types, [2, 3, 6, 6, 6, 7, 1, 1, 3, 1])
  assert.deepEqual(decoded, [
    Math.fround(0.2863907),
    0.1,
    3,
    -3,
    12,
    true,
    'x',
    '',
    2 ** 52 + 1,
    String(2n ** 60n),
  ])
}
console.log('MVT properties keep number and boolean types')

// Exact-geometry fallback uses the same encoder as packed overviews. Exercise
// float64 WKB, single/multi families, holes, empty/null geometry and boundaries.
const { geometryColumnFromWkb } = await import('@cogp/reader')
const { renderTile } = await import('../dist/tile.js')
function wkb(type, coordinates) {
  const bytes = [1]
  const uint = (value) => {
    const buffer = new ArrayBuffer(4)
    new DataView(buffer).setUint32(0, value, true)
    bytes.push(...new Uint8Array(buffer))
  }
  const point = (xy) => {
    for (const value of xy) {
      const buffer = new ArrayBuffer(8)
      new DataView(buffer).setFloat64(0, value, true)
      bytes.push(...new Uint8Array(buffer))
    }
  }
  uint(type)
  if (type === 1) point(coordinates)
  else {
    uint(coordinates.length)
    for (const part of coordinates) {
      if (type === 2) point(part)
      else if (type === 3) {
        uint(part.length)
        for (const xy of part) point(xy)
      } else bytes.push(...wkb(type - 3, part))
    }
  }
  return Uint8Array.from(bytes)
}
const ring = [
  [-0.123456789, -0.15],
  [0.18, -0.15],
  [0.18, 0.21],
  [-0.123456789, 0.21],
  [-0.123456789, -0.15],
]
const hole = [
  [0.01, 0.01],
  [0.01, 0.02],
  [0.02, 0.02],
  [0.02, 0.01],
  [0.01, 0.01],
]
const exact = [
  wkb(1, [0.0123456789, -0.0123456789]),
  wkb(2, ring),
  wkb(3, [ring, hole]),
  wkb(4, [
    [0.0123456789, -0.0123456789],
    [179.99999, 1],
  ]),
  wkb(5, [
    ring,
    [
      [-180.00001, 1],
      [-179.99999, 2],
    ],
  ]),
  wkb(6, [[ring, hole], [ring.map(([x, y]) => [x + 180, y])]]),
  wkb(3, []),
  null,
]
const exactRows = exact.map((_, i) => 2 ** 40 + i)
const columns = { name: exact.map((_, i) => `feature-${i}`) }
const geometry = geometryColumnFromWkb(exact)
const reader = {
  lod: { attributes: [{ name: 'name' }] },
  finestZoom: 0,
  lodForZoom: () => ({ geometry_type: 'MultiPolygon' }),
  readPrimary: async () => ({ rows: exactRows, wkb: exact, columns }),
}
for (const [z, x, y] of [
  [1, 0, 0],
  [1, 1, 1],
  [14, 8191, 8191],
  [14, 8192, 8192],
  [14, 16383, 8191],
]) {
  const options = { z, x, y, layer: 'features' }
  assert.deepEqual(
    new Uint8Array(await renderTile(reader, z, x, y)),
    new Uint8Array(reference({ geometry, rowIndex: exactRows, columns }, options)),
    `exact WKB fallback preserves reference MVT at ${z}/${x}/${y}`,
  )
}
await assert.rejects(() => renderTile(reader, 1, 0, 0, { signal: AbortSignal.abort() }), {
  name: 'AbortError',
})
console.log('exact WKB fallback preserves reference MVT across geometry families')
