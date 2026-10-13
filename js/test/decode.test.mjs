import assert from 'node:assert/strict'
import { GeometryBuilder, decodeRow } from '../dist/index.js'

// a MultiPolygon row as the decoder returns it: polygons > rings > [x, y] in grid units
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
const b = new GeometryBuilder()
b.add('MultiPolygon', mp)
b.add('MultiLineString', [
  [
    [0, 0],
    [1, 1],
  ],
])
b.add('Point', [7, 8])
const g = b.finish([1, 1], [0, 0])
assert.equal(g.length, 3)
assert.deepEqual(Array.from(g.types), [6, 5, 1])
assert.deepEqual(Array.from(g.geometryOffsets), [0, 2, 3, 4])
assert.deepEqual(Array.from(g.polygonOffsets), [0, 1, 2, 3, 4])
assert.deepEqual(Array.from(g.ringOffsets), [0, 4, 8, 10, 11])
assert.deepEqual(Array.from(g.x), [0, 3, 3, 0, 10, 12, 10, 10, 0, 1, 7])
assert.deepEqual(Array.from(g.y), [0, 0, 3, 0, 10, 10, 12, 10, 0, 1, 8])

assert.throws(() => b.add('MultiPolygon', [[[[0, 1, 2]]]]), /pair/)

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
const ringBytes = (pts) => {
  const out = varint(pts.length)
  let x = 0
  let y = 0
  for (const [px, py] of pts) {
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
const row = decodeRow(bytes, 'MultiPolygon', [
  { name: 'h', kind: 'int' },
  { name: 'name', kind: 'string' },
])
assert.deepEqual(row.geometry, [[square(5, 5, 2)]])
assert.deepEqual(row.attrs, [3, 'ab'])
assert.equal(row.spill.length, 1)
assert.equal(row.spill[0].tile, 12 * 2 ** 48 + 7)
assert.equal(row.spill[0].id, 9)
assert.deepEqual(row.spill[0].geometry, [[square(1, 1, 1)]])
assert.deepEqual(row.spill[0].attrs, [4, null])
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
// integer precision. Both the row and direct geometry decoders use this parser.
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
    Object.is(decodeRow(row, 'Point', [{ name: 'value', kind: 'float64' }]).attrs[0], value),
  )
}
