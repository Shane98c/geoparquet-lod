// Local comparison server: expose only the demo and the two explicitly supplied files.
// npm --prefix js run demo:compare -- --lod /path/file.parquet --pmtiles /path/file.pmtiles
import { createServer } from 'node:http'
import { createReadStream } from 'node:fs'
import { stat } from 'node:fs/promises'
import { resolve, extname, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parseArgs } from 'node:util'
import { pipeline } from 'node:stream/promises'

const { values } = parseArgs({
  options: {
    lod: { type: 'string' },
    pmtiles: { type: 'string' },
    port: { type: 'string', default: '8094' },
  },
})
if (!values.lod || !values.pmtiles)
  throw new Error('Supply --lod /path/file.parquet --pmtiles /path/file.pmtiles [--port 8094]')
const root = fileURLToPath(new URL('.', import.meta.url))
const files = new Map([
  ['/data/current.parquet', resolve(values.lod)],
  ['/data/baseline.pmtiles', resolve(values.pmtiles)],
])
for (const path of files.values()) {
  if (!(await stat(path)).isFile()) throw new Error(`Not a file: ${path}`)
}
const port = Number(values.port)
if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid port')
const params = new URLSearchParams({
  url: '/data/current.parquet',
  pmtiles: '/data/baseline.pmtiles',
  center: '139.76,35.68',
  zoom: '11',
  basemap: '0',
  demoServer: '1',
  latency: '50',
})
const comparePath = '/demo/compare.html?' + params
const types = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css' }
const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://localhost')
    if (req.method !== 'GET' && req.method !== 'HEAD') {
      res.writeHead(405, { Allow: 'GET, HEAD' }).end()
      return
    }
    if (url.pathname === '/') {
      res.writeHead(302, { Location: comparePath }).end()
      return
    }
    let path = files.get(url.pathname)
    const data = !!path
    if (!path) {
      if (!url.pathname.startsWith('/demo/')) {
        res.writeHead(404).end()
        return
      }
      path = resolve(root, decodeURIComponent(url.pathname.slice('/demo/'.length)))
      if (
        !path.startsWith(root.endsWith(sep) ? root : root + sep) ||
        !(path.endsWith('.html') || path.startsWith(resolve(root, 'dist') + sep))
      ) {
        res.writeHead(404).end()
        return
      }
    }
    const info = await stat(path)
    if (!info.isFile()) {
      res.writeHead(404).end()
      return
    }
    const size = info.size
    let start = 0,
      end = size - 1,
      status = 200
    if (data && req.headers.range) {
      const match = /^bytes=(\d+)-(\d*)$/.exec(req.headers.range)
      start = match ? Number(match[1]) : NaN
      end = match?.[2] ? Math.min(size - 1, Number(match[2])) : size - 1
      if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 0 || start > end) {
        res.writeHead(416, { 'Content-Range': `bytes */${size}` }).end()
        return
      }
      status = 206
    }
    if (data) {
      const delay = Number(url.searchParams.get('latency') ?? 0)
      if (![0, 50, 100].includes(delay)) {
        res.writeHead(400).end('Invalid latency')
        return
      }
      if (delay) await new Promise((done) => setTimeout(done, delay))
    }
    if (res.destroyed) return
    const headers = {
      'Content-Type': types[extname(path)] ?? 'application/octet-stream',
      'Content-Length': end - start + 1,
      'Cache-Control': 'no-store',
      'Accept-Ranges': 'bytes',
    }
    if (status === 206) headers['Content-Range'] = `bytes ${start}-${end}/${size}`
    res.writeHead(status, headers)
    if (req.method === 'HEAD' || size === 0) {
      res.end()
      return
    }
    await pipeline(createReadStream(path, { start, end }), res)
  } catch (error) {
    if (res.destroyed) return
    if (!res.headersSent) res.writeHead(error.code === 'ENOENT' ? 404 : 500)
    res.end()
  }
})
server.listen(port, '127.0.0.1', () => {
  console.log(`Compare: http://127.0.0.1:${port}${comparePath}`)
  console.log('Both data files use the selected per-request delay; bandwidth is not limited.')
})
