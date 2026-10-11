// The same tiles from four files of the same 155.6 million US buildings, read with range
// requests: this layout (through page indexes, and through its tile directory when
// TILEDIR_URL is set), COGP, PMTiles and a plain Hilbert-sorted GeoParquet. Prints markdown
// tables of the open cost, each tile cold and then warm on the same reader, and short pans.
//   node test/compare.mjs [z,lon,lat ...]     RUNS=3 by default; median time is reported
import { parquetMetadataAsync } from 'hyparquet'
import { CogpReader, toMvt } from '@cogp/reader'
import { PMTiles } from 'pmtiles'
import { VectorTile } from '@mapbox/vector-tile'
import { PbfReader } from 'pbf'
import {
  LodReader,
  TileDirectory,
  httpSource,
  coalescing,
  renderTile,
  tileBounds,
} from '../dist/index.js'

const FILES = {
  // LOD_URL overrides the lod file, e.g. a local rewrite served with range support
  lod:
    process.env.LOD_URL ??
    'https://carbonplan-ocr.s3.us-west-2.amazonaws.com/ocr-explore/geoparquet-overviews/lod/buildings.parquet',
  cogp: 'https://carbonplan-ocr.s3.us-west-2.amazonaws.com/ocr-explore/geoparquet-overviews/cogp/buildings.cogp.parquet',
  pmtiles:
    'https://s3.us-west-2.amazonaws.com/us-west-2.opendata.source.coop/carbonplan/carbonplan-ocr/output/fire-risk/vector/production/v1.1.0/pmtiles/buildings.pmtiles',
  sorted:
    'https://carbonplan-ocr.s3.us-west-2.amazonaws.com/ocr-explore/geoparquet-overviews/buildings_sorted.parquet',
}
// ATTRS overrides the carried attributes to request, e.g. height,class for the Denver file
const ATTRS = (process.env.ATTRS ?? 'rps_2011,rps_2047').split(',')
const RUNS = Number(process.env.RUNS ?? 3)
const DEFAULT_TILES = [
  [3, -98.5, 39.5, 'national'],
  [5, -122.27, 37.8, 'regional, Bay Area'],
  [6, -118.25, 34.05, 'regional, Southern California'],
  [8, -104.99, 39.74, 'metro, Denver'],
  [9, -84.39, 33.75, 'metro, Atlanta'],
  [11, -104.99, 39.74, 'city, Denver'],
  [12, -84.15, 33.95, 'suburbs, Atlanta'],
  [12, -117.23, 38.07, 'desert, Tonopah NV'],
  [13, -122.42, 37.78, 'downtown, San Francisco'],
  [13, -118.25, 34.05, 'downtown, Los Angeles'],
  [13, -99.64, 41.4, 'rural, Broken Bow NE'],
  [13, -101.3, 42.1, 'empty, Nebraska Sandhills'],
  [14, -87.72, 41.8, 'row-group edge, Chicago'],
  [15, -73.985, 40.758, 'street, Manhattan'],
  [15, -104.88, 39.62, 'street, Denver suburbs'],
  [16, -122.42, 37.78, 'street, San Francisco'],
  [17, -122.42, 37.78, 'beyond finest LOD, San Francisco'],
]
const tiles =
  process.argv.length > 2
    ? process.argv.slice(2).map((s) => {
        const [z, lon, lat] = s.split(',').map(Number)
        return [z, lon, lat, s]
      })
    : DEFAULT_TILES

// Every implementation goes through global fetch. Requests count every response;
// bytes count only bodies that were fetched: 206, or 200 to a request other than HEAD.
let reqs = 0,
  bytes = 0
const inflight = new Set()
const realFetch = globalThis.fetch
globalThis.fetch = (u, init) => {
  const method = (init?.method ?? (u instanceof Request ? u.method : 'GET')).toUpperCase()
  const p = realFetch(u, init).then((r) => {
    reqs++
    if (r.status === 206 || (r.status === 200 && method !== 'HEAD'))
      bytes += Number(r.headers.get('content-length') || 0)
    return r
  })
  inflight.add(p)
  p.catch(() => undefined).finally(() => inflight.delete(p))
  return p
}
const reset = () => {
  reqs = 0
  bytes = 0
}
// readers start some reads without awaiting them (page-index prefetch); wait for those
const settle = async () => {
  while (inflight.size) {
    await Promise.allSettled([...inflight])
    await new Promise((r) => setTimeout(r, 0))
  }
}

