// Run actual browser worker modules with Node's worker transport and range I/O.
import { parentPort, workerData, Worker as NodeWorker } from 'node:worker_threads'
import { setTimeout as delay } from 'node:timers/promises'
import { open } from 'node:fs/promises'

globalThis.self = globalThis
globalThis.addEventListener = (type, callback) => {
  if (type === 'message') parentPort.on('message', (data) => callback({ data }))
}
const network = {
  started: 0,
  completed: 0,
  aborted: 0,
  bytes: 0,
  uniqueBytes: 0,
  refetchedBytes: 0,
}
const ranges = []
globalThis.postMessage = (data, transfer) =>
  parentPort.postMessage(data.stats ? { ...data, network } : data, transfer)
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
    network.started++
    try {
      options.signal?.throwIfAborted()
      if (workerData.delay) await delay(workerData.delay, undefined, { signal: options.signal })
      if (options.method === 'HEAD')
        return new Response(null, { headers: { 'Content-Length': String(length) } })
      const [, a, b] = options.headers.Range.match(/bytes=(\d+)-(\d+)/)
      const start = Number(a),
        end = Math.min(Number(b) + 1, length)
      const data = new Uint8Array(end - start)
      const { bytesRead } = await file.read(data, 0, data.length, start)
      if (bytesRead !== data.length) throw new Error('truncated range')
      options.signal?.throwIfAborted()
      network.completed++
      network.bytes += data.length
      let covered = 0
      for (const [lo, hi] of ranges) covered += Math.max(0, Math.min(end, hi) - Math.max(start, lo))
      network.uniqueBytes += data.length - covered
      network.refetchedBytes += covered
      ranges.push([start, end])
      ranges.sort((a, b) => a[0] - b[0])
      for (let i = 1; i < ranges.length;) {
        if (ranges[i][0] <= ranges[i - 1][1]) {
          ranges[i - 1][1] = Math.max(ranges[i - 1][1], ranges[i][1])
          ranges.splice(i, 1)
        } else i++
      }
      return new Response(data, {
        status: 206,
        headers: {
          'Content-Length': String(end - start),
          'Content-Range': `bytes ${start}-${end - 1}/${length}`,
        },
      })
    } catch (error) {
      if (options.signal?.aborted) network.aborted++
      throw error
    }
  }
  await import('../dist/worker.js')
  parentPort.postMessage({ ready: true })
}
