import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { once } from 'node:events'
import { Worker } from 'node:worker_threads'
import { VectorTile } from '@mapbox/vector-tile'
import { PbfReader } from 'pbf'
import {
  GeometryBuilder,
  LodReader,
  SpatialReader,
  openReader,
  coalescing,
  renderTile,
} from '../dist/index.js'

const [oldPath, newPath, casesPath] = process.argv.slice(2)
if (!oldPath) throw new Error('spatial.test.mjs needs source, optimized file, and tile cases')
const makeSource = async (path) => {
  const data = await readFile(path)
  const reads = []
  return {
    reads,
    byteLength: data.length,
    async slice(a, b) {
      reads.push([a, b])
      return Uint8Array.from(data.subarray(a, b)).buffer
    },
  }
}
const original = await makeSource(oldPath)
const optimized = await makeSource(newPath)
const old = await LodReader.open(coalescing(original))
const fast = await openReader(coalescing(optimized))
assert.ok(fast instanceof SpatialReader)
assert.deepEqual(fast.bounds, old.bounds)
assert.deepEqual(fast.scalarColumns(), old.scalarColumns())
assert.deepEqual(optimized.reads, [[0, 16384]], 'one small open, no footer')
const nested = new Proxy(fast, {
  get(target, key) {
    if (key === 'readGeometryByTile') return undefined
    const value = Reflect.get(target, key)
    return typeof value === 'function' ? value.bind(target) : value
  },
})
const legacy = await LodReader.open(coalescing(await makeSource(newPath)))
const canonical = (buffer) => {
  const tile = new VectorTile(new PbfReader(new Uint8Array(buffer)))
  const features = []
  for (const layer of Object.values(tile.layers)) {
    for (let i = 0; i < layer.length; i++) {
      const f = layer.feature(i)
      features.push(JSON.stringify([f.id, f.type, f.properties, f.loadGeometry()]))
    }
  }
  return [...new Set(features)].sort()
}
const { cases, expected } = JSON.parse(await readFile(casesPath, 'utf8'))
for (const [i, [z, x, y]] of cases.entries()) {
  const [a, b, c] = await Promise.all([old, fast, legacy].map((r) => renderTile(r, z, x, y)))
  assert.deepEqual(canonical(c), canonical(a), `standard reader of relocated pages ${z}/${x}/${y}`)
  assert.deepEqual(
    new Uint8Array(b),
    new Uint8Array(await renderTile(nested, z, x, y)),
    `direct decoding preserves MVT bytes ${z}/${x}/${y}`,
  )
  if (expected[i]) {
    const attrs = fast.lod.attributes.map((a) => a.name)
    const rows = await fast.readLodByTile(fast.lodForZoom(z), z, x, y, attrs)
    const direct = await fast.readGeometryByTile(fast.lodForZoom(z), z, x, y, attrs)
    const builder = new GeometryBuilder()
    for (const coords of rows.coords) builder.add(fast.lodForZoom(z).geometry_type, coords)
    const lod = fast.lodForZoom(z)
    assert.deepEqual(direct.rows, rows.rows)
    assert.deepEqual(direct.columns, rows.columns)
    assert.deepEqual(direct.geometry, builder.finish(lod.scale, lod.offset))
    const unstyled = await fast.readGeometryByTile(lod, z, x, y, [])
    assert.deepEqual(unstyled.rows, direct.rows)
    assert.deepEqual(unstyled.geometry, direct.geometry)
    const actual = rows.rows.map((id, i) => [
      id,
      rows.coords[i],
      Object.fromEntries(attrs.map((a) => [a, rows.columns[a][i]])),
    ])
    assert.deepEqual(
      actual.sort((a, b) => a[0] - b[0]),
      expected[i].sort((a, b) => a[0] - b[0]),
      `exact directory membership ${z}/${x}/${y}`,
    )
  } else {
    assert.deepEqual(canonical(b), canonical(a), `exact geometry fallback ${z}/${x}/${y}`)
  }
}
for (const row of [0, 100, 1199]) {
  assert.deepEqual(
    await fast.readRow(row, ['name', 'height', 'geometry']),
    await old.readRow(row, ['name', 'height', 'geometry']),
  )
}
const freshSource = await makeSource(newPath)
const cached = await openReader(coalescing(freshSource))
const [z, x, y] = cases[8]
await renderTile(cached, z, x, y)
const after = freshSource.reads.length
await renderTile(cached, z, x, y)
assert.equal(freshSource.reads.length, after, 'repeat tile uses compressed page cache')
const aborted = new AbortController()
aborted.abort()
await assert.rejects(renderTile(cached, z, x, y, { signal: aborted.signal }), {
  name: 'AbortError',
})
console.log(`spatial tests passed: ${cases.length} tiles, analytical rows, legacy reads and cache`)

const worker = new Worker(new URL('./worker-harness.mjs', import.meta.url), {
  workerData: { path: newPath },
})
try {
  assert.deepEqual((await once(worker, 'message'))[0], { ready: true })
  let nextId = 0
  const call = async (message) => {
    const reply = once(worker, 'message')
    worker.postMessage({ id: ++nextId, ...message })
    const [response] = await reply
    assert.equal(response.id, nextId)
    assert.equal(response.ok, true, response.error)
    return response
  }
  const url = 'https://example.test/features.parquet'
  const opened = await call({ type: 'open', url })
  assert.deepEqual(opened.metadata.bounds, old.bounds)
  assert.deepEqual(opened.metadata.columns, old.scalarColumns())
  const stats = await call({ type: 'stats' })
  assert.deepEqual(
    stats.stats[url],
    { bytes: 16384, requests: 1 },
    'worker and map metadata share one bootstrap',
  )
  const tile = await call({ type: 'tile', config: { url }, z, x, y })
  assert.deepEqual(canonical(tile.data), canonical(await renderTile(fast, z, x, y)))
  const row = await call({ type: 'row', url, rowIndex: 100, columns: ['name', 'height'] })
  assert.deepEqual(row.row, await old.readRow(100, ['name', 'height']))
} finally {
  await worker.terminate()
}
console.log('worker metadata, rendering and exact-row integration passed')
