import assert from 'node:assert/strict'
import { gzipSync } from 'node:zlib'
import { httpSource, openReader } from '../dist/index.js'

const realFetch = globalThis.fetch
const file = new Uint8Array(128 * 1024)
file.set(new TextEncoder().encode('PAR1'))
file.set(new TextEncoder().encode('LODIDX01'), 4)
const calls = []
let exposeRange = true,
  truncate = false,
  status = 206,
  served = file.length,
  failTail = 0
globalThis.fetch = async (_url, options = {}) => {
  calls.push(options)
  if (options.method === 'HEAD')
    return new Response(null, { headers: { 'Content-Length': String(file.length) } })
  const [, a, b] = options.headers.Range.match(/bytes=(\d+)-(\d+)/)
  if (Number(a) >= served) return new Response(null, { status: 416 })
  if (failTail && Number(b) === file.length - 1 && Number(a) === file.length - 8) {
    failTail--
    return new Response(null, { status: 503 })
  }
  const end = Math.min(Number(b) + 1, served)
  return new Response(file.slice(Number(a), truncate ? end - 1 : end), {
    status,
    headers: exposeRange ? { 'Content-Range': `bytes ${a}-${end - 1}/${file.length}` } : {},
  })
}
try {
  const source = await httpSource(['https://example.test/data.parquet'])
  assert.equal(calls.length, 1, 'indexed startup is one GET')
  assert.equal((await source.slice(0, 16384)).byteLength, 16384)
  assert.equal(calls.length, 1, 'openReader reuses the bootstrap')
  await assert.rejects(
    openReader(source),
    /root length/,
    'bad directory is rejected, not silently used',
  )
  const controller = new AbortController()
  controller.abort()
  await assert.rejects(source.slice(0, 10, controller.signal), { name: 'AbortError' })
  await assert.rejects(source.slice(-1, 10), /invalid byte range/)
  truncate = true
  await assert.rejects(source.slice(20000, 20020), /truncated range/)
  truncate = false
  calls.length = 0
  exposeRange = false
  await httpSource(['https://example.test/data.parquet'])
  assert.equal(
    calls.length,
    2,
    'HEAD fallback when Content-Range is hidden and no root sizes the file',
  )
  const root = gzipSync(JSON.stringify({ version: 1, file_bytes: file.length }))
  new DataView(file.buffer).setUint32(12, root.length, true)
  file.set(root, 16)
  calls.length = 0
  file.set(new TextEncoder().encode('PAR1'), file.length - 4)
  const sized = await httpSource(['https://example.test/data.parquet'])
  assert.equal(calls.length, 1, 'the root sizes the file when Content-Range is hidden')
  assert.equal(sized.byteLength, file.length)
  assert.equal((await sized.slice(20000, 20010)).byteLength, 10)
  assert.equal(calls.length, 3, 'the first read past the bootstrap checks the footer magic')
  await sized.slice(30000, 30010)
  assert.equal(calls.length, 4, 'the footer magic is checked once')
  // a server error on the check is retried by the next read
  failTail = 1
  const retried = await httpSource(['https://example.test/data.parquet'])
  await assert.rejects(retried.slice(20000, 20010), /range request failed: 503/)
  assert.equal((await retried.slice(20000, 20010)).byteLength, 10)
  // an object shorter than its root claims fails on its first data read
  served = file.length / 2
  const truncated = await httpSource(['https://example.test/data.parquet'])
  await assert.rejects(truncated.slice(20000, 20010), /range request failed|does not end/)
  served = file.length
  file.fill(0, file.length - 4)
  file.fill(0, 12, 16 + root.length)
  exposeRange = true
  calls.length = 0
  file.fill(0, 4, 12)
  new DataView(file.buffer).setUint32(file.length - 8, 200, true)
  const unindexed = await httpSource(['https://example.test/data.parquet'])
  assert.equal(calls.length, 2, 'unindexed startup needs head and tail, no redundant margin read')
  await unindexed.slice(file.length - 208, file.length)
  assert.equal(calls.length, 2)
  status = 200
  await assert.rejects(httpSource(['https://example.test/data.parquet']), /range request failed/)
} finally {
  globalThis.fetch = realFetch
}
console.log('HTTP bootstrap and range tests passed')
