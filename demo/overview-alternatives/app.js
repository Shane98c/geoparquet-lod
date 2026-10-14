/* global maplibregl */
const status = document.querySelector('#status'),
  pre = document.querySelector('#results')
const grouped = new URLSearchParams(location.search).get('layout') === 'grouped'
const national = new URLSearchParams(location.search).get('dataset') === 'japan'
const compact = new URLSearchParams(location.search).get('layout') === 'compact'
const variants = national ? ['production', 'nested'] : ['packed', 'nested']
const width = Number(new URLSearchParams(location.search).get('width'))
if (width >= 256 && width <= 2048) document.querySelector('#map').style.width = `${width}px`
const dataUrl = (variant) =>
  `/data/${grouped && !national ? 'grouped.' : ''}${variant}.parquet?delay=50`
if (grouped) {
  document.querySelector('h1').textContent = 'Independent overview pages experiment'
  document.querySelector('.note').textContent +=
    ' Independent geometry pages per level; shared source attribute pages. No incidental prefetch.'
}
if (national) {
  document.querySelector('h1').textContent = 'Japan: production vs nested overviews'
  document.querySelector('.note').textContent =
    '53.7 million buildings. Same stored geometry at z4/8/11/14/16, with small differences in boundary copies; between these zooms nested uses the next finer stored level. Production worker/cache with its default two CPU workers; experimental nested reader in one worker. 50 ms per range, no bandwidth cap.'
  document.querySelector('#variant').innerHTML =
    '<option value="nested">Nested Japan</option><option value="production">Production Japan</option>'
  if (compact) {
    document.querySelector('h1').textContent = 'Japan: production vs compact overview rows'
    document.querySelector('#variant option[value="nested"]').textContent = 'Compact Japan'
    document.querySelector('.note').textContent +=
      ' Compact companion Parquet: source-row links, nested coordinates and ordinary height/class columns; original source untouched.'
  }
}
let worker,
  serial = 0,
  pending = new Map(),
  token = 0,
  properties = [],
  currentVariant
let currentUrl
const samples = []
const call = (type, data = {}) =>
  new Promise((resolve, reject) => {
    if (type === 'open') currentUrl = data.url
    if (national && currentVariant === 'production' && type === 'tile')
      data = {
        ...data,
        config: { url: currentUrl, layer: 'features', properties: data.properties },
      }
    const id = ++serial
    pending.set(id, { resolve, reject })
    worker.postMessage({ id, type, ...data })
  })
const startWorker = (variant) => {
  worker?.terminate()
  pending = new Map()
  currentVariant = variant
  worker = new Worker(
    national
      ? variant === 'production'
        ? '/production/worker.js'
        : '/nested-worker.js'
      : '/worker.js',
    { type: 'module' },
  )
  worker.onmessage = ({ data }) => {
    const task = pending.get(data.id)
    if (!task) return
    pending.delete(data.id)
    if (data.error) task.reject(new Error(`${data.error}\n${data.stack ?? ''}`))
    else if (national && currentVariant === 'production') {
      if (data.stats) {
        const entries = Object.values(data.stats)
        task.resolve({
          requests: entries.reduce((n, s) => n + s.requests, 0),
          bytes: entries.reduce((n, s) => n + s.bytes, 0),
          decodeMs: null,
          mvtMs: null,
          tiles: data.cache?.tiles,
          decodedEntries: data.cache?.decodes,
          compressedCacheBytes: null,
          productionVisibleCacheBytes: data.cache?.bytes,
          trace: [],
        })
      } else task.resolve(data.data ? { data: data.data } : {})
    } else task.resolve(data.result)
  }
  worker.onerror = (event) => {
    status.textContent = `Worker error: ${event.message}`
  }
}
maplibregl.addProtocol('experiment', async (params) => {
  const url = new URL(params.url),
    [, , z, x, y] = url.pathname.split('/')
  if (url.hostname !== currentVariant) throw new Error('Stale experiment source')
  const result = await call('tile', { z: Number(z), x: Number(x), y: Number(y), properties })
  return { data: result.data }
})
const map = new maplibregl.Map({
  container: 'map',
  center: [139.745, 35.665],
  zoom: 14,
  attributionControl: false,
  style: {
    version: 8,
    transition: { duration: 0, delay: 0 },
    sources: {},
    layers: [{ id: 'background', type: 'background', paint: { 'background-color': '#e9ece8' } }],
  },
  fadeDuration: 0,
  maxZoom: 16,
  minZoom: national ? 4 : 8,
  renderWorldCopies: false,
})
map.addControl(new maplibregl.NavigationControl())
const errors = []
map.on('error', (event) => {
  errors.push(String(event.error))
  status.textContent = `Map error: ${event.error}`
})
function setSource(styled) {
  properties = styled ? ['height', 'class'] : []
  map.setStyle({
    version: 8,
    transition: { duration: 0, delay: 0 },
    sources: {
      buildings: {
        type: 'vector',
        tiles: [`experiment://${currentVariant}/${++token}/{z}/{x}/{y}`],
        minzoom: national ? 4 : 8,
        maxzoom: 16,
      },
    },
    layers: [
      { id: 'background', type: 'background', paint: { 'background-color': '#e9ece8' } },
      {
        id: 'buildings',
        type: 'fill',
        source: 'buildings',
        'source-layer': 'features',
        paint: {
          'fill-color': styled
            ? [
                'interpolate',
                ['linear'],
                ['to-number', ['get', 'height'], 0],
                0,
                '#417d9b',
                20,
                '#d29a55',
                80,
                '#ba4c42',
              ]
            : '#417d9b',
          'fill-opacity': 0.85,
        },
      },
    ],
  })
}
const ready = () =>
  new Promise((resolve, reject) => {
    const timer = setTimeout(
      () => {
        map.off('idle', idle)
        reject(new Error('Map idle timeout'))
      },
      national ? 120000 : 30000,
    )
    const idle = () => {
      clearTimeout(timer)
      resolve()
    }
    map.once('idle', idle)
  })