const tileXY = (z, lon, lat) => {
  const n = 2 ** z
  return [
    Math.floor(((lon + 180) / 360) * n),
    Math.floor(
      ((1 -
        Math.log(Math.tan((lat * Math.PI) / 180) + 1 / Math.cos((lat * Math.PI) / 180)) / Math.PI) /
        2) *
        n,
    ),
  ]
}
const degreesPerPixel = (z) => 40075016.68557849 / (512 * 2 ** z) / 111320

// Features in an MVT. For a parent tile standing in for a finer one, only features
// whose geometry bbox meets the child's square are counted, as a client would draw.
const countFeatures = (buf, child) => {
  const vt = new VectorTile(new PbfReader(new Uint8Array(buf)))
  let n = 0
  for (const layer of Object.values(vt.layers)) {
    for (let i = 0; i < layer.length; i++) {
      if (!child) {
        n++
        continue
      }
      const f = layer.feature(i)
      const s = f.extent / child.scale
      const [x0, y0] = [child.dx * s, child.dy * s]
      const [bx0, by0, bx1, by1] = f.bbox()
      if (bx1 >= x0 && bx0 <= x0 + s && by1 >= y0 && by0 <= y0 + s) n++
    }
  }
  return n
}

const ONLY = process.env.IMPLS?.split(',')
// TILEDIR_URL adds a `lod_tiledir` implementation: the same file read through its tile
// directory sidecar (`<file>.tiledir.parquet`), whose open counts toward the reader's open
const TILEDIR = process.env.TILEDIR_URL
const impls = {
  lod: {
    open: async () => LodReader.open(coalescing(await httpSource([FILES.lod]))),
    async tile(r, z, x, y) {
      const mvt = await renderTile(r, z, x, y, { layer: 'b', properties: ATTRS })
      return {
        mvt,
        note: z > r.finestZoom ? 'primary WKB geometry' : `LOD ${r.lodForZoom(z).name}`,
      }
    },
  },
  ...(TILEDIR
    ? {
        lod_tiledir: {
          async open() {
            const r = await LodReader.open(coalescing(await httpSource([FILES.lod])))
            // a directory that fails to open or is bypassed must fail the run, not fall back
            const td = await TileDirectory.open(r, TILEDIR)
            const read = td.readLodByTile.bind(td)
            td.readLodByTile = async (...args) => {
              const rows = await read(...args)
              td.used = true
              return rows
            }
            return { r, td }
          },
          async tile({ r, td }, z, x, y) {
            td.used = false
            const mvt = await renderTile(r, z, x, y, {
              layer: 'b',
              properties: ATTRS,
              tiledir: td,
            })
            const covered = z <= r.finestZoom && td.covers(r.lodForZoom(z), z)
            if (covered && !td.used)
              throw new Error('tile directory silently fell back to page indexes')
            const lod = `LOD ${r.lodForZoom(z).name}`
            return {
              mvt,
              note:
                z > r.finestZoom
                  ? 'primary WKB geometry'
                  : covered
                    ? `directory, ${lod}`
                    : `page indexes (zoom not in directory), ${lod}`,
            }
          },
        },
      }
    : {}),
  cogp: {
    open: () => CogpReader.open(FILES.cogp),
    async tile(r, z, x, y) {
      const level = r.selectLevel(degreesPerPixel(z))
      const b = await r.read({
        maxLevel: level,
        bbox: tileBounds(z, x, y),
        useOverview: true,
        columns: [...ATTRS, 'geometry'],
      })
      const columns = Object.fromEntries(ATTRS.map((a) => [a, b.columns[a]]))
      const mvt = toMvt(
        { geometry: b.geometry, rowIndex: b.rowIndex, columns },
        { z, x, y, layer: 'b' },
      )
      return { mvt, note: `level ${level}, ${b.length} rows read` }
    },
  },
  pmtiles: {
    async open() {
      const p = new PMTiles(FILES.pmtiles)
      await p.getHeader()
      return p
    },
    async tile(p, z, x, y) {
      const h = await p.getHeader()
      if (z < h.minZoom)
        return { mvt: null, skipped: true, note: `no tile: archive minzoom is ${h.minZoom}` }
      let zz = z,
        xx = x,
        yy = y
      while (zz > h.maxZoom) {
        zz--
        xx = Math.floor(xx / 2)
        yy = Math.floor(yy / 2)
      }
      const t = await p.getZxy(zz, xx, yy)
      if (!t) return { mvt: null, note: 'no tile: empty' }
      const scale = 2 ** (z - zz)
      const child = zz === z ? null : { scale, dx: x - xx * scale, dy: y - yy * scale }
      return { mvt: t.data, child, note: child ? `overzoomed from z${zz}` : '' }
    },
  },
  sorted: {
    async open() {
      const head = await fetch(FILES.sorted, { method: 'HEAD' })
      const byteLength = Number(head.headers.get('content-length'))
      const file = {
        byteLength,
        slice: async (a, b) =>
          (await fetch(FILES.sorted, { headers: { Range: `bytes=${a}-${b - 1}` } })).arrayBuffer(),
      }
      return parquetMetadataAsync(file)
    },
    // No LOD and no page index: every row group whose bbox statistics touch the tile is
    // read for geometry and the two attributes (adjacent, so one range), two requests per
    // row group. Computed from the footer; transferring it would be hundreds of MB.
    async tile(md, z, x, y) {
      const bbox = tileBounds(z, x, y)
      let need = 0,
        groups = 0,
        rows = 0
      for (const rg of md.row_groups) {
        const col = (n) =>
          rg.columns.find((c) => c.meta_data.path_in_schema.join('.') === n).meta_data
        const s = (n) => col(n).statistics
        if (
          s('bbox.xmin').min_value > bbox[2] ||
          s('bbox.xmax').max_value < bbox[0] ||
          s('bbox.ymin').min_value > bbox[3] ||
          s('bbox.ymax').max_value < bbox[1]
        )
          continue
        groups++
        rows += Number(rg.num_rows)
        for (const n of ['geometry', ...ATTRS]) need += Number(col(n).total_compressed_size)
      }
      return {
        mvt: null,
        computed: { bytes: need, reqs: groups * 2 },
        note: `computed: ${groups} row group${groups === 1 ? '' : 's'}, ${rows.toLocaleString('en-US')} rows`,
      }
    },
  },
}

