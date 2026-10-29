import assert from 'node:assert/strict'
import { GeometryBuilder } from '../dist/index.js'
import { decodeAttributes, decodeSelectedRow } from '../dist/decode.js'
import { columnar, nested } from './columnar.mjs'

// a MultiPolygon, a MultiLineString and a Point in grid units, copied feature by feature
const mp = [
  [
    [
      [0, 0],
      [3, 0],
      [3, 3],
      [0, 0],
    ],
  ],
  [
    [
      [10, 10],
      [12, 10],
      [10, 12],
      [10, 10],
    ],
  ],
]
const line = [
  [
    [0, 0],
    [1, 1],
  ],
]
const sources = [columnar('MultiPolygon', [mp]), columnar('MultiLineString', [line])]
const b = new GeometryBuilder()
b.append(sources[0], 0)
b.append(sources[1], 0)
b.append(columnar('Point', [[7, 8]]), 0)
const g = b.finish([1, 1], [0, 0])
assert.equal(g.length, 3)
assert.deepEqual(Array.from(g.types), [6, 5, 1])
assert.deepEqual(Array.from(g.geometryOffsets), [0, 2, 3, 4])
assert.deepEqual(Array.from(g.polygonOffsets), [0, 1, 2, 3, 4])
assert.deepEqual(Array.from(g.ringOffsets), [0, 4, 8, 10, 11])
assert.deepEqual(Array.from(g.x), [0, 3, 3, 0, 10, 12, 10, 10, 0, 1, 7])
assert.deepEqual(Array.from(g.y), [0, 0, 3, 0, 10, 10, 12, 10, 0, 1, 8])
assert.deepEqual(nested(g, 0, 'MultiPolygon'), mp)
assert.deepEqual(nested(g, 1, 'MultiLineString'), line)
assert.deepEqual(nested(g, 2, 'Point'), [7, 8])

// a packed row: one square at (5, 5) of side 2, attributes h=3 (int), name='ab' (string),
// hosting one copy for tile zoom 12 / 7 of row 9: a unit square at (1, 1), h=4, name=null
const zz = (v) => (v < 0 ? -2 * v - 1 : 2 * v)
const varint = (v) => {
  const out = []
  while (v >= 128) {
    out.push((v % 128) + 128)
    v = Math.floor(v / 128)
  }
  out.push(v)
  return out
}
// a polygon ring is stored without its closing vertex, which repeats the first
const ringBytes = (pts) => {
  const out = varint(pts.length - 1)
  let x = 0
  let y = 0
  for (const [px, py] of pts.slice(0, -1)) {
    out.push(...varint(zz(px - x)), ...varint(zz(py - y)))
    x = px
    y = py
  }
  return out
}
const square = (x, y, s) => [
  [x, y],
  [x + s, y],
  [x + s, y + s],
  [x, y + s],
  [x, y],
]
const bytes = Uint8Array.from([
  ...varint(1),
  ...varint(1),
  ...ringBytes(square(5, 5, 2)), // own geometry: one polygon, one ring
  ...varint(0b11),
  ...varint(zz(3)),
  ...varint(2), // attrs: both present, h=3, name length 2
  ...varint(1), // one copy
  ...varint(12 * 2 ** 48 + 7),
  ...varint(9),
  ...varint(1),
  ...varint(1),
  ...ringBytes(square(1, 1, 1)),
  ...varint(0b01),
  ...varint(zz(4)), // copy attrs: only h present
  ...new TextEncoder().encode('ab'),
])
const fields = [
  { name: 'h', kind: 'int' },
  { name: 'name', kind: 'string' },
]
const decode = (own, tile, withAttributes = true) => {
  const builder = new GeometryBuilder()
  const features = []
  const add = (id, attrs, slot) => features.push({ id, attrs, slot })
  decodeSelectedRow(bytes, 'MultiPolygon', fields, builder, own, 4, tile, add, withAttributes)
  const geometry = builder.finish([1, 1], [0, 0])
  return features.map((f, i) => ({ ...f, geometry: nested(geometry, i, 'MultiPolygon') }))
}
const copyTile = 12 * 2 ** 48 + 7
assert.deepEqual(decode(true, copyTile), [
  { id: 4, attrs: [3, 'ab'], slot: 0, geometry: [[square(5, 5, 2)]] },
  { id: 9, attrs: [4, null], slot: 1, geometry: [[square(1, 1, 1)]] },
])
assert.deepEqual(decode(true, copyTile + 1), [
  { id: 4, attrs: [3, 'ab'], slot: 0, geometry: [[square(5, 5, 2)]] },
])
assert.deepEqual(decode(false, copyTile), [
  { id: 9, attrs: [4, null], slot: 1, geometry: [[square(1, 1, 1)]] },
])
assert.deepEqual(decode(false, copyTile + 1), [])
assert.deepEqual(
  decode(true, copyTile, false).map((f) => [f.id, f.attrs, f.geometry]),
  [
    [4, [], [[square(5, 5, 2)]]],
    [9, [], [[square(1, 1, 1)]]],
  ],
  'geometry without attributes',
)
assert.deepEqual(decodeAttributes(bytes, 'MultiPolygon', fields), [3, 'ab'])
// a hosted copy's strings trail the row's own
const named = Uint8Array.from([
  ...bytes.subarray(0, bytes.length - 2 - 2),
  ...varint(0b11),
  ...varint(zz(4)),
  ...varint(3),
  ...new TextEncoder().encode('abxyz'),
])
assert.deepEqual(decodeAttributes(named, 'MultiPolygon', fields), [3, 'ab'])
const copy = new GeometryBuilder()
const copies = []
decodeSelectedRow(named, 'MultiPolygon', fields, copy, false, 4, copyTile, (...f) => copies.push(f))
assert.deepEqual(copies, [[9, [4, 'xyz'], 1]])
console.log('decode tests passed')