const zero = () => ({ requests: 0, bytes: 0, decodeMs: 0, mvtMs: 0, tiles: 0, trace: [] })
const phases = national
  ? [
      ['cold Tokyo z11', 11, [139.76, 35.68], false, true],
      ['style Tokyo z11', 11, [139.76, 35.68], true, true],
      ['Tokyo z14 styled', 14, [139.76, 35.68], true, false],
      ['Tokyo east z14 styled', 14, [139.78, 35.68], true, false],
      ['Tokyo z16 styled', 16, [139.76, 35.68], true, false],
      ['Tokyo z14 revisit', 14, [139.76, 35.68], true, false],
      ['Osaka z14 styled', 14, [135.5, 34.69], true, false],
      ['rural z14 styled', 14, [140.12, 36.21], true, false],
      ['Sapporo z11 styled', 11, [141.35, 43.06], true, false],
      ['Tokyo z8 styled', 8, [139.76, 35.68], true, false],
      ['Japan z4 styled', 4, [137.5, 37], true, false],
      ['Japan z4 warm', 4, [137.5, 37], true, true],
      ['cold Japan z4 geometry', 4, [137.5, 37], false, true],
      ['cold Japan z4 styled', 4, [137.5, 37], true, true],
    ]
  : [
      ['cold city z14', 14, [139.745, 35.665], false, true],
      ['pan east z14', 14, [139.755, 35.665], false, false],
      ['zoom city z16', 16, [139.745, 35.665], false, false],
      ['add height/class z16', 16, [139.745, 35.665], true, true],
      ['revisit city z14 styled', 14, [139.745, 35.665], true, false],
      ['repeat city z14 styled', 14, [139.745, 35.665], true, true],
      ['pan periphery z14 styled', 14, [139.495, 35.705], true, false],
      ['zoom city z11 styled', 11, [139.745, 35.665], true, false],
      ['zoom city z8 styled', 8, [139.745, 35.665], true, false],
    ]
