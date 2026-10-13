// Build a self-contained static comparison with public dataset defaults.
// First run: npm --prefix js run demo:bundle
import { cp, mkdir, readFile, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parseArgs } from 'node:util'

const { values } = parseArgs({
  options: {
    out: { type: 'string' },
    url: { type: 'string' },
    pmtiles: { type: 'string' },
    cogp: { type: 'string' },
    esri: { type: 'string' },
    release: { type: 'string' },
  },
})
if (!values.out || !values.url || !values.pmtiles || !values.release)
  throw new Error('Supply --out DIRECTORY --url HTTPS_URL --pmtiles HTTPS_URL --release COMMIT')
for (const value of [values.url, values.pmtiles, values.cogp, values.esri].filter(Boolean)) {
  if (new URL(value).protocol !== 'https:') throw new Error('Public datasets must use HTTPS')
}
const defaults = {
  url: values.url,
  pmtiles: values.pmtiles,
  ...(values.cogp ? { cogp: values.cogp } : {}),
  ...(values.esri ? { esri: values.esri } : {}),
  panes: 'lod,pmtiles',
  center: '139.76,35.68',
  zoom: '11',
  basemap: '0',
  style: 'geometry',
  demoServer: '0',
  latency: '0',
}
const root = fileURLToPath(new URL('.', import.meta.url))
const out = resolve(values.out)
if (out === resolve(root)) throw new Error('Use a separate output directory')
await mkdir(out, { recursive: true })
let html = await readFile(new URL('./compare.html', import.meta.url), 'utf8')
const anchor = 'const params = new URLSearchParams(location.search)'
if (html.split(anchor).length !== 2) throw new Error('Comparison bootstrap has changed')
const json = (value) => JSON.stringify(value).replaceAll('<', '\\u003c')
html = html.replace(
  anchor,
  `${anchor}\n      for (const [key, value] of Object.entries(${json(defaults)})) {\n        if (!params.has(key)) params.set(key, value)\n      }\n      document.getElementById('notes').textContent += ${json(` R2 release ${values.release}; real network timing, no artificial delay. Subtype uses the GeoParquet source columns.`)}`,
)
await cp(new URL('./dist/', import.meta.url), resolve(out, 'dist'), { recursive: true })
await writeFile(resolve(out, 'index.html'), html)
await writeFile(resolve(out, 'compare.html'), html)
await writeFile(
  resolve(out, 'release.json'),
  JSON.stringify(
    {
      release: values.release,
      datasets: {
        geoparquet: values.url,
        pmtiles: values.pmtiles,
        cogp: values.cogp,
        esri: values.esri,
      },
      defaultPanes: ['lod', 'pmtiles'],
      artificialLatencyMs: 0,
    },
    null,
    2,
  ) + '\n',
)
await writeFile(
  resolve(out, '_headers'),
  '/*\n  Cache-Control: no-cache\n  Referrer-Policy: strict-origin-when-cross-origin\n',
)
console.log(`Static comparison: ${out}`)
