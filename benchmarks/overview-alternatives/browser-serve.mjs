// Isolated, allowlisted prototype server. Existing demos/servers are untouched.
import { createServer } from 'node:http'
import { open, readFile, writeFile, stat } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
const national = process.argv.includes('--japan')
const compact = process.argv.includes('--compact')
const directoryArg = process.argv.indexOf('--directory')
const base = fileURLToPath(new URL('../../', import.meta.url)),
  scratch =
    directoryArg >= 0
      ? process.argv[directoryArg + 1]
      : compact
        ? '/private/tmp/overview-japan-compact'
        : national
          ? '/private/tmp/overview-japan'
          : '/private/tmp/overview-alternatives'
const files = new Map([
  ['/', `${base}demo/overview-alternatives/index.html`],
  ['/app.js', `${base}demo/overview-alternatives/app.js`],
  ['/worker.js', `${scratch}/worker.js`],
  ['/data/packed.parquet', `${scratch}/browser.packed.parquet`],
  ['/data/nested.parquet', `${scratch}/browser.nested.parquet`],
  ['/data/grouped.packed.parquet', `${scratch}/browser.grouped.packed.parquet`],
  ['/data/grouped.nested.parquet', `${scratch}/browser.grouped.nested.parquet`],
])
if (national) {
  files.set('/nested-worker.js', `${scratch}/nested-worker.js`)
  files.set('/production/worker.js', '/private/tmp/overview-japan/production/worker.js')
  files.set('/data/nested.parquet', `${scratch}/japan.${compact ? 'compact' : 'nested'}.parquet`)
  files.set('/data/production.parquet', '/private/tmp/japan.tippecanoe-density.spatial.parquet')
}
createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://localhost')
    res.setHeader('Cache-Control', 'no-store')
    res.setHeader('Access-Control-Allow-Origin', '*')
    if (url.pathname === '/results' && req.method === 'POST') {
      let text = ''
      for await (const chunk of req) {
        text += chunk
        if (text.length > (national ? 16e6 : 4e6)) throw Error('Result too large')
      }
      const result = JSON.parse(text),
        suffix = url.searchParams.get('layout') === 'grouped' ? '-grouped' : ''
      await writeFile(
        `${scratch}/browser-results${suffix}.json`,
        JSON.stringify(result, null, 2) + '\n',
      )
      res.end('saved')
      return
    }
    const path = files.get(url.pathname)
    if (!path) {
      res.writeHead(404)
      res.end()
      return
    }
    const metadata = await stat(path),
      range = /^bytes=(\d+)-(\d+)$/.exec(req.headers.range ?? '')
    res.setHeader(
      'Content-Type',
      path.endsWith('.html')
        ? 'text/html'
        : path.endsWith('.js')
          ? 'text/javascript'
          : 'application/octet-stream',
    )
    if (range) {
      const start = Number(range[1]),
        end = Number(range[2])
      if (start < 0 || end < start || end >= metadata.size) throw Error('Invalid range')
      const delay = Math.min(200, Number(url.searchParams.get('delay') ?? 0))
      if (delay) await new Promise((r) => setTimeout(r, delay))
      const file = await open(path),
        data = Buffer.alloc(end - start + 1)
      await file.read(data, 0, data.length, start)
      await file.close()
      res.writeHead(206, {
        'Content-Range': `bytes ${start}-${end}/${metadata.size}`,
        'Content-Length': data.length,
        'Accept-Ranges': 'bytes',
      })
      res.end(data)
    } else {
      res.setHeader('Content-Length', metadata.size)
      res.end(await readFile(path))
    }
  } catch (error) {
    res.writeHead(500)
    res.end(String(error))
  }
}).listen(Number(process.argv[2] ?? 8103), '127.0.0.1', () =>
  console.log(`Overview prototype: http://localhost:${process.argv[2] ?? 8103}/`),
)