async function show(variant) {
  startWorker(variant)
  status.textContent = `Opening ${variant}…`
  await call('open', { url: dataUrl(variant) })
  const wait = ready()
  setSource(true)
  map.jumpTo({ center: [139.745, 35.665], zoom: 14 })
  await wait
  status.textContent = `${variant}: map ready. Pan, zoom, or run the matched benchmark.`
}
async function benchmark() {
  document.querySelector('#run').disabled = true
  document.querySelector('#show').disabled = true
  samples.length = 0
  errors.length = 0
  try {
    for (let run = 0; run < 3; run++)
      for (const variant of run % 2 ? [...variants].reverse() : variants) {
        startWorker(variant)
        let previous = zero()
        for (const [phase, zoom, center, styled, refresh] of phases) {
          if (phase.startsWith('cold Japan')) {
            startWorker(variant)
            previous = zero()
          }
          status.textContent = `Run ${run + 1}/3 · ${variant} · ${phase}`
          const start = performance.now()
          if (phase.startsWith('cold'))
            await call('open', { url: `${dataUrl(variant)}&run=${run}` })
          const wait = ready()
          if (refresh) setSource(styled)
          map.jumpTo({ center, zoom })
          await wait
          // Two animation frames ensure the completed map has had a paint opportunity.
          await new Promise((resolve) =>
            requestAnimationFrame(() => requestAnimationFrame(resolve)),
          )
          const visibleReadyMs = performance.now() - start,
            now = await call('stats')
          const rendered = map.queryRenderedFeatures({ layers: ['buildings'] })
          const row = {
            run,
            variant,
            phase,
            visibleReadyMs,
            visibleUniqueIds: new Set(rendered.map((f) => f.id)).size,
            requests: now.requests - previous.requests,
            bytes: now.bytes - previous.bytes,
            decodeMs: now.decodeMs - previous.decodeMs,
            mvtMs: now.mvtMs - previous.mvtMs,
            tiles: now.tiles - previous.tiles,
            compressedCacheBytes: now.compressedCacheBytes,
            decodedEntries: now.decodedEntries,
          }
          if (national && variant === 'production') {
            row.decodeMs = null
            row.mvtMs = null
          }
          if (national && run === 0)
            row.visibleIds = [...new Set(rendered.map((f) => f.id))].sort((a, b) => a - b)
          if (now.compactReader) {
            row.compactReader = now.compactReader
            row.geometryCacheBytes = now.geometryCacheBytes
            row.leafCacheBytes = now.leafCacheBytes
          }
          if (national) {
            row.directoryShards = now.directoryShards
            row.indexedPages = now.indexedPages
          }
          if (run === 0) row.ranges = now.trace.slice(previous.trace.length)
          samples.push(row)
          previous = now
          // Keep the live DOM small: full ID sets/range traces remain in saved evidence.
          const { visibleIds, ranges, ...preview } = row
          pre.textContent = JSON.stringify(
            { status: 'running', latest: preview, completed: samples.length },
            null,
            2,
          )
        }
      }
    const median = (values) => [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)]
    const summary = phases.flatMap(([phase]) =>
      variants.map((variant) => {
        const rows = samples.filter((r) => r.phase === phase && r.variant === variant)
        return {
          phase,
          variant,
          ...Object.fromEntries(
            ['visibleReadyMs', 'bytes', 'requests', 'decodeMs', 'mvtMs', 'visibleUniqueIds'].map(
              (k) => [k, median(rows.map((r) => r[k]))],
            ),
          ),
        }
      }),
    )
    window.__experimentResults = {
      layout: national
        ? compact
          ? 'japan-compact'
          : 'japan-sharded'
        : grouped
          ? 'independent-pages'
          : 'shared-pages',
      runs: 3,
      delayMs: 50,
      browser: navigator.userAgent,
      viewport: [map.getCanvas().clientWidth, map.getCanvas().clientHeight],
      timing:
        'Visible-ready includes open/HTTP/decode/MVT/MapLibre idle + two animation frames; decode/MVT are worker CPU sums',
      cache: national
        ? {
            production:
              'Actual production defaults, including 32MiB / 64-entry visible tile cache plus reader caches',
            nested: samples.some((s) => s.compactReader)
              ? '32MiB compressed; 32MiB estimated retained geometry + 8MiB leaves; in-flight work separate; 8 active fetches'
              : '32MiB compressed leaf cache; 512 decoded entries (not a heap byte bound); 8 active fetches',
          }
        : '32MiB compressed leaf cache; 512 decoded entries (entry limit, not heap byte bound)',
      errors,
      summary,
      samples,
    }
    pre.textContent = JSON.stringify(
      {
        ...window.__experimentResults,
        samples: `${samples.length} samples; full IDs and ranges in Save results`,
      },
      null,
      2,
    )
    document.querySelector('#summary').innerHTML =
      '<table><tr><th>Phase</th><th>Variant</th><th>Visible ms</th><th>KB</th><th>Requests</th><th>Decode ms</th><th>MVT ms</th></tr>' +
      summary
        .map(
          (r) =>
            `<tr><td>${r.phase}</td><td>${r.variant}</td><td>${r.visibleReadyMs.toFixed(0)}</td><td>${(r.bytes / 1000).toFixed(1)}</td><td>${r.requests}</td><td>${r.decodeMs == null ? '—' : r.decodeMs.toFixed(1)}</td><td>${r.mvtMs == null ? '—' : r.mvtMs.toFixed(1)}</td></tr>`,
        )
        .join('') +
      '</table>'
    // Save evidence locally via the experiment-only server; no external transmission.
    await fetch(`/results${grouped ? '?layout=grouped' : ''}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(window.__experimentResults),
    })
    document.querySelector('#download').disabled = false
    status.textContent = `Complete: ${samples.length} samples, ${errors.length} map errors. Results saved locally.`
  } catch (error) {
    status.textContent = `Benchmark failed: ${error.stack}`
    window.__experimentError = String(error)
  } finally {
    document.querySelector('#run').disabled = false
    document.querySelector('#show').disabled = false
  }
}
document.querySelector('#run').onclick = benchmark
document.querySelector('#show').onclick = () =>
  show(document.querySelector('#variant').value).catch((e) => {
    status.textContent = String(e)
  })
document.querySelector('#download').onclick = () => {
  const link = document.createElement('a')
  link.href = URL.createObjectURL(
    new Blob([JSON.stringify(window.__experimentResults, null, 2)], { type: 'application/json' }),
  )
  link.download = 'overview-browser-results.json'
  link.click()
}
window.__experiment = { map, show, benchmark, phases }
const initialVariant = new URLSearchParams(location.search).get('variant') ?? 'nested'
document.querySelector('#variant').value = variants.includes(initialVariant)
  ? initialVariant
  : 'nested'
map.once('load', () =>
  show(document.querySelector('#variant').value).catch((error) => {
    status.textContent = String(error)
  }),
)
