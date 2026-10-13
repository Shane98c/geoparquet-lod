// Compare two indexed constructions with the same reader and two CPU workers.
// node test/shared-detail-benchmark.mjs baseline.parquet shared.parquet [latency-ms]
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { VectorTile } from '@mapbox/vector-tile'
import { PbfReader } from 'pbf'
import { workerClient } from './worker-client.mjs'

const [baseline, shared, delay = '50'] = process.argv.slice(2)
if (!baseline || !shared) throw new Error('Supply baseline and shared-detail paths')
const latencyMs = Number(delay),
  runs = Number(process.env.RUNS ?? 5)
assert.ok(Number.isFinite(latencyMs) && latencyMs >= 0)
assert.ok(Number.isInteger(runs) && runs > 0)
const url = 'https://example.test/features.parquet'
const variants = [
  ['baseline', baseline],
  ['shared', shared],
]
const locations = [
  ['Tokyo control', 11, 139.76, 35.68],
  ...[14, 15, 16].flatMap((z) => [
    ['Tokyo', z, 139.76, 35.68],
    ['Osaka', z, 135.5, 34.69],
    ['rural Niigata', z, 138.76, 37.1],
  ]),
]
const samples = [],
  hashes = new Map()
for (const [label, z, lon, lat] of locations) {
  const x = Math.floor(((lon + 180) / 360) * 2 ** z) - 1
  const y =
    Math.floor(((1 - Math.asinh(Math.tan((lat * Math.PI) / 180)) / Math.PI) / 2) * 2 ** z) - 1
  const cells = Array.from({ length: 9 }, (_, i) => [x + (i % 3), y + Math.floor(i / 3)])
  for (let run = 0; run < runs; run++) {
    for (const [variant, path] of run % 2 ? [...variants].reverse() : variants) {
      const client = await workerClient(path, 2, latencyMs)
      try {
        let previous = { bytes: 0, requests: 0 }
        const take = async () => {
          const now = (await client.call({ type: 'stats' })).stats[url]
          const diff = {
            bytes: now.bytes - previous.bytes,
            requests: now.requests - previous.requests,
          }
          previous = { ...now }
          return diff
        }
        let start = performance.now()
        await client.call({ type: 'open', url })
        const opening = { ms: performance.now() - start, ...(await take()) }
        const phase = async (name, tiles, properties) => {
          start = performance.now()
          const values = await Promise.all(
            tiles.map(([x, y]) =>
              client.call({ type: 'tile', config: { url, properties }, z, x, y }),
            ),
          )
          const ms = performance.now() - start
          const hash = createHash('sha256')
          let features = 0
          for (const value of values) {
            hash.update(new Uint8Array(value.data))
            const tile = new VectorTile(new PbfReader(new Uint8Array(value.data)))
            for (const layer of Object.values(tile.layers)) features += layer.length
          }
          const digest = hash.digest('hex')
          // Finer quantization changes z14/15 output. Control and z16 should
          // remain identical across constructions; every run is deterministic.
          const key = `${label}/${z}/${name}/${z === 11 || z === 16 ? 'both' : variant}`
          if (hashes.has(key)) assert.equal(digest, hashes.get(key), key)
          else hashes.set(key, digest)
          return { ms, ...(await take()), features, hash: digest }
        }
        const cold = await phase('cold', cells, [])
        const pan = await phase(
          'pan',
          Array.from({ length: 3 }, (_, i) => [x + 3, y + i]),
          [],
        )
        const warm = await phase('warm', cells, [])
        const carried = await phase('carried', cells, ['height', 'class'])
        const source = await phase('source', cells, ['height', 'class', 'version'])
        const repeat = await phase('repeat', cells, ['height', 'class', 'version'])
        for (const result of [warm, carried, repeat]) {
          assert.equal(result.bytes, 0)
          assert.equal(result.requests, 0)
        }
        assert.equal(cold.hash, warm.hash)
        assert.equal(source.hash, repeat.hash)
        samples.push({ label, z, variant, run, opening, cold, pan, warm, carried, source, repeat })
        console.error(
          `${label} z${z} ${variant} run ${run}: ${cold.bytes} bytes, ${cold.requests} requests, ${cold.ms.toFixed(1)} ms`,
        )
      } finally {
        await client.close()
      }
    }
  }
}
const median = (values) => {
  const sorted = values.toSorted((a, b) => a - b)
  return (sorted[Math.floor((sorted.length - 1) / 2)] + sorted[Math.floor(sorted.length / 2)]) / 2
}
const results = []
for (const [label, z] of locations)
  for (const [variant] of variants) {
    const rows = samples.filter((r) => r.label === label && r.z === z && r.variant === variant)
    const result = { label, z, variant }
    for (const name of ['opening', 'cold', 'pan', 'warm', 'carried', 'source', 'repeat']) {
      result[name] = { ...rows[0][name], ms: median(rows.map((r) => r[name].ms)) }
      for (const row of rows)
        for (const key of ['bytes', 'requests']) assert.equal(row[name][key], result[name][key])
    }
    results.push(result)
  }
console.log(
  JSON.stringify(
    {
      baseline,
      shared,
      latencyMs,
      runs,
      workers: 2,
      viewport: '3x3 tiles; pan adds one column',
      results,
      samples,
    },
    null,
    2,
  ),
)
