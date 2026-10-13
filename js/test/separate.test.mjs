import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { openReader, LodReader, readFeatures, renderTile } from '../dist/index.js'
import { workerClient } from './worker-client.mjs'
const [a, b, raw, projectionPath, projectionExpected] = process.argv.slice(2)
const source = async (path) => {
  const bytes = await readFile(path),
    reads = []
  return {
    reads,
    byteLength: bytes.length,
    async slice(a, b) {
      reads.push([a, b])
      return Uint8Array.from(bytes.subarray(a, b)).buffer
    },
  }
}
const first = await openReader(await source(a)),
  secondSource = await source(b)
const second = await openReader(secondSource)
const fallback = await LodReader.open(await source(raw))
const attrs = first.lod.attributes.map((a) => a.name)
const xy = (z, lon, lat) => [
  Math.floor(((lon + 180) / 360) * 2 ** z),
  Math.floor(((1 - Math.asinh(Math.tan((lat * Math.PI) / 180)) / Math.PI) / 2) * 2 ** z),
]
for (const z of [9, 10, 11, 12]) {
  const [x, y] = xy(z, -104.85, 39.85)
  for (const columns of [[], ['height'], attrs]) {
    const one = await first.readLodByTile(first.lodForZoom(z), z, x, y, columns)
    const two = await second.readLodByTile(second.lodForZoom(z), z, x, y, columns)
    assert.deepEqual(two, one, `nested ${z} ${columns}`)
    assert.deepEqual(
      await renderTile(second, z, x, y, { properties: columns }),
      await renderTile(first, z, x, y, { properties: columns }),
      `MVT ${z} ${columns}`,
    )
    const old = await fallback.readLodByTile(fallback.lodForZoom(z), z, x, y, columns)
    const lookup = new Map(one.rows.map((id, i) => [id, i]))
    for (let i = 0; i < old.rows.length; i++)
      if (lookup.has(old.rows[i])) {
        const j = lookup.get(old.rows[i])
        for (const name of columns)
          assert.deepEqual(old.columns[name][i], one.columns[name][j], `fallback ${name}`)
      }
  }
}
const z = 11,
  [x, y] = xy(z, -104.85, 39.85)
const extra = await second.readLodByTile(second.lodForZoom(z), z, x, y, ['source_only'])
const beforeProjection = secondSource.reads.length
const projected = await second.readColumns(extra.rows, ['source_only'])
assert.equal(secondSource.reads.length, beforeProjection, 'requested source values are cached')
assert.deepEqual(extra.columns, projected)
const duplicated = await second.readColumns([extra.rows[0], extra.rows[0]], ['source_only'])
assert.equal(duplicated.source_only[0], duplicated.source_only[1])
await assert.rejects(second.readColumns([-1], ['source_only']), /out of range/)
await assert.rejects(second.readColumns([400], ['source_only']), /out of range/)
await assert.rejects(second.readColumns([], ['does_not_exist']), /unknown column/)
// Fetch a new style after geometry is cached: none of its geometry pages are fetched again.
const coldSource = await source(b),
  cold = await openReader(coldSource)
await cold.readLodByTile(cold.lodForZoom(z), z, x, y, [])
const prior = new Set(coldSource.reads.map((range) => range.join(',')))
const boundary = coldSource.reads.length
await cold.readLodByTile(cold.lodForZoom(z), z, x, y, attrs)
assert.ok(coldSource.reads.length > boundary)
assert.ok(coldSource.reads.slice(boundary).every((range) => !prior.has(range.join(','))))
const warm = coldSource.reads.length
await cold.readLodByTile(cold.lodForZoom(z), z, x, y, attrs)
assert.equal(coldSource.reads.length, warm)
const query = {
  bbox: [-105, 39.65, -104.7, 39.95],
  resolution: 360 / (512 * 2 ** 11),
  columns: ['height', 'source_only'],
}
const one = await readFeatures(first, query),
  two = await readFeatures(second, query)
assert.deepEqual(two, one)
assert.equal(new Set(two.rows).size, two.rows.length)
assert.ok(two.rows.length > 0)
const originals = await second.readColumns(two.rows, ['source_only'])
assert.deepEqual(two.columns.source_only, originals.source_only)
// Large geometry is returned whole even for a small query: both test polygon
// and line reach well beyond the bbox; no tile clipping is in the read API.
if (two.lod.geometry_type !== 'Point') {
  let outside = false
  const walk = (value) => {
    if (typeof value[0] === 'number') {
      const lon = value[0] * two.lod.scale[0] + two.lod.offset[0]
      outside ||= lon < query.bbox[0] || lon > query.bbox[2]
    } else value.forEach(walk)
  }
  two.coords.forEach(walk)
  assert.ok(outside)
}
const primary = await readFeatures(second, { ...query, geometry: 'primary' })
assert.deepEqual(primary.columns, await second.readColumns(primary.rows, query.columns))
const exactA = await first.readPrimary(query.bbox, attrs),
  exactB = await second.readPrimary(query.bbox, attrs)
