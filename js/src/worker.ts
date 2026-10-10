/// <reference lib="webworker" />
import type { WorkerRequest, WorkerResponse } from './maplibre.js'
import { coalescing, httpSource, s3Mirrors } from './range-buffer.js'
import { LodReader } from './reader.js'
import { renderTile } from './tile.js'

const scope = self as unknown as DedicatedWorkerGlobalScope
const readers = new Map<string, Promise<LodReader>>()
const controllers = new Map<number, AbortController>()

const getReader = (url: string): Promise<LodReader> => {
  let reader = readers.get(url)
  if (!reader) {
    reader = httpSource(s3Mirrors(url)).then((file) => LodReader.open(coalescing(file)))
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
  const controller = new AbortController()
  controllers.set(request.id, controller)
  const work =
    request.type === 'row'
      ? getReader(request.url)
          .then((reader) => reader.readRow(request.rowIndex, request.columns, controller.signal))
          .then((row) => scope.postMessage({ id: request.id, ok: true, row } satisfies WorkerResponse))
      : getReader(request.config.url)
          .then((reader) =>
            renderTile(reader, request.z, request.x, request.y, {
              layer: request.config.layer,
              pointLayer: request.config.pointLayer,
              properties: request.config.properties,
              signal: controller.signal,
            }),
          )
          .then((data) => scope.postMessage({ id: request.id, ok: true, data } satisfies WorkerResponse, [data]))
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
