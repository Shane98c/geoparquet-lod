import { Worker } from 'node:worker_threads'
import { once } from 'node:events'

export async function workerClient(path, workers, delay = 0) {
  const start = performance.now()
  const worker = new Worker(new URL('./worker-harness.mjs', import.meta.url), {
    workerData: { path, delay },
  })
  await once(worker, 'message')
  const pending = new Map()
  let id = 0
  worker.on('message', (response) => {
    const job = pending.get(response.id)
    if (!job) return
    pending.delete(response.id)
    if (response.ok) job.resolve(response)
    else job.reject(Object.assign(new Error(response.error), { name: response.name }))
  })
  worker.on('error', (error) => {
    for (const job of pending.values()) job.reject(error)
    pending.clear()
  })
  const call = (request) => {
    const requestId = ++id
    const result = new Promise((resolve, reject) => {
      pending.set(requestId, { resolve, reject })
      worker.postMessage({ ...request, id: requestId })
    })
    result.id = requestId
    return result
  }
  await call({ type: 'configure', workers })
  return {
    call,
    startupMs: performance.now() - start,
    cancel: (id) => worker.postMessage({ type: 'cancel', id }),
    close: () => worker.terminate(),
  }
}
