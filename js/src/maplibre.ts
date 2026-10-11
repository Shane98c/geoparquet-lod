/**
 * MapLibre integration: `registerProtocol(maplibregl)` once, then use
 * `tileUrl(fileUrl, options)` as a vector source's tile template. Tiles are
 * rendered in a worker so page scrolling stays smooth.
 */
import type { TileOptions } from './tile.js'

export const PROTOCOL = 'lod'

export interface SourceConfig {
  url: string
  layer?: string
  pointLayer?: string
  properties?: string[]
  /** Tile directory sidecar URL, e.g. `<url>.tiledir.parquet`; see TileOptions. */
  tiledir?: string
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
  | { id: number; type: 'cancel' }
export type WorkerResponse =
  | { id: number; ok: true; data: ArrayBuffer }
  | { id: number; ok: true; row: Record<string, unknown> }
  | { id: number; ok: true; stats: Record<string, Transfer> }
  | { id: number; ok: false; error: string; name: string }

let worker: Worker | undefined
let nextId = 0
const pending = new Map<
  number,
  { resolve: (r: WorkerResponse) => void; reject: (e: Error) => void }
>()

const getWorker = (): Worker => {
  if (worker) return worker
  worker = new Worker(new URL('./worker.js', import.meta.url), { type: 'module' })
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
    pending.set(id, { resolve, reject })
    signal?.addEventListener('abort', () => {
      if (!pending.delete(id)) return
      worker?.postMessage({ id, type: 'cancel' } satisfies WorkerRequest)
      reject(new DOMException('Aborted', 'AbortError'))
    })
    getWorker().postMessage({ ...request, id } as WorkerRequest)
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
