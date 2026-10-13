import assert from 'node:assert/strict'
import { visitPlainPage } from '../dist/pages.js'
const varint = (v) => {
  const bytes = []
  while (v >= 128) {
    bytes.push((v % 128) + 128)
    v = Math.floor(v / 128)
  }
  return [...bytes, v]
}
const i32 = (value) => {
  const b = new Uint8Array(4)
  new DataView(b.buffer).setUint32(0, value, true)
  return [...b]
}
const page = (rows, levels, values, type = 0) => {
  const payload = [
    ...i32(levels.length),
    ...levels,
    ...values.flatMap((v) => [...i32(v.length), ...v]),
  ]
  // Compact Thrift PageHeader + DataPageHeader, plain/uncompressed binary.
  const header = [
    0x15,
    type * 2,
    0x15,
    ...varint(payload.length * 2),
    0x15,
    ...varint(payload.length * 2),
    0x2c,
    0x15,
    ...varint(rows * 2),
    0x15,
    0,
    0x15,
    6,
    0x15,
    6,
    0,
    0,
  ]
  return Uint8Array.from([...header, ...payload]).buffer
}
for (const [rows, levels, indices] of [
  [19, [...varint(38), 1], Array.from({ length: 19 }, (_, i) => i)],
  [1000000, [...varint(2000000), 0], []],
  [1000001, [...varint(2000000), 0, 2, 1], [1000000]],
  [13, [5, 0b01010010, 0b00001110], [1, 4, 6, 9, 10, 11]],
  [17, [16, 0, 3, 0b10101010, 2, 1], [9, 11, 13, 15, 16]],
]) {
  const values = indices.map((_, i) => [i, 0, 255])
  const bytes = page(rows, levels, values),
    found = []
  assert.equal(
    visitPlainPage(bytes, 'UNCOMPRESSED', rows, (row, value) => found.push([row, [...value]])),
    true,
  )
  assert.deepEqual(
    found,
    indices.map((row, i) => [row, values[i]]),
  )
  assert.throws(() => visitPlainPage(bytes, 'UNCOMPRESSED', rows + 1, () => {}), /row count/)
  assert.throws(
    () => visitPlainPage(bytes.slice(0, -1), 'UNCOMPRESSED', rows, () => {}),
    /page length/,
  )
}
assert.equal(
  visitPlainPage(page(1, [2, 1], [[42]], 3), 'UNCOMPRESSED', 1, () => assert.fail()),
  false,
  'v2 falls back',
)
assert.throws(
  () => visitPlainPage(page(1, [0], []), 'UNCOMPRESSED', 1, () => {}),
  /empty definition run/,
)
console.log(
  'sparse pages: long null runs, mixed bitpacking, partial bytes, corruption and fallback',
)
