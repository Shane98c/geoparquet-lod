import assert from 'node:assert/strict'
import { decodeAttributes, decodePoint, decodePolygonTree, PolygonBuilder } from '../dist/index.js'

// attribute block: f32 risk, i32 floors, utf8 name; then a point
const buf = new Uint8Array([0, 0, 0, 63, 2, 0, 0, 0, 2, 104, 105, 0, 0, 128, 63, 0, 0, 0, 64])
const { values, pos } = decodeAttributes(buf, [{ name: 'risk', type: 'f32' }, { name: 'floors', type: 'i32' }, { name: 'name', type: 'utf8' }])
assert.equal(values.risk, 0.5)
assert.equal(values.floors, 2)
assert.equal(values.name, 'hi')
assert.deepEqual(decodePoint(buf, pos), [1, 2])

// polygon tree: 1 polygon, 1 ring, 4 vertices: (0,0) (3,0) (3,3) (0,0) as zigzag deltas
const tree = new Uint8Array([1, 1, 4, 0, 0, 6, 0, 0, 6, 5, 5])
const polygons = decodePolygonTree(tree, 0, [1, 1], [0, 0])
assert.deepEqual(polygons, [[[[0, 0], [3, 0], [3, 3], [0, 0]]]])
const b = new PolygonBuilder()
b.add(tree, 0)
const g = b.finish([1, 1], [0, 0])
assert.equal(g.length, 1)
assert.deepEqual(Array.from(g.x), [0, 3, 3, 0])

assert.throws(() => decodePolygonTree(tree.subarray(0, 6), 0, [1, 1], [0, 0]), /truncated|more vertices/)
assert.throws(() => decodeAttributes(new Uint8Array(2), [{ name: 'a', type: 'f32' }]), /truncated/)
console.log('decode tests passed')
