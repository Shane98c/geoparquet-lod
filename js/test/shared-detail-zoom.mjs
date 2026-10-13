// Measure the potential benefit of sharing pages while zooming into one place.
// node test/shared-detail-zoom.mjs baseline.parquet shared.parquet [delay-ms]
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { workerClient } from './worker-client.mjs'

const [baseline, shared, delay = '50'] = process.argv.slice(2)
if (!baseline || !shared) throw new Error('Supply baseline and shared-detail paths')
const runs = Number(process.env.RUNS ?? 5),
  latencyMs = Number(delay)
assert.ok(Number.isInteger(runs) && runs > 0 && Number.isFinite(latencyMs) && latencyMs >= 0)
const url = 'https://example.test/features.parquet'
const variants = [
  ['baseline', baseline],
  ['shared', shared],
]
const samples = [],
  hashes = new Map()
for (const [label, lon, lat] of [
  ['Tokyo', 139.76, 35.68],
  ['Osaka', 135.5, 34.69],
  ['rural Niigata', 138.76, 37.1],
]) {
  for (let run = 0; run < runs; run++)
    for (const [variant, path] of run % 2 ? [...variants].reverse() : variants) {
      const client = await workerClient(path, 2, latencyMs)
      try {
        await client.call({ type: 'open', url })
        let previous = { ...(await client.call({ type: 'stats' })).stats[url] }
        const phases = []
        for (const z of [14, 15, 16, 15, 14]) {
          const x = Math.floor(((lon + 180) / 360) * 2 ** z) - 1
          const y =
            Math.floor(((1 - Math.asinh(Math.tan((lat * Math.PI) / 180)) / Math.PI) / 2) * 2 ** z) -
            1
          const start = performance.now()
          const tiles = await Promise.all(
            Array.from({ length: 9 }, (_, i) =>
              client.call({
                type: 'tile',
                config: { url, properties: [] },
                z,
                x: x + (i % 3),
                y: y + Math.floor(i / 3),
              }),
            ),
          )
          const ms = performance.now() - start
          const now = (await client.call({ type: 'stats' })).stats[url]
          const hash = createHash('sha256')
          for (const tile of tiles) hash.update(new Uint8Array(tile.data))
          const digest = hash.digest('hex'),
            key = `${label}/${z}/${z === 16 ? 'both' : variant}`
          if (hashes.has(key)) assert.equal(digest, hashes.get(key), key)
          else hashes.set(key, digest)
          phases.push({
            z,
            ms,
            bytes: now.bytes - previous.bytes,
            requests: now.requests - previous.requests,
            hash: digest,
          })
          previous = { ...now }
        }
        samples.push({ label, variant, run, phases })
        console.error(
          `${label} ${variant} run ${run}: ${phases.map((p) => `z${p.z}=${p.bytes}`).join(', ')}`,
        )
      } finally {
        await client.close()
      }
    }
}
const median = (values) => {
  const a = values.toSorted((a, b) => a - b)
  return (a[Math.floor((a.length - 1) / 2)] + a[Math.floor(a.length / 2)]) / 2
}
const results = []
for (const label of new Set(samples.map((r) => r.label)))
  for (const [variant] of variants) {
    const rows = samples.filter((r) => r.label === label && r.variant === variant)
    results.push({
      label,
      variant,
      phases: rows[0].phases.map((p, i) => {
        for (const row of rows)
          for (const key of ['bytes', 'requests']) assert.equal(row.phases[i][key], p[key])
        return { ...p, ms: median(rows.map((r) => r.phases[i].ms)) }
      }),
    })
  }
console.log(
  JSON.stringify(
    {
      baseline,
      shared,
      runs,
      latencyMs,
      workers: 2,
      viewport: '3x3 tiles at each zoom; same center; opening excluded',
      results,
      samples,
    },
    null,
    2,
  ),
)
