import assert from 'node:assert/strict'
import { toMvt as reference } from '@cogp/reader'
import { toMvt } from '../dist/mvt.js'
import { GeometryBuilder } from '../dist/geometry.js'

let seed = 71
const random = () => (seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0) / 2 ** 32
// Include paths crossing tile boundaries, holes, disjoint parts, collapsed
// rings, duplicate vertices, poles and antimeridian wrapping. Compare the
// complete protobuf, including ids, stringified attributes and empty tiles.
for (const type of ['Point', 'MultiLineString', 'MultiPolygon']) {
  for (const scale of [
    [1, 1],
    [-0.1, 0.1],
    [0.00001, 0.00001],
  ]) {
    const builder = new GeometryBuilder()
    const rows = [],
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
      builder.add(
        type,
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
    const geometry = builder.finish(scale, [0, 0])
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
      for (const columns of [{}, { value: values }]) {
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
