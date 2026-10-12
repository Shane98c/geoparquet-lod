import assert from 'node:assert/strict'
import { GeometryBuilder, decodeCoordinates } from '../dist/index.js'

// rings as stored: each pair is the zigzag-coded step from the previous vertex of its ring
const zigzag = (v) => (v << 1) ^ (v >> 31)
const stored = (ring) => {
  let x = 0
  let y = 0
  return ring.map(([px, py]) => {
    const d = [zigzag(px - x), zigzag(py - y)]
    x = px
    y = py
    return d
  })
}
// a MultiPolygon row as hyparquet returns the interleaved coordinate leaf: polygons > rings > [x, y]
const mp = [
  [
    stored([
      [0, 0],
      [3, 0],
      [3, 3],
      [0, 0],
    ]),
  ],
  [
    stored([
      [10, 10],
      [12, 10],
      [10, 12],
      [10, 10],
    ]),
  ],
]
assert.deepEqual(mp[0][0][3], [5, 5], 'a step back of three is zigzag 5')
assert.deepEqual(decodeCoordinates(mp, [0.5, 0.5], [100, 200])[0][0][1], [101.5, 200])
assert.deepEqual(decodeCoordinates(mp, [0.5, 0.5], [100, 200])[1][0][2], [105, 206])

const b = new GeometryBuilder()
b.add('MultiPolygon', mp)
b.add(
  'MultiLineString',
  [
    [
      [0, 0],
      [1, 1],
    ],
  ].map(stored),
)
b.add('Point', stored([[7, 8]])[0])
const g = b.finish([1, 1], [0, 0])
assert.equal(g.length, 3)
assert.deepEqual(Array.from(g.types), [6, 5, 1])
assert.deepEqual(Array.from(g.geometryOffsets), [0, 2, 3, 4])
assert.deepEqual(Array.from(g.polygonOffsets), [0, 1, 2, 3, 4])
assert.deepEqual(Array.from(g.ringOffsets), [0, 4, 8, 10, 11])
assert.deepEqual(Array.from(g.x), [0, 3, 3, 0, 10, 12, 10, 10, 0, 1, 7])
assert.deepEqual(Array.from(g.y), [0, 0, 3, 0, 10, 10, 12, 10, 0, 1, 8])

assert.throws(() => b.add('MultiPolygon', [[[[0, 1, 2]]]]), /pair/)
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
