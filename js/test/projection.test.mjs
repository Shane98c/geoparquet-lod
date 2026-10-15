import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { LodReader } from '../dist/index.js'
import { decodeProjection } from '../dist/projection.js'
const bytes = await readFile(process.argv[2]),
  expected = JSON.parse(await readFile(process.argv[3], 'utf8'))
let reads = 0
const reader = await LodReader.open({
  byteLength: bytes.length,
  async slice(a, b = bytes.length) {
    reads++
    return Uint8Array.from(bytes.subarray(a, b)).buffer
  },
})
const rows = [239, 0, 66, 15, 128, 66, 212, ...Array.from({ length: 240 }, (_, i) => i)]
const columns = Object.keys(expected[0])
const got = await reader.readColumns(rows, columns)
const normalize = (v) =>
  typeof v === 'bigint'
    ? Number(v)
    : Array.isArray(v)
      ? v.map(normalize)
      : v && typeof v === 'object'
        ? Object.fromEntries(Object.entries(v).map(([k, x]) => [k, normalize(x)]))
        : v
for (const c of columns)
  assert.deepEqual(
    got[c].map(normalize),
    rows.map((i) => expected[i][c]),
    c,
  )
const scalarReader = await LodReader.open({
  byteLength: bytes.length,
  async slice(a, b = bytes.length) {
    return Uint8Array.from(bytes.subarray(a, b)).buffer
  },
})
const leaves = []
await scalarReader.readColumns([0, 50, 239], ['details.height'], undefined, async (task) => {
  leaves.push(task.decoder.pathInSchema.join('.'))
  return decodeProjection(task)
})
assert.ok(leaves.length > 1)
assert.ok(
  leaves.every((x) => x === 'details.height'),
  'only requested struct leaf is decoded',
)
assert.deepEqual(await reader.readColumns([], ['details.height', 'nums[99]']), {
  'details.height': [],
  'nums[99]': [],
})
for (const column of ['details.missing', 'nums[-1]', 'nums[1.2]', 'nums.name'])
  await assert.rejects(reader.readColumns([0], [column]), /unknown|invalid|needs an index/)
const before = reads
await assert.rejects(reader.readColumns([240], ['id']), /out of range/)
assert.equal(reads, before)
console.log(
  'logical projection: nullable structs, indexed lists, maps, duplicates, sparse groups, leaf pruning, invalid paths',
)
