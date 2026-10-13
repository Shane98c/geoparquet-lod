/**
 * MapLibre integration: `registerProtocol(maplibregl)` once, then use
 * `tileUrl(config)` as a vector source's tile template. Tiles are rendered in
 * a worker so page scrolling stays smooth; `prefetch(url)` opens the file
 * before the map exists.
 */
import type { TileOptions } from './tile.js'
import type { Bbox, LodMetadata } from './reader.js'

export const PROTOCOL = 'lod'

export interface SourceConfig {
  url: string
  layer?: string
  pointLayer?: string
  properties?: string[]
}

export const tileUrl = (config: SourceConfig): string =>
  `${PROTOCOL}://tile/${encode(config)}/{z}/{x}/{y}.pbf`

export const parseTileUrl = (
  url: string,
): { config: SourceConfig; z: number; x: number; y: number } => {
  const match = new RegExp(`^${PROTOCOL}://tile/([A-Za-z0-9_-]+)/(\\d+)/(\\d+)/(\\d+)\\.pbf$`).exec(
    url,
  )
  if (!match) throw new Error(`invalid ${PROTOCOL} tile url: ${url}`)
  const [z, x, y] = match.slice(2).map(Number)
  return { config: decode(match[1]), z, x, y }
}

const encode = (config: SourceConfig): string => {
  const bytes = new TextEncoder().encode(JSON.stringify(config))
  let binary = ''
  for (let i = 0; i < bytes.length; i += 8192)
    binary += String.fromCharCode(...bytes.subarray(i, i + 8192))
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

const decode = (token: string): SourceConfig => {
  const binary = atob(token.replace(/-/g, '+').replace(/_/g, '/'))
  return JSON.parse(new TextDecoder().decode(Uint8Array.from(binary, (c) => c.charCodeAt(0))))
}

export interface Transfer {
  requests: number
  bytes: number
}

export type WorkerRequest =
  | { id: number; type: 'tile'; config: SourceConfig; z: number; x: number; y: number }
  | { id: number; type: 'row'; url: string; rowIndex: number; columns: string[] }
  | { id: number; type: 'stats' }
  | { id: number; type: 'open'; url: string }
  | { id: number; type: 'cancel' }
  | { id: number; type: 'configure'; workers: number }
export type WorkerResponse =
  | { id: number; ok: true; configured: true }
  | { id: number; ok: true; data: ArrayBuffer }
  | { id: number; ok: true; row: Record<string, unknown> }
  | { id: number; ok: true; stats: Record<string, Transfer> }
  | {
      id: number
      ok: true
      opened: true
      metadata: { lod: LodMetadata; bounds: Bbox; columns: string[] }
    }
  | { id: number; ok: false; error: string; name: string }

let worker: Worker | undefined
let workerCount = Math.min(2, Math.max(1, (globalThis.navigator?.hardwareConcurrency ?? 4) - 1))
/** Set 1–4 tile CPU workers before prefetch, readMetadata, or registerProtocol.
 * One disables the pool. Indexed sources share one I/O coordinator in all modes.
 */
export const setWorkerCount = (count: number): void => {
  if (!Number.isInteger(count) || count < 1 || count > 4) throw new Error('Expected 1–4 workers')
  if (worker) throw new Error('Set worker count before opening a source')
  workerCount = count
}
let nextId = 0
const pending = new Map<
  number,
  { resolve: (r: WorkerResponse) => void; reject: (e: Error) => void }
>()

const getWorker = (): Worker => {
  if (worker) return worker
  worker = new Worker(new URL('./worker.js', import.meta.url), { type: 'module' })
  worker.postMessage({ id: 0, type: 'configure', workers: workerCount } satisfies WorkerRequest)
  const current = worker
  const fail = (event: ErrorEvent | MessageEvent) => {
    if (worker !== current) return
    const error = new Error('message' in event ? event.message : 'Worker transport failed')
    for (const request of pending.values()) request.reject(error)
    pending.clear()
    current.terminate()
    worker = undefined
  }
  worker.addEventListener('error', fail)
  worker.addEventListener('messageerror', fail)
  worker.addEventListener('message', (event: MessageEvent<WorkerResponse>) => {
    const response = event.data
    const request = pending.get(response.id)
    if (!request) return
    pending.delete(response.id)
    if (response.ok) request.resolve(response)
    else {
      const error = new Error(response.error)
      error.name = response.name
      request.reject(error)
    }
  })
  return worker
}

type Request = WorkerRequest extends infer R ? (R extends unknown ? Omit<R, 'id'> : never) : never

const call = (request: Request, signal?: AbortSignal): Promise<WorkerResponse> =>
  new Promise((resolve, reject) => {
    const id = ++nextId
    if (signal?.aborted) {
      reject(new DOMException('Aborted', 'AbortError'))
      return
    }
    const abort = () => {
      if (!pending.delete(id)) return
      worker?.postMessage({ id, type: 'cancel' } satisfies WorkerRequest)
      reject(new DOMException('Aborted', 'AbortError'))
    }
    const cleanup = () => signal?.removeEventListener('abort', abort)
    pending.set(id, {
      resolve: (response) => {
        cleanup()
        resolve(response)
      },
      reject: (error) => {
        cleanup()
        reject(error)
      },
    })
    signal?.addEventListener('abort', abort, { once: true })
    try {
      getWorker().postMessage({ ...request, id } as WorkerRequest)
    } catch (error) {
      pending.delete(id)
      cleanup()
      reject(error)
    }
  })

/** Values of flat parquet columns for one feature, by its MVT feature id (the file row). */
export const readRow = async (url: string, rowIndex: number, columns: string[]) => {
  const response = await call({ type: 'row', url, rowIndex, columns })
  return 'row' in response ? response.row : {}
}

/** Bytes and requests the worker has fetched, by URL. */
export const readStats = async (): Promise<Record<string, Transfer>> => {
  const response = await call({ type: 'stats' })
  return 'stats' in response ? response.stats : {}
}

/**
 * Start the worker and open `url` now, so its footer and prefix indexes are in
 * hand by the time the map asks for a tile. Call it as early as the page can,
 * before the map is constructed.
 */
export const prefetch = (url: string): void => {
  void call({ type: 'open', url }).catch(() => undefined)
}

/** Map metadata from the same worker/reader that will serve tiles. */
export const readMetadata = async (
  url: string,
): Promise<{ lod: LodMetadata; bounds: Bbox; columns: string[] }> => {
  const response = await call({ type: 'open', url })
  if (!('metadata' in response)) throw new Error('worker returned no map metadata')
  return response.metadata
}

/** Register the `lod://` tile scheme once per MapLibre module instance. */
export const registerProtocol = (maplibre: {
  addProtocol: (
    scheme: string,
    action: (
      params: { url: string },
      controller: AbortController,
    ) => Promise<{ data: ArrayBuffer }>,
  ) => void
}) => {
  maplibre.addProtocol(PROTOCOL, async (params, controller) => {
    const { config, z, x, y } = parseTileUrl(params.url)
    const response = await call({ type: 'tile', config, z, x, y }, controller.signal)
    return { data: 'data' in response ? response.data : new ArrayBuffer(0) }
  })
}

export type { TileOptions }