const median = (xs) => {
  const s = [...xs].sort((a, b) => a - b)
  return s[Math.floor(s.length / 2)]
}
const mb = (b) => `${(b / 1e6).toFixed(2)} MB`
const kb = (b) => (b >= 1e6 ? mb(b) : `${(b / 1e3).toFixed(0)} KB`)
const span = (xs) =>
  Math.min(...xs) === Math.max(...xs) ? null : [Math.min(...xs), Math.max(...xs)]

// lod and lod_tiledir must decode to the same features: ids, geometry and properties
const canonical = (buf) => {
  const vt = new VectorTile(new PbfReader(new Uint8Array(buf)))
  return Object.entries(vt.layers)
    .flatMap(([layer, value]) =>
      Array.from({ length: value.length }, (_, i) => {
        const f = value.feature(i)
        return JSON.stringify([layer, f.id, f.type, f.properties, f.loadGeometry()])
      }),
    )
    .sort()
}
const decodedByTile = new Map()
let mismatches = 0
const checkEqual = (key, name, mvt) => {
  if (name !== 'lod' && name !== 'lod_tiledir') return
  if (!decodedByTile.has(key)) decodedByTile.set(key, {})
  const seen = decodedByTile.get(key)
  seen[name] = mvt ? canonical(mvt) : null
  if (!('lod' in seen && 'lod_tiledir' in seen)) return
  const [a, b] = [seen.lod, seen.lod_tiledir]
  delete decodedByTile.get(key).lod
  delete decodedByTile.get(key).lod_tiledir
  if (JSON.stringify(a) === JSON.stringify(b)) return
  mismatches++
  const sa = new Set(a ?? [])
  const sb = new Set(b ?? [])
  console.error(
    `MISMATCH ${key}: ${a?.length} page-index features, ${b?.length} directory features, ` +
      `${[...sa].filter((f) => !sb.has(f)).length} only in page index, ` +
      `${[...sb].filter((f) => !sa.has(f)).length} only in directory`,
  )
}