// the stored key: a tile is one interval, its parent the union of its four children
import { hilbertIndex, tileInterval, cellInterval } from '../dist/index.js'
assert.deepEqual(
  [0, 1, 2, 3].map((i) => hilbertIndex(1, i & 1, i >> 1)),
  [0, 3, 1, 2],
)
const child = (x, y) => cellInterval(16, 12, x, y)
const parent = cellInterval(16, 11, 7, 5)
const kids = [child(14, 10), child(15, 10), child(14, 11), child(15, 11)].sort(
  (a, b) => a[0] - b[0],
)
assert.deepEqual([kids[0][0], kids[3][1]], parent)
assert.ok(
  kids.every((k, i) => i === 0 || k[0] === kids[i - 1][1]),
  'children are contiguous',
)
assert.deepEqual(tileInterval(16, 11, 7, 5), parent)
assert.deepEqual(
  tileInterval(16, 18, 100, 100),
  cellInterval(16, 16, 25, 25),
  'beyond the depth, the depth cell',
)
console.log('key tests passed')

// Float64 packing stores raw bits, including low mantissa bits above Number’s
// integer precision.
for (const value of [
  0.1,
  -13.37,
  Number.MIN_VALUE,
  Number.MAX_VALUE,
  -0,
  Infinity,
  -Infinity,
  NaN,
]) {
  const view = new DataView(new ArrayBuffer(8))
  view.setFloat64(0, value, true)
  let bits = view.getBigUint64(0, true)
  const encoded = []
  do {
    const byte = Number(bits & 127n)
    bits >>= 7n
    encoded.push(byte | (bits ? 128 : 0))
  } while (bits)
  const row = Uint8Array.from([0, 0, 1, ...encoded, 0]) // point(0,0), one attr, no copies
  assert.ok(
    Object.is(decodeAttributes(row, 'Point', [{ name: 'value', kind: 'float64' }])[0], value),
  )
}
