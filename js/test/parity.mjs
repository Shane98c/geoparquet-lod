// Reproducible range-level comparison. Optional latency models round trips;
// local elapsed time is not a measurement of S3/CDN performance.
// node test/parity.mjs source.parquet optimized.parquet baseline.pmtiles [latency-ms]
import { open } from 'node:fs/promises'
import { PMTiles } from 'pmtiles'
import { PbfReader } from 'pbf'
import { LodReader, openReader, coalescing, renderTile } from '../dist/index.js'

const [sourcePath, optimizedPath, pmPath, latencyArg = '0'] = process.argv.slice(2)
if (!sourcePath || !optimizedPath || !pmPath)
  throw new Error('expected source.parquet optimized.parquet baseline.pmtiles [latency-ms]')
const latency = Number(latencyArg)
const runs = Number(process.env.RUNS ?? 3)
// LAYOUTS=1 treats both Parquet arguments as indexed files, isolating layout.
const layouts = process.env.LAYOUTS === '1'
if (!Number.isInteger(runs) || runs < 1 || !Number.isFinite(latency) || latency < 0)
  throw new Error('invalid runs or latency')
const tiles = [
  [8, 139.76, 35.68, 'Tokyo metro'],
  [11, 139.76, 35.68, 'Tokyo city'],
  [12, 139.76, 35.68, 'Tokyo district'],
  [13, 139.7, 35.66, 'Shibuya'],
  [13, 139.95, 35.45, 'Chiba'],
  [15, 139.76, 35.68, 'Tokyo station'],
  [15, 135.5, 34.69, 'Osaka'],
]
const xy = (z, lon, lat) => [
  Math.floor(((lon + 180) / 360) * 2 ** z),
  Math.floor(((1 - Math.asinh(Math.tan((lat * Math.PI) / 180)) / Math.PI) / 2) * 2 ** z),
]
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const featureCount = (buffer) => {
  let count = 0
  if (buffer)
    new PbfReader(new Uint8Array(buffer)).readFields((tag, _, pbf) => {
      if (tag === 3)
        pbf.readMessage((tag, _, pbf) => {
          if (tag === 2) count++
          pbf.skip(pbf.type)
        }, {})
      else pbf.skip(pbf.type)
    }, {})
  return count
}
async function source(path) {
  const handle = await open(path)
  const size = (await handle.stat()).size
  let bytes = 0,
    requests = 0,
    pending = 0
  const file = {
    byteLength: size,
    async slice(start, end = size) {
      pending++
      requests++
      end = Math.min(end, size)
      bytes += end - start
      try {
        if (latency) await sleep(latency)
        const buffer = new Uint8Array(end - start)
        const { bytesRead } = await handle.read(buffer, 0, buffer.length, start)
        if (bytesRead !== buffer.length) throw new Error('truncated read')
        return buffer.buffer
      } finally {
        pending--
      }
    },
  }
  return {
    file,
    take() {
      const result = { bytes, requests }
      bytes = requests = 0
      return result
    },
    async drain() {
      while (pending) await sleep(5)
    },
    close: () => handle.close(),
    getKey: () => path,
    async getBytes(offset, length) {
      return { data: await file.slice(offset, offset + length) }
    },
  }
}
const results = []
for (const [z, lon, lat, label] of tiles) {
  for (const [implementation, path] of [
    [layouts ? 'prefix' : 'original', sourcePath],
    [layouts ? 'single' : 'spatial', optimizedPath],
    ['pmtiles', pmPath],
  ]) {
    for (let run = 0; run < runs; run++) {
      const src = await source(path)
      let read,
        nativeZoom = z
      const openStart = performance.now()
      if (implementation === 'pmtiles') {
        const archive = new PMTiles(src)
        nativeZoom = Math.min(z, (await archive.getHeader()).maxZoom)
        read = async (x, y) => (await archive.getZxy(nativeZoom, x, y))?.data
      } else {
        const reader = await (implementation === 'original' ? LodReader.open : openReader)(
          coalescing(src.file),
        )
        read = (x, y) => renderTile(reader, z, x, y, { properties: [] })
      }
      await src.drain()
      const opening = { ...src.take(), ms: performance.now() - openStart }
      const [x, y] = xy(nativeZoom, lon, lat)
      const t = performance.now()
      const data = await read(x, y)
      const ms = performance.now() - t
      await src.drain()
      const cold = {
        ...src.take(),
        ms,
        features: featureCount(data),
        outputBytes: data?.byteLength ?? 0,
      }
      const nextStart = performance.now()
      const nextData = await read(x + 1, y)
      const nextMs = performance.now() - nextStart
      await src.drain()
      const next = { ...src.take(), ms: nextMs, features: featureCount(nextData) }
      results.push({ label, z, nativeZoom, implementation, run, opening, cold, next })
      await src.close()
    }
  }
}
const medians = []
for (let first = 0; first < results.length; first += runs) {
  const group = results.slice(first, first + runs)
  const entry = { ...group[0] }
  delete entry.run
  for (const phase of ['opening', 'cold', 'next']) {
    entry[phase] = { ...entry[phase] }
    entry[phase].ms = group.map((r) => r[phase].ms).sort((a, b) => a - b)[Math.floor(runs / 2)]
  }
  medians.push(entry)
}
console.log(
  JSON.stringify({ latencyMs: latency, runs, results: medians, samples: results }, null, 2),
)
