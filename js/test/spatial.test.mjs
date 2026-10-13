import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { workerClient } from './worker-client.mjs'
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

for (const workers of [1, 2, 4]) {
  const client = await workerClient(newPath, workers)
  const { call } = client
  try {
    const url = 'https://example.test/features.parquet'
    const opened = await call({ type: 'open', url })
    assert.deepEqual(opened.metadata.bounds, old.bounds)
    assert.deepEqual(opened.metadata.columns, old.scalarColumns())
    assert.deepEqual(
      (await call({ type: 'stats' })).stats[url],
      { bytes: 16384, requests: 1 },
      'one bootstrap regardless of worker count',
    )
    const cancelled = call({ type: 'tile', config: { url }, z, x, y })
    const rejection = assert.rejects(cancelled, { name: 'AbortError' })
    client.cancel(cancelled.id)
    await rejection
    const concurrent = cases
      .slice(0, 16)
      .map(([z, x, y]) =>
        call({ type: 'tile', config: { url, properties: ['height', 'name'] }, z, x, y }),
      )
    const replies = await Promise.all(concurrent)
    for (const [i, reply] of replies.entries()) {
      const [z, x, y] = cases[i]
      assert.deepEqual(
        new Uint8Array(reply.data),
        new Uint8Array(await renderTile(fast, z, x, y, { properties: ['height', 'name'] })),
        `worker ${workers} preserves tile bytes, ids and properties`,
      )
    }
    const before = (await call({ type: 'stats' })).stats[url]
    await Promise.all(
      cases.slice(0, 16).map(([z, x, y]) => call({ type: 'tile', config: { url }, z, x, y })),
    )
    assert.deepEqual(
      (await call({ type: 'stats' })).stats[url],
      before,
      'transfers never detach the shared page cache',
    )
    const tile = await call({ type: 'tile', config: { url }, z, x, y })
    assert.deepEqual(new Uint8Array(tile.data), new Uint8Array(await renderTile(fast, z, x, y)))
    const [ez, ex, ey] = cases.find(([z]) => z > fast.finestZoom)
    const exact = await call({ type: 'tile', config: { url }, z: ez, x: ex, y: ey })
    assert.deepEqual(new Uint8Array(exact.data), new Uint8Array(await renderTile(fast, ez, ex, ey)))
    const row = await call({ type: 'row', url, rowIndex: 100, columns: ['name', 'height'] })
    assert.deepEqual(row.row, await old.readRow(100, ['name', 'height']))
  } finally {
    await client.close()
  }
}
console.log('1/2/4-worker concurrency, cancellation, metadata, rendering and exact rows passed')
