/// <reference lib="webworker" />
import type { Transfer, WorkerRequest, WorkerResponse } from './maplibre.js'
import { coalescing, httpSource, s3Mirrors } from './range-buffer.js'
import { openReader, type TileReader } from './spatial.js'
import { renderTile } from './tile.js'

const scope = self as unknown as DedicatedWorkerGlobalScope
const readers = new Map<string, Promise<TileReader>>()
const stats: Record<string, Transfer> = {}

const fetched = scope.fetch.bind(scope)
scope.fetch = async (input, init) => {
  const response = await fetched(input, init)
  const key = String(input instanceof Request ? input.url : input)
  const entry = (stats[key] ??= { requests: 0, bytes: 0 })
  entry.requests++
  if (init?.method !== 'HEAD') entry.bytes += Number(response.headers.get('content-length') ?? 0)
  return response
}
const controllers = new Map<number, AbortController>()

const getReader = (url: string): Promise<TileReader> => {
  let reader = readers.get(url)
  if (!reader) {
    reader = httpSource(s3Mirrors(url)).then((file) => openReader(coalescing(file)))
    readers.set(url, reader)
    reader.catch(() => readers.delete(url))
  }
  return reader
}

scope.addEventListener('message', (event: MessageEvent<WorkerRequest>) => {
  const request = event.data
  if (request.type === 'cancel') {
    controllers.get(request.id)?.abort()
    return
  }
  if (request.type === 'stats') {
    scope.postMessage({ id: request.id, ok: true, stats } satisfies WorkerResponse)
    return
  }
  if (request.type === 'open') {
    getReader(request.url)
      .then((reader) =>
        scope.postMessage({
          id: request.id,
          ok: true,
          opened: true,
          metadata: { lod: reader.lod, bounds: reader.bounds, columns: reader.scalarColumns() },
        } satisfies WorkerResponse),
      )
      .catch((error: Error) =>
        scope.postMessage({
          id: request.id,
          ok: false,
          error: error?.message ?? String(error),
          name: error?.name ?? 'Error',
        } satisfies WorkerResponse),
      )
    return
  }
  const controller = new AbortController()
  controllers.set(request.id, controller)
  const work =
    request.type === 'row'
      ? getReader(request.url)
          .then((reader) => reader.readRow(request.rowIndex, request.columns, controller.signal))
          .then((row) =>
            scope.postMessage({ id: request.id, ok: true, row } satisfies WorkerResponse),
          )
      : getReader(request.config.url)
          .then((reader) =>
            renderTile(reader, request.z, request.x, request.y, {
              layer: request.config.layer,
              pointLayer: request.config.pointLayer,
              properties: request.config.properties,
              signal: controller.signal,
            }),
          )
          .then((data) =>
            scope.postMessage({ id: request.id, ok: true, data } satisfies WorkerResponse, [data]),
          )
  work
    .catch((error: Error) =>
      scope.postMessage({
        id: request.id,
        ok: false,
        error: error?.message ?? String(error),
        name: error?.name ?? 'Error',
      } satisfies WorkerResponse),
    )
    .finally(() => controllers.delete(request.id))
})
