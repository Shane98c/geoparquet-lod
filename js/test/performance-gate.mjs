// Same fixture, process, harness and dependencies for both revisions.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { pathToFileURL } from 'node:url'
import { resolve } from 'node:path'
import { createHash } from 'node:crypto'
import { VectorTile } from '@mapbox/vector-tile'
import { PbfReader } from 'pbf'

export const median = (values) => [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)]
export function checkRegression(base, candidate, { cpuRatio = 1.25, cpuFloorMs = 15 } = {}) {
  for (const key of ['bytes', 'requests'])
    assert.ok(candidate[key] <= base[key], `${key} regression: ${candidate[key]} > ${base[key]}`)
  for (const key of ['coldCpuMs', 'warmCpuMs'])
    assert.ok(
      candidate[key] <= base[key] * cpuRatio + cpuFloorMs,
      `${key} CPU regression: ${candidate[key].toFixed(1)} ms > ${(base[key] * cpuRatio + cpuFloorMs).toFixed(1)} ms`,
    )
}

async function main() {
  const [basePath, candidatePath, fixture] = process.argv.slice(2)
  if (!fixture)
    throw new Error('Usage: node test/performance-gate.mjs BASE_DIST CANDIDATE_DIST FIXTURE')
  const modules = await Promise.all(
    [basePath, candidatePath].map((path) => import(pathToFileURL(resolve(path, 'index.js')))),
  )
  const data = await readFile(fixture)
  const samples = [[], []]
  const results = []
  const tile = (z, lon, lat) => [
    z,
    Math.floor(((lon + 180) / 360) * 2 ** z),
    Math.floor(((1 - Math.asinh(Math.tan((lat * Math.PI) / 180)) / Math.PI) / 2) * 2 ** z),
  ]
  const center = tile(14, -104.98, 39.73)
  const cases = [
    [10, ...tile(10, -104.98, 39.73).slice(1), {}],
    ...Array.from({ length: 9 }, (_, i) => [
      14,
      center[1] + (i % 3) - 1,
      center[2] + Math.floor(i / 3) - 1,
      {},
    ]),
    [...center, { properties: ['height', 'class', 'name'] }],
    [...tile(17, -104.98, 39.73), { properties: ['height', 'name'] }],
  ]
  async function run(api, iterations) {
    let bytes = 0,
      requests = 0
    const source = {
      byteLength: data.length,
      async slice(start, end = data.length) {
        requests++
        bytes += end - start
        return Uint8Array.from(data.subarray(start, end)).buffer
      },
    }
    const reader = await api.openReader(api.coalescing(source))
    const hash = createHash('sha256')
    let features = 0
    for (const [z, x, y, options] of cases) {
      const bytes = new Uint8Array(await api.renderTile(reader, z, x, y, options))
      hash.update(bytes)
      const count = Object.values(new VectorTile(new PbfReader(bytes)).layers).reduce(
        (sum, layer) => sum + layer.length,
        0,
      )
      if (z === 10 || z === 17) assert.ok(count > 0, `fixture z${z} must render features`)
      features += count
    }
    assert.ok(features > 0, 'fixture workload must render features')
    // Count cold opening, viewport, property projection and exact-geometry I/O.
    const io = { bytes, requests }
    let start = performance.now()
    for (let i = 0; i < iterations; i++)
      for (const [z, x, y, options] of cases) await api.renderTile(reader, z, x, y, options)
    const warmCpuMs = performance.now() - start
    start = performance.now()
    for (let i = 0; i < iterations; i++) {
      const cold = await api.openReader(api.coalescing(source))
      for (const [z, x, y, options] of cases) await api.renderTile(cold, z, x, y, options)
    }
    return {
      ...io,
      coldCpuMs: performance.now() - start,
      warmCpuMs,
      features,
      hash: hash.digest('hex'),
    }
  }
  // Calibrate both revisions to at least 200ms; then use identical iteration counts.
  let iterations = 1
  for (;;) {
    const warm = []
    for (const api of modules) warm.push(await run(api, iterations))
    if (Math.min(...warm.flatMap((r) => [r.coldCpuMs, r.warmCpuMs])) >= 200) break
    iterations *= 2
    if (iterations > 4096) throw new Error('CPU calibration failed')
  }
  // Sequential paired samples alternate which revision gets the first slot.
  for (let round = 0; round < 7; round++)
    for (const index of round % 2 ? [1, 0] : [0, 1])
      samples[index].push(await run(modules[index], iterations))
  for (const rows of samples) {
    for (const row of rows) {
      assert.equal(row.bytes, rows[0].bytes)
      assert.equal(row.requests, rows[0].requests)
      assert.equal(row.hash, rows[0].hash)
    }
    results.push({
      ...rows[0],
      coldCpuMs: median(rows.map((row) => row.coldCpuMs)),
      warmCpuMs: median(rows.map((row) => row.warmCpuMs)),
    })
  }
  assert.equal(results[0].hash, results[1].hash, 'rendered tile bytes must match')
  console.log(
    JSON.stringify(
      { fixture, iterations, baseline: results[0], candidate: results[1], samples },
      null,
      2,
    ),
  )
  checkRegression(...results)
}
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href)
  await main()
