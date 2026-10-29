import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { workerClient } from './worker-client.mjs'
import { VectorTile } from '@mapbox/vector-tile'
import { PbfReader } from 'pbf'
import { LodReader, SpatialReader, openReader, coalescing, renderTile } from '../dist/index.js'
import { nested } from './columnar.mjs'

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
const footer = await LodReader.open(coalescing(await makeSource(newPath)))
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

// Exact-geometry neighbours share primary geometry pages. Abort the caller
// that starts a read while a second caller still needs those bytes, as
// during rapid zooming.
const geometryRanges = footer.metadata.row_groups.flatMap((group) =>
  group.columns
    .filter((c) => c.meta_data.path_in_schema.join('.') === fast.primary)
    .map((c) => {
      const start = Number(c.meta_data.data_page_offset)
      return [start, start + Number(c.meta_data.total_compressed_size)]
    }),
)
const footerStart = (await readFile(newPath)).length - 8 - footer.metadata.metadata_length
for (const cancelBoth of [false, true]) {
  const data = await readFile(newPath)
  let hold = true
  let abortedReads = 0
  let started
  const firstPage = new Promise((resolve) => {
    started = resolve
  })
  const releases = []
  const reader = await openReader({
    byteLength: data.length,
    async slice(a, b = data.length, signal) {
      signal?.throwIfAborted()
      if (hold && geometryRanges.some(([lo, hi]) => a < hi && b > lo)) {
        started()
        await new Promise((resolve, reject) => {
          const abort = () => {
            abortedReads++
            reject(signal.reason)
          }
          signal?.addEventListener('abort', abort, { once: true })
          releases.push(() => {
            signal?.removeEventListener('abort', abort)
            resolve()
          })
        })
      }
      return Uint8Array.from(data.subarray(a, b)).buffer
    },
  })
  const cancelled = new AbortController()
  const a = reader.readPrimary(footer.bounds, [], cancelled.signal)
  const rejected = assert.rejects(a, { name: 'AbortError' })
  await firstPage
  const other = new AbortController()
  const b = reader.readPrimary(footer.bounds, [], other.signal)
  const secondRejected = cancelBoth ? assert.rejects(b, { name: 'AbortError' }) : undefined
  // All ungated source reads resolve immediately. Drain them so b reaches
  // the shared pages before cancelling a.
  await new Promise(setImmediate)
  cancelled.abort()
  if (cancelBoth) other.abort()
  hold = false
  for (const release of releases) release()
  const [, live] = await Promise.all([rejected, secondRejected ?? b])
  const control = await footer.readPrimary(footer.bounds)
  if (cancelBoth) assert.ok(abortedReads > 0, 'last neighbour departure stops shared page reads')
  else {
    assert.equal(abortedReads, 0, 'live neighbour keeps shared reads alive')
    assert.ok(live.rows.length, 'live neighbour retains exact features')
    assert.deepEqual(live, control, 'cancelled neighbour cannot poison shared geometry pages')
  }
  assert.deepEqual(await reader.readPrimary(footer.bounds), control, 'cache remains reusable')
}

// Exact reads through the primary directory never open the footer, and give
// the rows, geometry and source attributes the footer's covering selects.
{
  const source = await makeSource(newPath)
  const reader = await openReader(coalescing(source))
  // carried attributes come with the geometry pages, the key through row metadata
  const attrs = [...reader.lod.attributes.map((a) => a.name), reader.lod.key.column]
  const [w, s, e, n] = footer.bounds
  for (const box of [
    footer.bounds,
    [w, s, (w + e) / 2, (s + n) / 2],
    [(w + e) / 2, (s + n) / 2, e, n],
    [w + (e - w) * 0.3, s + (n - s) * 0.3, w + (e - w) * 0.35, s + (n - s) * 0.32],
    [e + 1, n + 1, e + 2, n + 2],
  ]) {
    const exact = await reader.readPrimary(box, attrs)
    assert.deepEqual(exact, await footer.readPrimary(box, attrs), `primary rows in ${box}`)
  }
  assert.ok(
    source.reads.every(([, b]) => b <= footerStart),
    'exact reads use the primary directory, not the footer',
  )
}

// Leaving a view while its analytical footer opens must stop that work too,
// and a later inspection must be able to open it afresh.
{
  const data = await readFile(newPath)
  let hold = true,
    started,
    release,
    aborted = false
  const trailer = new Promise((resolve) => {
    started = resolve
  })
  const reader = await openReader({
    byteLength: data.length,
    async slice(a, b = data.length, signal) {
      signal?.throwIfAborted()
      if (hold && b === data.length) {
        started()
        await new Promise((resolve, reject) => {
          const cancel = () => {
            aborted = true
            reject(signal.reason)
          }
          signal?.addEventListener('abort', cancel, { once: true })
          release = () => {
            signal?.removeEventListener('abort', cancel)
            resolve()
          }
        })
      }
      return Uint8Array.from(data.subarray(a, b)).buffer
    },
  })
  const controller = new AbortController()
  // a nested column is only in the footer
  const request = reader.readColumns([0, 5], ['bbox'], controller.signal)
  const rejected = assert.rejects(request, { name: 'AbortError' })
  await trailer
  controller.abort()
  hold = false
  release()
  await rejected
  assert.ok(aborted, 'abandoned analytical opening aborts its transport')
  assert.deepEqual(
    await reader.readColumns([0, 5], ['bbox']),
    await footer.readColumns([0, 5], ['bbox']),
  )
}

for (const [i, [z, x, y]] of cases.entries()) {
  const [a, b, c] = await Promise.all([old, fast, footer].map((r) => renderTile(r, z, x, y)))
  assert.deepEqual(canonical(c), canonical(a), `standard reader of relocated pages ${z}/${x}/${y}`)
  if (expected[i]) {
    assert.deepEqual(
      new Uint8Array(b),
      new Uint8Array(c),
      `the spatial index and the footer give the same MVT bytes ${z}/${x}/${y}`,
    )
    const attrs = fast.lod.attributes.map((a) => a.name)
    const lod = fast.lodForZoom(z)
    const rows = await fast.readLodByTile(lod, z, x, y, attrs)
    const unstyled = await fast.readLodByTile(lod, z, x, y, [])
    assert.deepEqual(unstyled.rows, rows.rows)
    assert.deepEqual(unstyled.geometry, rows.geometry)
    for (const [reader, data, name] of [
      [fast, rows, 'directory'],
      [old, await old.readLodByTile(lod, z, x, y, attrs), 'key interval'],
    ]) {
      const actual = data.rows.map((id, i) => [
        id,
        nested(data.geometry, i, lod.geometry_type),
        Object.fromEntries(attrs.map((a) => [a, data.columns[a][i]])),
      ])
      assert.deepEqual(
        actual.sort((a, b) => a[0] - b[0]),
        [...expected[i]].sort((a, b) => a[0] - b[0]),
        `exact ${name} membership ${z}/${x}/${y} (${reader.constructor.name})`,
      )
    }
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
// Directory bounds and fences are whole cells of an LOD's finest zoom.
const finest = fast.lodForZoom(fast.finestZoom)
await assert.rejects(
  fast.readLodByTile(finest, Math.max(...finest.zooms) + 1, 0, 0),
  /finer than/,
  'a tile finer than its LOD is refused, not read at cell precision',
)
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
console.log(`spatial tests passed: ${cases.length} tiles, analytical rows, footer reads and cache`)

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
