// A row read on an indexed file fetches the footer in one request and a row
// group's offset indexes for every requested column in one more.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { coalescing, openReader } from '../dist/index.js'

const [path, casesPath] = process.argv.slice(2)
if (!casesPath) throw new Error('row-read.test.mjs needs a file and its cases')
const data = await readFile(path)
const { row, columns, expected, indexes } = JSON.parse(await readFile(casesPath, 'utf8'))
const reads = []
const reader = await openReader(
  coalescing(
    {
      byteLength: data.length,
      async slice(a, b) {
        reads.push([a, b])
        return Uint8Array.from(data.subarray(a, b)).buffer
      },
    },
    { windowMs: 1 },
  ),
)
reads.length = 0

const values = await reader.readRow(row, columns)
assert.deepEqual(values, expected)

const footerStart = data.length - 8 - data.readUInt32LE(data.length - 8)
const tail = reads.filter(([, b]) => b > footerStart)
assert.equal(tail.length, 1, 'the footer and its trailer come in one request')
const [first, last] = indexes
const index = reads.filter(([a, b]) => a < last && b > first)
assert.equal(index.length, 1, 'offset indexes for every requested column come in one request')
console.log('row read:', JSON.stringify({ requests: reads.length }))
