// Actual coordinator + CPU workers, range delay, cold viewport, pan, cached repeat.
// node test/viewport-workers.mjs file.parquet [latency-ms] [worker-counts]
// Or supply a JSON matrix: [{ variant, path, workers }, ...].
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import { workerClient } from './worker-client.mjs'
const [path, delay = '50', counts = '1,2,4'] = process.argv.slice(2)
if (!path) throw new Error('expected file.parquet [latency-ms] [worker-counts]')
const configs = path.endsWith('.json')
  ? JSON.parse(await readFile(path, 'utf8'))
  : counts
      .split(',')
      .map(Number)
      .map((workers) => ({ variant: `${workers} workers`, path, workers }))
const runs = Number(process.env.RUNS ?? 3)
const url = 'https://example.test/features.parquet'
const samples = [],
  hashes = new Map()
const locations = [
  ['Japan overview', 6, 137, 36, 2],
  ['Tokyo metro', 8, 139.76, 35.68, 2],
  ['Tokyo city', 11, 139.76, 35.68, 3],
  ['Chiba', 13, 139.95, 35.45, 3],
  ['Osaka', 15, 135.5, 34.69, 3],
]
for (const [label, z, lon, lat, width] of locations) {
  const half = Math.floor(width / 2)
  const x = Math.floor(((lon + 180) / 360) * 2 ** z) - half
  const y =
    Math.floor(((1 - Math.asinh(Math.tan((lat * Math.PI) / 180)) / Math.PI) / 2) * 2 ** z) - half
  const cells = Array.from({ length: width * width }, (_, i) => [
    x + (i % width),
    y + Math.floor(i / width),
  ])
  for (let run = 0; run < runs; run++) {
    // Rotate order to reduce thermal/order bias.
    for (const config of [
      ...configs.slice(run % configs.length),
      ...configs.slice(0, run % configs.length),
    ]) {
      const { workers: count, path, variant } = config
      const rssBefore = process.memoryUsage().rss
      let peakRss = rssBefore
      const sampler = setInterval(() => {
        peakRss = Math.max(peakRss, process.memoryUsage().rss)
      }, 5)
      const client = await workerClient(path, count, Number(delay))
      try {
        let previous = { requests: 0, bytes: 0 }
        const take = async () => {
          const now = (await client.call({ type: 'stats' })).stats[url]
          const diff = {
            requests: now.requests - previous.requests,
            bytes: now.bytes - previous.bytes,
          }
          previous = { ...now }
          return diff
        }
        let t = performance.now()
        await client.call({ type: 'open', url })
        const opening = { ms: performance.now() - t, ...(await take()) }
        const phase = async (tiles, name) => {
          const t = performance.now()
          const values = await Promise.all(
            tiles.map(([x, y]) =>
              client.call({ type: 'tile', config: { url, properties: [] }, z, x, y }),
            ),
          )
          const ms = performance.now() - t
          const hash = createHash('sha256')
          for (const v of values) hash.update(new Uint8Array(v.data))
          const digest = hash.digest('hex'),
            key = `${label}/${name}`
          if (hashes.has(key))
            assert.equal(digest, hashes.get(key), 'all workers produce identical MVT bytes')
          else hashes.set(key, digest)
          return {
            ms,
            ...(await take()),
            hash: digest,
            outputBytes: values.reduce((n, v) => n + v.data.byteLength, 0),
          }
        }
        const cold = await phase(cells, 'viewport')
        const pan = await phase(
          Array.from({ length: width }, (_, i) => [x + width, y + i]),
          'pan',
        )
        const warm = await phase(cells, 'viewport')
        assert.equal(warm.bytes, 0, 'cached repeat must not fetch data')
        assert.equal(warm.requests, 0, 'cached repeat must not issue requests')
        const sample = {
          label,
          variant,
          z,
          width,
          workers: count,
          run,
          startupMs: client.startupMs,
          opening,
          cold,
          pan,
          warm,
          peakRssDelta: peakRss - rssBefore,
        }
        samples.push(sample)
        console.error(
          `${label} ${variant} run=${run}: ${cold.ms.toFixed(1)}ms cold, ${warm.ms.toFixed(1)}ms warm`,
        )
      } finally {
        clearInterval(sampler)
        await client.close()
      }
    }
  }
}
const median = (v) => {
  const a = v.toSorted((a, b) => a - b)
  return (a[Math.floor((a.length - 1) / 2)] + a[Math.floor(a.length / 2)]) / 2
}
const results = []
for (const [label] of locations)
  for (const { variant, workers } of configs) {
    const group = samples.filter((v) => v.label === label && v.variant === variant)
    const entry = {
      label,
      variant,
      workers,
      startupMs: median(group.map((v) => v.startupMs)),
      peakRssDelta: median(group.map((v) => v.peakRssDelta)),
    }
    for (const phase of ['opening', 'cold', 'pan', 'warm']) {
      entry[phase] = { ...group[0][phase], ms: median(group.map((v) => v[phase].ms)) }
      for (const row of group) {
        assert.equal(row[phase].bytes, entry[phase].bytes)
        assert.equal(row[phase].requests, entry[phase].requests)
      }
    }
    results.push(entry)
  }
// Shared I/O must not depend on which CPU worker renders a tile.
for (const [label] of locations)
  for (const phase of ['opening', 'cold', 'pan', 'warm']) {
    for (const file of new Set(configs.map((c) => c.path))) {
      const variants = configs.filter((c) => c.path === file).map((c) => c.variant)
      const group = results.filter((v) => v.label === label && variants.includes(v.variant))
      for (const row of group) {
        assert.equal(row[phase].bytes, group[0][phase].bytes)
        assert.equal(row[phase].requests, group[0][phase].requests)
      }
    }
  }
console.log(JSON.stringify({ configs, latencyMs: Number(delay), runs, results, samples }, null, 2))
