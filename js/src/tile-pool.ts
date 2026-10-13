import type { ProjectionTask } from './projection.js'
import type { PreparedTile } from './spatial.js'
import type { DecodedTile, TileOptions } from './tile.js'
import type { TileJob, TileReply, TileWork, TileResult } from './tile-protocol.js'

interface Job {
  id: number
  work: TileWork
  resolve: (data: TileResult) => void
  reject: (error: Error) => void
  cleanup: () => void
  cancelled: boolean
}
interface Slot {
  worker: Worker
  job?: Job
  ready: boolean
}

/** One shared reader owns I/O. CPU jobs receive source pages or decoded geometry.
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
                if ('error' in data)
                  job.reject(Object.assign(new Error(data.error), { name: data.name }))
                else job.resolve(data)
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

  async run(task: PreparedTile, options: TileOptions): Promise<ArrayBuffer> {
    const { signal, ...settings } = options
    return (
      (await this.submit({ type: 'render', task, options: settings }, signal)) as {
        data: ArrayBuffer
      }
    ).data
  }
  async decode(
    task: PreparedTile,
    options?: TileOptions,
  ): Promise<{ tile: DecodedTile; data?: ArrayBuffer }> {
    const { signal, ...settings } = options ?? {}
    return (await this.submit(
      { type: 'decode', task, options: options ? settings : undefined },
      signal,
    )) as { tile: DecodedTile; data?: ArrayBuffer }
  }
  async render(tile: DecodedTile, options: TileOptions): Promise<ArrayBuffer> {
    const { signal, ...settings } = options
    return (
      (await this.submit({ type: 'encode', tile, options: settings }, signal)) as {
        data: ArrayBuffer
      }
    ).data
  }
  async project(task: ProjectionTask, signal?: AbortSignal): Promise<unknown[]> {
    return ((await this.submit({ type: 'project', task }, signal)) as { values: unknown[] }).values
  }
  private submit(work: TileWork, signal?: AbortSignal): Promise<TileResult> {
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
        work,
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
        const work = copyWork(job.work)
        slot.worker.postMessage({ ...work, id: job.id } satisfies TileJob, workTransfers(work))
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

/** Copy at dispatch, never detach cached geometry or compressed source pages. */
function copyWork(work: TileWork): TileWork {
  if (work.type === 'encode') {
    const geometry = { ...work.tile.geometry }
    for (const key of [
      'types',
      'x',
      'y',
      'geometryOffsets',
      'polygonOffsets',
      'ringOffsets',
    ] as const)
      (geometry[key] as unknown) = work.tile.geometry[key].slice()
    return { ...work, tile: { ...work.tile, geometry } }
  }
  if (work.type === 'project')
    return {
      ...work,
      task: {
        ...work.task,
        dictionary: work.task.dictionary?.slice(0) ?? null,
        runs: work.task.runs.map((run) => ({ ...run, bytes: run.bytes.slice(0) })),
      },
    }
  return {
    ...work,
    task: {
      ...work.task,
      payloads: work.task.payloads.map((bytes) => bytes.slice(0)),
      attributePages: work.task.attributePages?.map((attr) => ({
        ...attr,
        payloads: attr.payloads.map((bytes) => bytes.slice(0)),
      })),
    },
  }
}
export function geometryTransfers(tile: DecodedTile): ArrayBuffer[] {
  return [
    ...new Set(
      Object.values(tile.geometry).flatMap((value) =>
        ArrayBuffer.isView(value) ? [value.buffer as ArrayBuffer] : [],
      ),
    ),
  ]
}
function workTransfers(work: TileWork): ArrayBuffer[] {
  if (work.type === 'encode') return geometryTransfers(work.tile)
  if (work.type === 'project')
    return [
      ...work.task.runs.map((run) => run.bytes),
      ...(work.task.dictionary ? [work.task.dictionary] : []),
    ]
  return [
    ...work.task.payloads,
    ...(work.task.attributePages ?? []).flatMap((attr) => attr.payloads),
  ]
}