// Short pans on one reader opened beforehand: four tiles eastward from the given one.
const PANS = [
  [13, -122.42, 37.78, 'z13 San Francisco, 4 tiles east'],
  [15, -73.985, 40.758, 'z15 Manhattan, 4 tiles east'],
]

const results = new Map()
const opens = Object.fromEntries(Object.keys(impls).map((k) => [k, []]))
const pans = new Map()
const record = (map, key, value) => {
  if (!map.has(key)) map.set(key, [])
  map.get(key).push(value)
}
const measure = async (fn) => {
  await settle()
  reset()
  const t = performance.now()
  const value = await fn()
  const ms = performance.now() - t
  await settle()
  return { value, bytes, reqs, ms }
}
const active = Object.entries(impls).filter(([name]) => !ONLY || ONLY.includes(name))

for (let run = 0; run < RUNS; run++) {
  for (const [z, lon, lat, label] of tiles) {
    const [x, y] = tileXY(z, lon, lat)
    for (const [name, impl] of active) {
      const key = `${z}|${label}|${name}`
      try {
        const open = await measure(() => impl.open())
        opens[name].push({ bytes: open.bytes, reqs: open.reqs, ms: open.ms })
        const state = open.value
        const cold = await measure(() => impl.tile(state, z, x, y))
        const r = cold.value
        const c = r.computed ?? { bytes: cold.bytes, reqs: cold.reqs }
        checkEqual(`${run}|${z}/${x}/${y}`, name, r.mvt)
        let warm = null
        if (r.mvt) {
          const again = await measure(() => impl.tile(state, z, x, y))
          warm = { bytes: again.bytes, reqs: again.reqs, ms: again.ms }
          checkEqual(`${run}|warm|${z}/${x}/${y}`, name, again.value.mvt)
        }
        record(results, key, {
          bytes: c.bytes,
          reqs: c.reqs,
          ms: r.computed || r.skipped ? null : cold.ms,
          decoded: r.mvt ? r.mvt.byteLength : null,
          features: r.mvt ? countFeatures(r.mvt, r.child) : null,
          warm,
          none: Boolean(r.skipped),
          note: r.note ?? '',
        })
      } catch (e) {
        await settle()
        record(results, key, { error: String(e?.message ?? e).slice(0, 100) })
      }
      process.stderr.write(`run ${run + 1}/${RUNS} z${z} ${label} ${name}\n`)
    }
  }
  for (const [z, lon, lat, label] of PANS) {
    const [x0, y] = tileXY(z, lon, lat)
    for (const [name, impl] of active) {
      if (name === 'sorted') continue
      try {
        const state = (await measure(() => impl.open())).value
        let features = 0
        const pan = await measure(async () => {
          for (let i = 0; i < 4; i++) {
            const x = (x0 + i) % 2 ** z
            const r = await impl.tile(state, z, x, y)
            checkEqual(`${run}|pan|${z}/${x}/${y}`, name, r.mvt)
            if (r.mvt) features += countFeatures(r.mvt, r.child)
          }
        })
        record(pans, `${label}|${name}`, { bytes: pan.bytes, reqs: pan.reqs, ms: pan.ms, features })
      } catch (e) {
        await settle()
        record(pans, `${label}|${name}`, { error: String(e?.message ?? e).slice(0, 100) })
      }
      process.stderr.write(`run ${run + 1}/${RUNS} pan ${label} ${name}\n`)
    }
  }
}

const cell = (b, q, ms) => `${mb(b)}, ${q} req${ms == null ? '' : `, ${Math.round(ms)} ms`}`

