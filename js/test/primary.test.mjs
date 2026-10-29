// Exact rows a file's primary directory finds for bboxes, without its footer.
//   node test/primary.test.mjs file.parquet cases.json
// cases.json: [{ "bbox": [w, s, e, n], "rows": [...] }, ...]
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { SpatialReader, coalescing, openReader } from '../dist/index.js'

const [path, casesPath] = process.argv.slice(2)
if (!path) throw new Error('primary.test.mjs needs a laid-out file and cases')
const data = await readFile(path)
const footer = data.length - 8 - data.readUInt32LE(data.length - 8)
const reads = []
const reader = await openReader(
  coalescing({
    byteLength: data.length,
    async slice(a, b) {
      reads.push([a, b])
      return Uint8Array.from(data.subarray(a, b)).buffer
    },
  }),
)
assert.ok(reader instanceof SpatialReader)
const cases = JSON.parse(await readFile(casesPath, 'utf8'))
for (const { bbox, rows } of cases)
  assert.deepEqual((await reader.readPrimary(bbox)).rows, rows, `rows in ${bbox}`)
assert.ok(
  reads.every(([, b]) => b <= footer),
  'exact rows come through the primary directory, not the footer',
)
console.log(`primary directory: ${cases.length} bboxes, ${reads.length} reads`)
