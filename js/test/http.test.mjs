import assert from 'node:assert/strict'
import { httpSource, openReader } from '../dist/index.js'

const realFetch = globalThis.fetch
const file = new Uint8Array(128 * 1024)
file.set(new TextEncoder().encode('PAR1'))
file.set(new TextEncoder().encode('LODIDX01'), 4)
const calls = []
let exposeRange = true,
  truncate = false,
  status = 206
globalThis.fetch = async (_url, options = {}) => {
  calls.push(options)
  if (options.method === 'HEAD')
    return new Response(null, { headers: { 'Content-Length': String(file.length) } })
  const [, a, b] = options.headers.Range.match(/bytes=(\d+)-(\d+)/)
  const end = Math.min(Number(b) + 1, file.length)
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
  assert.equal(calls.length, 2, 'HEAD fallback when Content-Range is hidden by CORS')
  exposeRange = true
  calls.length = 0
  file.fill(0, 4, 12)
  new DataView(file.buffer).setUint32(file.length - 8, 200, true)
  const legacy = await httpSource(['https://example.test/data.parquet'])
  assert.equal(calls.length, 2, 'legacy startup needs prefix and tail, no redundant margin read')
  await legacy.slice(file.length - 208, file.length)
  assert.equal(calls.length, 2)
  status = 200
  await assert.rejects(httpSource(['https://example.test/data.parquet']), /range request failed/)
} finally {
  globalThis.fetch = realFetch
}
console.log('HTTP bootstrap and range tests passed')
