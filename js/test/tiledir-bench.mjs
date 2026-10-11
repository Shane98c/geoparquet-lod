import assert from 'node:assert/strict'
import { open, stat, writeFile } from 'node:fs/promises'
import { pathToFileURL } from 'node:url'
import { VectorTile } from '@mapbox/vector-tile'
import { PbfReader } from 'pbf'
import { LodReader, TileDirectory, coalescing, httpSource, renderTile } from '../dist/index.js'

const denver = process.env.DENVER_URL ?? 'http://localhost:8091/overture_denver.lod.parquet'
const nyc = process.env.NYC_URL ?? 'http://localhost:8098/p16384.parquet'
const runs = Number(process.env.RUNS ?? 3)
const local = process.env.FILE_TRANSPORT === '1'
const scratch =
  process.env.SCRATCHPAD ??
  '/private/tmp/claude-501/-Users-shaneloeffler-dev-carbonplan-geoparquet-lod/d381515f-9625-4789-8845-832ecdedf85f/scratchpad'
const datasets = [
  {
    name: 'Denver',
    url: local ? pathToFileURL('../overture_denver.lod.parquet').href : denver,
    center: [-104.99, 39.74],
  },
  {
    name: 'NYC',
    url: local ? pathToFileURL(`${scratch}/nyc/p16384.parquet`).href : nyc,
    center: [-73.98, 40.75],
  },
]
const tileAt = ([lon, lat], z) => [
  z,
  Math.floor(((lon + 180) / 360) * 2 ** z),
  Math.floor(((1 - Math.asinh(Math.tan((lat * Math.PI) / 180)) / Math.PI) / 2) * 2 ** z),
]
const decode = (buffer) => {
  const tile = new VectorTile(new PbfReader(new Uint8Array(buffer)))
  return Object.entries(tile.layers)
    .flatMap(([layer, value]) =>
      Array.from({ length: value.length }, (_, i) => {
        const f = value.feature(i)
        return {
          layer,
          id: f.id,
          type: f.type,
          geometry: f.loadGeometry().map((ring) => ring.map(({ x, y }) => [x, y])),
          properties: f.properties,
        }
      }),
    )
    .sort((a, b) => a.layer.localeCompare(b.layer) || a.id - b.id)
}
const networkFetch = globalThis.fetch
const handles = new Map()
const pending = new Set()
const settle = async () => {
  do {
    await new Promise((resolve) => setImmediate(resolve))
    await Promise.all([...pending])
  } while (pending.size)
}
const originalFetch = async (url, options) => {
  if (!String(url).startsWith('file:')) return networkFetch(url, options)
  const path = new URL(url)
  const { size } = await stat(path)
  if (options?.method === 'HEAD')
    return new Response(null, { headers: { 'content-length': String(size) } })
  const suffix = /^bytes=-(\d+)$/.exec(options.headers.Range)
  const match = /^bytes=(\d+)-(\d+)$/.exec(options.headers.Range)
  const start = suffix ? Math.max(0, size - Number(suffix[1])) : Number(match[1]),
    end = suffix ? size : Number(match[2]) + 1
  let handle = handles.get(url)
  if (!handle) handles.set(url, (handle = await open(path)))
  const bytes = new Uint8Array(end - start)
  const { bytesRead } = await handle.read(bytes, 0, bytes.length, start)
  return new Response(bytes.subarray(0, bytesRead), {
    status: 206,
    headers: {
      'content-length': String(bytesRead),
      'content-range': `bytes ${start}-${start + bytesRead - 1}/${size}`,
    },
  })
}
let active
// Count response bodies and all HTTP requests, including HEAD and the sidecar open.
globalThis.fetch = async (...args) => {
  const stats = active
  if (stats) stats.requests++
  const request = originalFetch(...args)
  pending.add(request)
  let response
  try {
    response = await request
  } finally {
    pending.delete(request)
  }
  const arrayBuffer = response.arrayBuffer.bind(response)
  response.arrayBuffer = async () => {
    const body = arrayBuffer()
    pending.add(body)
    try {
      return await body
    } finally {
      pending.delete(body)
    }
  }
  if (stats && args[1]?.method !== 'HEAD')
    stats.bytes += Number(response.headers.get('content-length') ?? 0)
  return response
}
// the fixtures declare no generation id; the open warns about that on every cold trial
const warn = console.warn
console.warn = (...args) => {
  if (!String(args[0]).includes('no generation id')) warn(...args)
}
const results = []
const sidecars = []
let mismatches = 0
for (const dataset of datasets) {
  const sidecarUrl = `${dataset.url}.tiledir.parquet`
  const metadataReader = await LodReader.open(coalescing(await httpSource([dataset.url])))
  await settle()
  const openStats = { bytes: 0, requests: 0 }
  active = openStats
  const directory = await TileDirectory.open(metadataReader, sidecarUrl)
  active = undefined
  const sizeResponse = await fetch(sidecarUrl, { method: 'HEAD' })
  sidecars.push({
    dataset: dataset.name,
    bytes: Number(sizeResponse.headers.get('content-length')),
    footer: directory.metadata.metadata_length,
    rowGroups: directory.metadata.row_groups.length,
    open: openStats,
    header: directory.header,
  })
  const tiles = [2, 6, 10, 12, 13, 14, 15, 16, 17].map((z) => tileAt(dataset.center, z))
  const street = tileAt(dataset.center, 16)
  tiles.push([16, street[1] + 1, street[2]], [16, street[1], street[2] + 1], [16, 0, 0])
  await settle()
  for (const tile of tiles) {
    const measurements = { index: [], tiledir: [] }
    for (let run = 0; run < runs; run++) {
      const outputs = {}
      for (const mode of run % 2 ? ['tiledir', 'index'] : ['index', 'tiledir']) {
        const stats = { bytes: 0, requests: 0 }
        active = stats
        const start = performance.now()
        const reader = await LodReader.open(coalescing(await httpSource([dataset.url])))
        let used = false
        let td
        if (mode === 'tiledir' && tile[0] <= reader.finestZoom) {
          td = await TileDirectory.open(reader, sidecarUrl)
          const read = td.readLodByTile.bind(td)
          td.readLodByTile = async (...args) => {
            const rows = await read(...args)
            used = true
            return rows
          }
        }
        const buffer = await renderTile(reader, ...tile, td ? { tiledir: td } : {})
        await settle()
        const ms = performance.now() - start
        active = undefined
        if (td && td.covers(reader.lodForZoom(tile[0]), tile[0]))
          assert.ok(used, 'benchmark must not silently fall back')
        outputs[mode] = decode(buffer)
        measurements[mode].push({
          ...stats,
          ms,
          mvt: buffer.byteLength,
          features: outputs[mode].length,
        })
      }
      try {
        assert.deepEqual(outputs.tiledir, outputs.index)
      } catch (error) {
        mismatches++
        const index = new Map(outputs.index.map((f) => [f.id, f]))
        const td = new Map(outputs.tiledir.map((f) => [f.id, f]))
        console.error('MISMATCH', dataset.name, tile, {
          onlyIndex: [...index.keys()].filter((id) => !td.has(id)),
          onlyDirectory: [...td.keys()].filter((id) => !index.has(id)),
          different: [...index.keys()].filter(
            (id) => td.has(id) && JSON.stringify(index.get(id)) !== JSON.stringify(td.get(id)),
          ),
        })
        if (process.env.VERBOSE) console.error(error)
      }
    }
    const median = (values) => values.sort((a, b) => a - b)[Math.floor(values.length / 2)]
    const summary = Object.fromEntries(
      Object.entries(measurements).map(([mode, values]) => [
        mode,
        Object.fromEntries(
          ['bytes', 'requests', 'ms', 'mvt', 'features'].map((key) => [
            key,
            median(values.map((v) => v[key])),
          ]),
        ),
      ]),
    )
    results.push({ dataset: dataset.name, tile: tile.join('/'), ...summary })
    console.log(JSON.stringify(results.at(-1)))
  }
}
console.log(
  '\n| Dataset | Tile | Index KiB / req / ms | Directory KiB / req / ms | MVT bytes | Features |',
)
console.log('|---|---|---:|---:|---:|---:|')
for (const r of results) {
  const cell = (v) => `${(v.bytes / 1024).toFixed(1)} / ${v.requests} / ${v.ms.toFixed(1)}`
  console.log(
    `| ${r.dataset} | ${r.tile} | ${cell(r.index)} | ${cell(r.tiledir)} | ${r.index.mvt} / ${r.tiledir.mvt} | ${r.index.features} / ${r.tiledir.features} |`,
  )
}
console.log('\n| Dataset | Sidecar bytes | Footer bytes | Row groups | Open bytes / requests |')
console.log('|---|---:|---:|---:|---:|')
for (const { dataset, bytes, footer, rowGroups, open } of sidecars) {
  console.log(
    `| ${dataset} | ${bytes} | ${footer} | ${rowGroups} | ${open.bytes} / ${open.requests} |`,
  )
}
console.log('\n| Dataset | LOD | Zoom | Entries |')
console.log('|---|---|---:|---:|')
for (const { dataset, header } of sidecars)
  for (const d of header.directories) {
    console.log(`| ${dataset} | ${d.lod} | ${d.zoom} | ${d.entries} |`)
  }
console.log(
  `\nMismatches: ${mismatches}; ${runs} cold repetitions, medians. Transport: ${local ? 'local-file byte-range adapter' : 'HTTP'}`,
)
if (process.env.RESULTS)
  await writeFile(
    process.env.RESULTS,
    JSON.stringify(
      {
        transport: local ? 'local-file byte-range adapter' : 'HTTP',
        runs,
        results,
        sidecars,
        mismatches,
      },
      null,
      2,
    ),
  )
for (const handle of handles.values()) await handle.close()
if (mismatches) process.exitCode = 1
