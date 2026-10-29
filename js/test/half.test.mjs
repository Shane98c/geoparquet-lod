// Rounding to half precision, as exact tiles round a carried float16.
//   node test/half.test.mjs [values.f64 rounded.f64]
// Given two files of little-endian float64s, the values and numpy's float16
// of each, every value must round to numpy's.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { asCarried, toHalf } from '../dist/decode.js'

const same = (a, b) => Object.is(a, b)
for (const [value, half] of [
  [0.01, 0.01000213623046875],
  [1 + 2 ** -11, 1],
  [1 + 3 * 2 ** -11, 1 + 2 ** -9],
  [65519, 65504],
  [65520, Infinity],
  [-65520, -Infinity],
  [2 ** -24, 2 ** -24],
  [2 ** -25, 0],
  [3 * 2 ** -25, 2 ** -23],
  [-(2 ** -26), -0],
  [-0, -0],
  [Infinity, Infinity],
  [NaN, NaN],
])
  assert.ok(same(toHalf(value), half), `${value} rounds to ${half}, not ${toHalf(value)}`)
assert.equal(asCarried(0.01, 'float16'), 0.01000213623046875)
assert.equal(asCarried(0.1, 'float32'), Math.fround(0.1))
assert.equal(asCarried(7n, 'float64'), 7)
assert.equal(asCarried(null, 'float16'), null)
assert.equal(asCarried(7n, 'int'), 7n)
assert.equal(asCarried('a', 'string'), 'a')

const [valuesPath, roundedPath] = process.argv.slice(2)
if (valuesPath) {
  const floats = async (path) => new Float64Array(Uint8Array.from(await readFile(path)).buffer)
  const [values, rounded] = await Promise.all([floats(valuesPath), floats(roundedPath)])
  assert.equal(values.length, rounded.length)
  const wrong = []
  for (let i = 0; i < values.length; i++)
    if (!same(toHalf(values[i]), rounded[i])) wrong.push([values[i], toHalf(values[i]), rounded[i]])
  assert.deepEqual(wrong.slice(0, 10), [], `${wrong.length} values round otherwise than numpy`)
  console.log(`half precision: ${values.length} values round as numpy`)
} else console.log('half precision: ties to even, subnormals, overflow, NaN and carried kinds')
