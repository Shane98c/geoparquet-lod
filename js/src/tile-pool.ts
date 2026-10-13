import type { PreparedTile } from './spatial.js'
import type { TileOptions } from './tile.js'
import type { TileJob, TileReply } from './tile-protocol.js'

interface Job {
  id: number
  task: PreparedTile
  options: Omit<TileOptions, 'signal'>
  resolve: (data: ArrayBuffer) => void
  reject: (error: Error) => void
  cleanup: () => void
  cancelled: boolean
}
interface Slot {
  worker: Worker
  job?: Job
  ready: boolean
}

/** One shared reader owns I/O. Only compressed page copies cross this boundary.
 * Copies are made at dispatch, so queued jobs retain cache references, not copies.
 */
export class TilePool {
  readonly ready: Promise<void>
  private slots: Slot[] = []
  private queue: Job[] = []
  private nextId = 0
  private failure?: Error

  constructor(count: number) {
    this.ready = Promise.all(
      Array.from(
        { length: count },
        () =>
          new Promise<void>((resolve, reject) => {
            const worker = new Worker(new URL('./worker.js', import.meta.url), {
              type: 'module',
            })
            const slot: Slot = { worker, ready: false }
            this.slots.push(slot)
            worker.addEventListener('message', ({ data }: MessageEvent<TileReply>) => {
              if ('ready' in data) {
                slot.ready = true
                resolve()
                this.pump()
                return
              }
              const job = slot.job
              if (!job || job.id !== data.id) return
              slot.job = undefined
              job.cleanup()
              if (!job.cancelled) {
                if ('data' in data) job.resolve(data.data)
                else job.reject(Object.assign(new Error(data.error), { name: data.name }))
              }
              this.pump()
            })
            const fail = (event: ErrorEvent | MessageEvent) => {
              const error = new Error(
                'message' in event ? event.message : 'Tile worker transport failed',
              )
              reject(error)
              this.fail(error)
            }
            worker.addEventListener('error', fail)
            worker.addEventListener('messageerror', fail)
            worker.postMessage({ type: 'cpu-init' })
          }),
      ),
    ).then(() => undefined)
    // A construction/load failure may precede the first caller of ready.
    void this.ready.catch((error: Error) => this.fail(error))
  }

  run(task: PreparedTile, options: TileOptions): Promise<ArrayBuffer> {
    const { signal, ...settings } = options
    signal?.throwIfAborted()
    if (this.failure) return Promise.reject(this.failure)
    return new Promise((resolve, reject) => {
      const abort = () => {
        job.cancelled = true
        const index = this.queue.indexOf(job)
        if (index >= 0) this.queue.splice(index, 1)
        job.cleanup()
        reject(new DOMException('Aborted', 'AbortError'))
        // Running synchronous work finishes in its slot; its result is discarded.
        // Other tiles' fetches and computations are never cancelled with it.
      }
      const job: Job = {
        id: ++this.nextId,
        task,
        options: settings,
        resolve,
        reject,
        cancelled: false,
        cleanup: () => signal?.removeEventListener('abort', abort),
      }
      signal?.addEventListener('abort', abort, { once: true })
      this.queue.push(job)
      this.pump()
    })
  }

  private pump() {
    for (const slot of this.slots) {
      if (!slot.ready || slot.job || this.failure) continue
      const job = this.queue.shift()
      if (!job) return
      slot.job = job
      try {
        const payloads = job.task.payloads.map((bytes) => bytes.slice(0))
        slot.worker.postMessage(
          {
            type: 'render',
            id: job.id,
            task: { ...job.task, payloads },
            options: job.options,
          } satisfies TileJob,
          payloads,
        )
      } catch (error) {
        slot.job = undefined
        job.cleanup()
        job.reject(error as Error)
        this.pump()
      }
    }
  }

  private fail(error: Error) {
    if (this.failure) return
    this.failure = error
    for (const slot of this.slots) {
      slot.worker.terminate()
      if (slot.job) this.queue.push(slot.job)
      slot.job = undefined
    }
    for (const job of this.queue.splice(0)) {
      job.cleanup()
      job.reject(error)
    }
  }
}
