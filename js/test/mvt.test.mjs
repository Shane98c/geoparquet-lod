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
