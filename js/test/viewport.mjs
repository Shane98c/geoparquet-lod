// node test/viewport.mjs prefix.parquet single.parquet baseline.pmtiles [latency-ms]
// Parallel visible tiles, then only the newly exposed column during an east pan.
import { open } from 'node:fs/promises'
import { PMTiles } from 'pmtiles'
import { PbfReader } from 'pbf'
import { openReader, coalescing, renderTile } from '../dist/index.js'
const [prefix, single, pmPath, delay = '50'] = process.argv.slice(2)
const latencyMs = Number(delay),
  runs = Number(process.env.RUNS ?? 3)
if (!pmPath) throw new Error('expected prefix.parquet single.parquet baseline.pmtiles [latency-ms]')
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
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
  const handle = await open(path),
    byteLength = (await handle.stat()).size
  let bytes = 0,
    requests = 0
  return {
    file: {
      byteLength,
      async slice(a, b = byteLength) {
        bytes += b - a
        requests++
        if (latencyMs) await sleep(latencyMs)
        const data = new Uint8Array(b - a)
        const { bytesRead } = await handle.read(data, 0, data.length, a)
        if (bytesRead !== data.length) throw new Error('truncated read')
        return data.buffer
      },
    },
    take() {
      const out = { bytes, requests }
      bytes = requests = 0
      return out
    },
    close: () => handle.close(),
  }
}
const samples = []
for (const [label, z, lon, lat, width] of [
  ['Japan overview', 6, 137, 36, 2],
  ['Tokyo metro', 8, 139.76, 35.68, 2],
  ['Tokyo city', 11, 139.76, 35.68, 3],
  ['Chiba', 13, 139.95, 35.45, 3],
  ['Osaka', 15, 135.5, 34.69, 3],
]) {
  for (const [implementation, path] of [
    ['prefix', prefix],
    ['single', single],
    ['pmtiles', pmPath],
  ]) {
    for (let run = 0; run < runs; run++) {
      const src = await source(path)
      let nativeZoom = z,
        read
      let t = performance.now()
      if (implementation === 'pmtiles') {
        const archive = new PMTiles({
          getKey: () => path,
          getBytes: async (a, n) => ({ data: await src.file.slice(a, a + n) }),
        })
        nativeZoom = Math.min(z, (await archive.getHeader()).maxZoom)
        read = async (x, y) => (await archive.getZxy(nativeZoom, x, y))?.data
      } else {
        const reader = await openReader(coalescing(src.file))
        read = (x, y) => renderTile(reader, z, x, y, { properties: [] })
      }
      const opening = { ...src.take(), ms: performance.now() - t }
      const half = Math.floor(width / 2)
      const x = Math.floor(((lon + 180) / 360) * 2 ** nativeZoom) - half,
        y =
          Math.floor(
            ((1 - Math.asinh(Math.tan((lat * Math.PI) / 180)) / Math.PI) / 2) * 2 ** nativeZoom,
          ) - half
      const phase = async (cells) => {
        const t = performance.now(),
          data = await Promise.all(cells.map(([x, y]) => read(x, y)))
        return {
          ...src.take(),
          ms: performance.now() - t,
          features: data.reduce((sum, d) => sum + featureCount(d), 0),
        }
      }
      const cells = Array.from({ length: width * width }, (_, i) => [
        x + (i % width),
        y + Math.floor(i / width),
      ])
      const cold = await phase(cells),
        pan = await phase(Array.from({ length: width }, (_, i) => [x + width, y + i]))
      samples.push({ label, z, nativeZoom, width, implementation, run, opening, cold, pan })
      await src.close()
    }
  }
}
const results = []
for (let i = 0; i < samples.length; i += runs) {
  const group = samples.slice(i, i + runs),
    entry = { ...group[0] }
  delete entry.run
  for (const phase of ['opening', 'cold', 'pan'])
    entry[phase] = {
      ...entry[phase],
      ms: group.map((v) => v[phase].ms).sort((a, b) => a - b)[Math.floor(runs / 2)],
    }
  results.push(entry)
}
console.log(JSON.stringify({ latencyMs, runs, results, samples }, null, 2))