console.log(`Open, once per map session (median of ${RUNS * tiles.length} cold opens):\n`)
console.log('| implementation | transferred | requests | time |')
console.log('| --- | --- | --- | --- |')
for (const [name, xs] of Object.entries(opens)) {
  if (!xs.length) continue
  const b = span(xs.map((o) => o.bytes))
  console.log(
    `| ${name} | ${b ? `${mb(b[0])} to ${mb(b[1])}` : mb(xs[0].bytes)} | ${median(xs.map((o) => o.reqs))} | ${Math.round(median(xs.map((o) => o.ms)))} ms |`,
  )
}

const rowsFor = (pred) => [...results].filter(([key, runs]) => pred(key.split('|')[2], runs))
const computed = (runs) => runs.some((r) => r.note?.startsWith('computed'))

console.log(
  `\nPer tile after open: first render on a fresh reader (cold), then the same tile again on that reader (warm); median time of ${RUNS} runs:\n`,
)
console.log('| tile | impl | cold | warm, same tile | decoded MVT | features | note |')
console.log('| --- | --- | --- | --- | --- | --- | --- |')
for (const [key, runs] of rowsFor((_, runs) => !computed(runs))) {
  const [z, label, name] = key.split('|')
  const tile = `z${z} ${label}`
  const ok = runs.filter((r) => !r.error)
  const errors = runs.filter((r) => r.error)
  if (!ok.length) {
    console.log(
      `| ${tile} | ${name} | | | | | error (${errors.length}/${runs.length}): ${errors[0].error} |`,
    )
    continue
  }
  const first = ok[0]
  const b = span(ok.map((r) => r.bytes)),
    q = span(ok.map((r) => r.reqs))
  const notes = [first.note]
  if (b || q)
    notes.push(
      `cold varies across runs: ${b ? `${mb(b[0])}-${mb(b[1])}` : ''} ${q ? `${q[0]}-${q[1]} req` : ''}`.trim(),
    )
  if (errors.length) notes.push(`error in ${errors.length}/${runs.length} runs: ${errors[0].error}`)
  const coldCell = first.none
    ? ''
    : cell(first.bytes, first.reqs, first.ms == null ? null : median(ok.map((r) => r.ms)))
  const warmCell = first.warm
    ? cell(first.warm.bytes, first.warm.reqs, median(ok.map((r) => r.warm.ms)))
    : ''
  console.log(
    `| ${tile} | ${name} | ${coldCell} | ${warmCell} | ${first.decoded == null ? '' : kb(first.decoded)} | ${first.features == null ? '' : first.features.toLocaleString('en-US')} | ${notes.filter(Boolean).join('; ')} |`,
  )
}

console.log(
  `\nPans on one reader after open, four tiles eastward, totals (median time of ${RUNS} runs):\n`,
)
console.log('| pan | impl | transferred | requests | time | features |')
console.log('| --- | --- | --- | --- | --- | --- |')
for (const [key, runs] of pans) {
  const [label, name] = key.split('|')
  const ok = runs.filter((r) => !r.error)
  if (!ok.length) {
    console.log(`| ${label} | ${name} | | | | error: ${runs[0].error} |`)
    continue
  }
  console.log(
    `| ${label} | ${name} | ${mb(ok[0].bytes)} | ${ok[0].reqs} | ${Math.round(median(ok.map((r) => r.ms)))} ms | ${ok[0].features.toLocaleString('en-US')} |`,
  )
}

console.log(
  '\nPlain Hilbert-sorted GeoParquet, computed from the footer (geometry and the two attributes of every row group whose bbox statistics touch the tile):\n',
)
console.log('| tile | would transfer | requests | row groups and rows |')
console.log('| --- | --- | --- | --- |')
for (const [key, runs] of rowsFor((_, runs) => computed(runs))) {
  const [z, label] = key.split('|')
  const r = runs.find((x) => !x.error)
  if (!r) {
    console.log(`| z${z} ${label} | | | error: ${runs[0].error} |`)
    continue
  }
  console.log(
    `| z${z} ${label} | ${mb(r.bytes)} | ${r.reqs} | ${r.note.replace('computed: ', '')} |`,
  )
}
const errors = [...results.values(), ...pans.values()].flat().filter((r) => r.error).length
if (mismatches || errors) {
  console.error(`${mismatches} feature mismatches, ${errors} errors`)
  process.exitCode = 1
}
