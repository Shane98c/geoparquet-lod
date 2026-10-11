import assert from 'node:assert/strict'
import { GeometryBuilder, decodeCoordinates } from '../dist/index.js'

// a MultiPolygon row as hyparquet returns the interleaved coordinate leaf: polygons > rings > [x, y]
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
assert.deepEqual(decodeCoordinates(mp, [0.5, 0.5], [100, 200])[0][0][1], [101.5, 200])

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
console.log('decode tests passed')
