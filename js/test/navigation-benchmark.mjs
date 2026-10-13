// node test/navigation-benchmark.mjs file.parquet baseline/worker-client.mjs [delay-ms]
// Both harnesses must use this revision's abort-aware worker-harness.mjs.
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { setTimeout as sleep } from 'node:timers/promises'
import { workerClient } from './worker-client.mjs'
const [file, baseline, delay = '50'] = process.argv.slice(2)
const { workerClient: before } = await import(baseline)
const url = 'https://example.test/navigation.parquet'
const runs = Number(process.env.RUNS ?? 3)
const samples = [],
  hashes = new Map()
const route = [11, 12, 13, 14, 15, 16, 17, 16, 14, 12, 11]
for (const dwell of [null, 40, 100]) {
  for (let run = 0; run < runs; run++) {
    const variants = [
      ['before', before],
      ['after', workerClient],
    ]
    if (run % 2) variants.reverse()
    for (const [variant, factory] of variants) {
      const client = await factory(file, 2, Number(delay))
      try {
        await client.call({ type: 'open', url })
        const started = performance.now()
        let finalHash
        for (const [step, z] of route.entries()) {
          const lon = step === route.length - 1 ? 139.76 : 139.76 + step * 0.001
          const n = 2 ** z,
            cx = Math.floor(((lon + 180) / 360) * n)
          const cy = Math.floor(
            ((1 - Math.asinh(Math.tan((35.68 * Math.PI) / 180)) / Math.PI) / 2) * n,
          )
          const jobs = Array.from({ length: 9 }, (_, i) =>
            client.call({
              type: 'tile',
              config: { url, properties: [] },
              z,
              x: cx - 1 + (i % 3),
              y: cy - 1 + Math.floor(i / 3),
            }),
          )
          const results = Promise.allSettled(jobs)
          if (dwell !== null && step < route.length - 1) {
            await sleep(dwell)
            for (const job of jobs) client.cancel(job.id)
          }
          const settled = await results
          for (const r of settled)
            if (r.status === 'rejected') assert.equal(r.reason.name, 'AbortError')
          if (dwell === null || step === route.length - 1) {
            assert.ok(settled.every((r) => r.status === 'fulfilled'))
            const h = createHash('sha256')
            for (const r of settled) h.update(new Uint8Array(r.value.data))
            finalHash = h.digest('hex')
            const key = `${dwell}:${step}`
            if (hashes.has(key))
              assert.equal(finalHash, hashes.get(key), 'unchanged live viewport MVT')
            else hashes.set(key, finalHash)
          }
        }
        const ms = performance.now() - started
        // Let abandoned baseline work finish before taking totals.
        await sleep(1000)
        const result = await client.call({ type: 'stats' })
        assert.equal(result.network.started, result.network.completed + result.network.aborted)
        assert.equal(result.stats[url].bytes, result.network.bytes)
        samples.push({
          variant,
          dwellMs: dwell,
          run,
          ms,
          network: result.network,
          cache: result.cache,
          finalHash,
        })
        console.error(
          `${variant} dwell=${dwell} run=${run}: ${(result.network.bytes / 1e6).toFixed(2)} MB, ${result.network.aborted} aborted`,
        )
      } finally {
        await client.close()
      }
    }
  }
}
console.log(JSON.stringify({ file, delayMs: Number(delay), runs, route, samples }, null, 2))