assert.deepEqual(exactB, exactA)
await assert.rejects(
  readFeatures(second, { ...query, bbox: [-180, -80, 180, 80], maxTiles: 1 }),
  /maxTiles/,
)
const wrapped = await readFeatures(second, { ...query, bbox: [179, 39, -179, 40], resolution: 1 })
assert.equal(wrapped.rows.length, 0)
// Actual CPU workers must receive companion buffers and preserve requested properties.
const client = await workerClient(b, 2, 0)
try {
  const response = await client.call({
    type: 'tile',
    config: { url: 'https://example.test/features.parquet', properties: attrs },
    z,
    x,
    y,
  })
  assert.deepEqual(
    new Uint8Array(response.data),
    new Uint8Array(await renderTile(second, z, x, y, { properties: attrs })),
  )
  const dynamic = await client.call({
    type: 'tile',
    config: { url: 'https://example.test/features.parquet', properties: ['source_only'] },
    z,
    x,
    y,
  })
  assert.deepEqual(
    new Uint8Array(dynamic.data),
    new Uint8Array(await renderTile(second, z, x, y, { properties: ['source_only'] })),
  )
} finally {
  await client.close()
}
console.log('separated attributes, whole-feature queries, batch joins, and CPU workers passed')

// The same packed viewport changes styles without another geometry decode.
for (const workers of [1, 2]) {
  const cached = await workerClient(a, workers, 0)
  try {
    const url = 'https://example.test/features.parquet'
    for (const properties of [
      ['source_only'],
      [],
      attrs,
      ['height', 'source_only'],
      ['source_only'],
      [],
      ['height', 'source_only'],
    ]) {
      const response = await cached.call({ type: 'tile', config: { url, properties }, z, x, y })
      assert.deepEqual(
        new Uint8Array(response.data),
        new Uint8Array(await renderTile(first, z, x, y, { properties })),
      )
      const { cache } = await cached.call({ type: 'stats' })
      assert.equal(cache.decodes, 1, 'styles reuse geometry')
      assert.ok(cache.bytes <= 32 * 1024 * 1024)
    }
    const metrics = await cached.call({ type: 'stats' })
    const different = await cached.call({
      type: 'tile',
      config: { url: 'https://another.test/features.parquet', properties: [] },
      z,
      x,
      y,
    })
    assert.ok(different.data.byteLength)
    assert.equal(
      (await cached.call({ type: 'stats' })).cache.decodes,
      metrics.cache.decodes + 1,
      'source URLs cannot share cached tiles',
    )
    await assert.rejects(
      cached.call({ type: 'tile', config: { url, properties: ['does_not_exist'] }, z, x, y }),
      /unknown column/,
    )
  } finally {
    await cached.close()
  }
}
const projectedFile = await source(projectionPath)
const projectionReader = await LodReader.open(projectedFile)
const golden = JSON.parse(await readFile(projectionExpected, 'utf8'))
assert.deepEqual(
  await projectionReader.readColumns(golden.rows, Object.keys(golden.expected)),
  golden.expected,
)
// Actual worker projection, separate reader to avoid value-cache hits.
import { TilePool } from '../dist/tile-pool.js'
import { Worker as NodeWorker } from 'node:worker_threads'
globalThis.Worker = class {
  constructor(url) {
    this.worker = new NodeWorker(new URL('./worker-harness.mjs', import.meta.url), {
      workerData: { entry: url.href },
    })
  }
  postMessage(data, transfer) {
    this.worker.postMessage(data, transfer)
  }
  addEventListener(type, callback) {
    this.worker.on(type, (data) => callback(type === 'message' ? { data } : data))
  }
  terminate() {
    return this.worker.terminate()
  }
}
const pool = new TilePool(2)
try {
  await pool.ready
  const reader = await LodReader.open(await source(projectionPath))
  let tasks = 0
  assert.deepEqual(
    await reader.readColumns(
      golden.rows,
      Object.keys(golden.expected),
      undefined,
      (task, signal) => {
        tasks++
        return pool.project(task, signal)
      },
    ),
    golden.expected,
  )
  assert.ok(tasks > 0)
} finally {
  for (const slot of pool.slots) slot.worker.terminate()
}
