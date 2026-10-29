import assert from 'node:assert/strict'
import { VisibleTileCache, decodedTileBytes, waitForTile } from '../dist/visible-cache.js'
import { renderDecodedTile } from '../dist/tile.js'
import { columnar } from './columnar.mjs'
const tile = {
  lod: { geometry_type: 'Point', scale: [1, 1], offset: [0, 0] },
  z: 0,
  x: 0,
  y: 0,
  rows: [7],
  columns: { height: [3] },
  geometry: columnar('Point', [[0, 0]]),
}
const size = decodedTileBytes(tile)
assert.ok(
  size >= tile.geometry.x.buffer.byteLength + tile.geometry.y.buffer.byteLength,
  'counts full backing stores',
)
const cache = new VisibleTileCache(size * 2, 2)
let loads = 0,
  resolve
const pending = cache.getOrLoad('a', () => {
  loads++
  return new Promise((r) => (resolve = r))
})
const second = cache.getOrLoad('a', () => {
  throw new Error('duplicate decode')
})
await Promise.resolve()
assert.equal(loads, 1)
const controller = new AbortController(),
  cancelled = waitForTile(pending, controller.signal)
const rejection = assert.rejects(cancelled, { name: 'AbortError' })
controller.abort()
await rejection
resolve(tile)
assert.equal(await second, tile)
assert.equal(cache.bytes, size)
await cache.getOrLoad('b', async () => tile)
await cache.getOrLoad('a', async () => {
  throw new Error('cache hit')
})
await cache.getOrLoad('c', async () => tile)
assert.equal(cache.size, 2)
assert.equal(cache.bytes, size * 2)
await cache.getOrLoad('b', async () => {
  loads++
  return tile
})
assert.equal(loads, 2, 'least recently used tile evicted')
const tiny = new VisibleTileCache(size - 1)
await tiny.getOrLoad('huge', async () => tile)
assert.equal(tiny.size, 0)
assert.equal(tiny.bytes, 0)
await assert.rejects(
  cache.getOrLoad('bad', async () => {
    throw new Error('decode failed')
  }),
  /decode failed/,
)
await cache.getOrLoad('bad', async () => tile)
const before = structuredClone(tile)
const a = renderDecodedTile(tile, { properties: [] })
renderDecodedTile(tile, { properties: ['height'], layer: 'custom' })
assert.deepEqual(renderDecodedTile(tile, { properties: [] }), a)
assert.deepEqual(tile, before, 'style changes never mutate cached geometry or columns')
console.log(
  'visible cache sharing, independent cancellation, LRU limits, failure retry and read-only rendering passed',
)

const growing = new VisibleTileCache(size + 100)
const growth = structuredClone(tile)
await growing.getOrLoad('grow', async () => growth)
growing.addColumns('grow', growth, { extra: ['x'.repeat(1024)] })
assert.equal(growing.bytes, 0)
assert.equal(growing.size, 0, 'hydration growth obeys the cache budget')

// Reacquire an abandoned key before the old transport finishes. Its late
// rejection must not evict the replacement or corrupt byte accounting.
const retryCache = new VisibleTileCache(size * 2)
const departing = new AbortController()
let finishOld
const old = retryCache.getOrLoad(
  'retry',
  () =>
    new Promise((r) => {
      finishOld = r
    }),
  departing.signal,
)
const oldRejected = assert.rejects(old, { name: 'AbortError' })
await Promise.resolve()
departing.abort()
await oldRejected
assert.equal(await retryCache.getOrLoad('retry', async () => tile), tile)
finishOld(tile)
await new Promise(setImmediate)
assert.equal(retryCache.bytes, size)
assert.equal(await retryCache.getOrLoad('retry', () => assert.fail('replacement evicted')), tile)
