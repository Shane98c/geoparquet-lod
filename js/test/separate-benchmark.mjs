// node test/separate-benchmark.mjs packed.parquet separate.parquet [delay-ms]
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { stat } from 'node:fs/promises'
import { workerClient } from './worker-client.mjs'
const [packed, separate, delay = '50'] = process.argv.slice(2)
const runs = Number(process.env.RUNS ?? 5),
  url = 'https://example.test/features.parquet'
const samples = [],
  hashes = new Map()
const variants = [
  ['packed', packed],
  ['separate', separate],
]
for (const [label, z, width] of [
  ['metro', 8, 2],
  ['city', 11, 3],
  ['neighborhood', 14, 3],
]) {
  const n = 2 ** z,
    x = Math.floor(((139.76 + 180) / 360) * n) - Math.floor(width / 2),
    y =
      Math.floor(((1 - Math.asinh(Math.tan((35.68 * Math.PI) / 180)) / Math.PI) / 2) * n) -
      Math.floor(width / 2)
  const cells = Array.from({ length: width * width }, (_, i) => [
    x + (i % width),
    y + Math.floor(i / width),
  ])
  for (let run = 0; run < runs; run++)
    for (const [variant, path] of run % 2 ? [...variants].reverse() : variants) {
      for (const styled of [false, true]) {
        const client = await workerClient(path, 2, Number(delay))
        try {
          let prior = { requests: 0, bytes: 0 }
          const take = async () => {
            const now = (await client.call({ type: 'stats' })).stats[url]
            const diff = { requests: now.requests - prior.requests, bytes: now.bytes - prior.bytes }
            prior = { ...now }
            return diff
          }
          let t = performance.now()
          await client.call({ type: 'open', url })
          const opening = { ms: performance.now() - t, ...(await take()) }
          const phase = async (name, tiles, properties) => {
            const t = performance.now()
            const results = await Promise.all(
              tiles.map(([x, y]) =>
                client.call({ type: 'tile', config: { url, properties }, z, x, y }),
              ),
            )
            const ms = performance.now() - t,
              hash = createHash('sha256')
            for (const result of results) hash.update(new Uint8Array(result.data))
            const key = JSON.stringify([label, name]),
              digest = hash.digest('hex')
            if (hashes.has(key)) assert.equal(digest, hashes.get(key), `identical output ${key}`)
            else hashes.set(key, digest)
            const result = { variant, label, run, phase: name, ms, ...(await take()) }
            samples.push(result)
            console.error(JSON.stringify(result))
          }
          samples.push({
            variant,
            label,
            run,
            phase: styled ? 'open styled' : 'open geometry',
            ...opening,
          })
          if (styled) await phase('cold styled', cells, ['height', 'class'])
          else {
            await phase('cold geometry', cells, [])
            await phase(
              'pan geometry',
              cells.map(([x, y]) => [x + 1, y]),
              [],
            )
            await phase('warm geometry', cells, [])
            await phase('add height', cells, ['height'])
            await phase('add class', cells, ['height', 'class'])
            await phase('add source version', cells, ['height', 'class', 'version'])
            await phase('repeat source version', cells, ['height', 'class', 'version'])
          }
        } finally {
          await client.close()
        }
      }
    }
}
const median = (values) => [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)]
const summary = []
for (const variant of variants.map((v) => v[0]))
  for (const label of ['metro', 'city', 'neighborhood']) {
    const rows = samples.filter((s) => s.variant === variant && s.label === label)
    for (const phase of [...new Set(rows.map((s) => s.phase))]) {
      const values = rows.filter((s) => s.phase === phase)
      summary.push({
        variant,
        label,
        phase,
        ...Object.fromEntries(
          ['ms', 'requests', 'bytes'].map((key) => [key, median(values.map((s) => s[key]))]),
        ),
      })
    }
  }
console.log(
  JSON.stringify(
    {
      runs,
      delayMs: Number(delay),
      workers: 2,
      files: await Promise.all(
        variants.map(async ([variant, path]) => ({
          variant,
          path,
          bytes: (await stat(path)).size,
        })),
      ),
      summary,
      samples,
    },
    null,
    2,
  ),
)
