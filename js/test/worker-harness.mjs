// Run the actual browser worker module against a deterministic range server.
// The adapter only supplies the Web Worker transport and fetch surface in Node.
import { parentPort, workerData } from 'node:worker_threads'
import { readFile } from 'node:fs/promises'

const file = await readFile(workerData.path)
globalThis.self = globalThis
globalThis.addEventListener = (type, callback) => {
  if (type === 'message') parentPort.on('message', (data) => callback({ data }))
}
globalThis.postMessage = (data, transfer) => parentPort.postMessage(data, transfer)
globalThis.fetch = async (_url, options) => {
  if (options.method === 'HEAD')
    return new Response(null, { headers: { 'Content-Length': String(file.length) } })
  const [, a, b] = options.headers.Range.match(/bytes=(\d+)-(\d+)/)
  const start = Number(a),
    end = Math.min(Number(b) + 1, file.length)
  return new Response(file.subarray(start, end), {
    status: 206,
    headers: {
      'Content-Length': String(end - start),
      'Content-Range': `bytes ${start}-${end - 1}/${file.length}`,
    },
  })
}
await import('../dist/worker.js')
parentPort.postMessage({ ready: true })
