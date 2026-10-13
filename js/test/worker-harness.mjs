// Run actual browser worker modules with Node's worker transport and range I/O.
import { parentPort, workerData, Worker as NodeWorker } from 'node:worker_threads'
import { open } from 'node:fs/promises'

globalThis.self = globalThis
globalThis.addEventListener = (type, callback) => {
  if (type === 'message') parentPort.on('message', (data) => callback({ data }))
}
globalThis.postMessage = (data, transfer) => parentPort.postMessage(data, transfer)
globalThis.Worker = class {
  constructor(url) {
    this.worker = new NodeWorker(new URL('./worker-harness.mjs', import.meta.url), {
      workerData: { entry: url.href },
    })
  }
  postMessage(data, transfer) {
    this.worker.postMessage(data, transfer)
  }
  terminate() {
    return this.worker.terminate()
  }
  addEventListener(type, callback) {
    this.worker.on(type, (data) => callback(type === 'message' ? { data } : data))
  }
}
if (workerData.entry) {
  await import(workerData.entry)
} else {
  const file = await open(workerData.path)
  const length = (await file.stat()).size
  globalThis.fetch = async (_url, options) => {
    if (workerData.delay) await new Promise((resolve) => setTimeout(resolve, workerData.delay))
    if (options.method === 'HEAD')
      return new Response(null, { headers: { 'Content-Length': String(length) } })
    const [, a, b] = options.headers.Range.match(/bytes=(\d+)-(\d+)/)
    const start = Number(a),
      end = Math.min(Number(b) + 1, length)
    const data = new Uint8Array(end - start)
    const { bytesRead } = await file.read(data, 0, data.length, start)
    if (bytesRead !== data.length) throw new Error('truncated range')
    return new Response(data, {
      status: 206,
      headers: {
        'Content-Length': String(end - start),
        'Content-Range': `bytes ${start}-${end - 1}/${length}`,
      },
    })
  }
  await import('../dist/worker.js')
  parentPort.postMessage({ ready: true })
}
