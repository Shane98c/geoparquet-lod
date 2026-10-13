import { VisibleTileCache, waitForTile } from './visible-cache.js'
import { decodeProjection } from './projection.js'
/// <reference lib="webworker" />
import type { Transfer, WorkerRequest, WorkerResponse } from './maplibre.js'
import { coalescing, httpSource, s3Mirrors } from './range-buffer.js'
import { openReader, SpatialReader, type TileReader } from './spatial.js'
import { decodePreparedTile, renderDecodedTile, renderPreparedTile, renderTile } from './tile.js'
import { TilePool, geometryTransfers } from './tile-pool.js'
import type { CpuRequest, TileReply } from './tile-protocol.js'

const scope = self as unknown as DedicatedWorkerGlobalScope
const visible = new VisibleTileCache()
let geometryDecodes = 0,
  projectionJobs = 0
const readers = new Map<string, Promise<TileReader>>()
let workerCount = 2
let pool: TilePool | undefined
let started = false
const getPool = () => (workerCount === 1 ? undefined : (pool ??= new TilePool(workerCount)))
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

scope.addEventListener('message', (event: MessageEvent<WorkerRequest | CpuRequest>) => {
  const request = event.data
  // CPU workers reuse this same URL, so the pool adds no second JS bundle.
  // They receive prepared pages or decoded geometry, never their own reader.
  if (request.type === 'cpu-init') {
    scope.postMessage({ ready: true } satisfies TileReply)
    return
  }
  if (
    request.type === 'render' ||
    request.type === 'decode' ||
    request.type === 'encode' ||
    request.type === 'project'
  ) {
    try {
      if (request.type === 'project') {
        scope.postMessage({
          id: request.id,
          values: decodeProjection(request.task),
        } satisfies TileReply)
      } else if (request.type === 'decode') {
        const tile = decodePreparedTile(request.task)
        const data = request.options ? renderDecodedTile(tile, request.options) : undefined
        scope.postMessage({ id: request.id, tile, data } satisfies TileReply, [
          ...geometryTransfers(tile),
          ...(data ? [data] : []),
        ])
      } else {
        const data =
          request.type === 'render'
            ? renderPreparedTile(request.task, request.options)
            : renderDecodedTile(request.tile, request.options)
        scope.postMessage({ id: request.id, data } satisfies TileReply, [data])
      }
    } catch (error) {
      scope.postMessage({
        id: request.id,
        error: String(error),
        name: (error as Error).name ?? 'Error',
      } satisfies TileReply)
    }
    return
  }
  if (request.type === 'configure') {
    if (
      started ||
      !Number.isInteger(request.workers) ||
      request.workers < 1 ||
      request.workers > 4
    ) {
      scope.postMessage({
        id: request.id,
        ok: false,
        name: 'Error',
        error: 'Configure 1–4 workers before opening a source',
      } satisfies WorkerResponse)
    } else {
      workerCount = request.workers
      scope.postMessage({ id: request.id, ok: true, configured: true } satisfies WorkerResponse)
    }
    return
  }
  if (request.type === 'cancel') {
    controllers.get(request.id)?.abort()
    return
  }
  if (request.type === 'stats') {
    scope.postMessage({
      id: request.id,
      ok: true,
      stats,
      cache: {
        bytes: visible.bytes,
        tiles: visible.size,
        decodes: geometryDecodes,
        projections: projectionJobs,
      },
    } satisfies WorkerResponse)
    return
  }
  if (request.type === 'open') {
    started = true
    Promise.all([getReader(request.url), getPool()?.ready])
      .then(([reader]) => reader)
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
  started = true
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
          .then(async (reader) => {
            const options = {
              layer: request.config.layer,
              pointLayer: request.config.pointLayer,
              properties: request.config.properties,
              signal: controller.signal,
            }
            const cpu = getPool()
            const properties = options.properties ?? reader.lod.attributes.map((a) => a.name)
            if (
              reader instanceof SpatialReader &&
              reader.lod.encoding === 'packed' &&
              request.z <= reader.finestZoom
            ) {
              const source = properties.filter(
                (name) => !reader.lod.attributes.some((a) => a.name === name),
              )
              const key = JSON.stringify([request.config.url, request.z, request.x, request.y])
              let rendered: ArrayBuffer | undefined
              const tile = await waitForTile(
                visible.getOrLoad(key, async () => {
                  // Shared work outlives any one cancelled style request. Cache
                  // all carried values already in the fetched packed pages.
                  const task = await reader.prepareTile(
                    reader.lodForZoom(request.z),
                    request.z,
                    request.x,
                    request.y,
                  )
                  geometryDecodes++
                  const settings = source.length ? undefined : { ...options, signal: undefined }
                  if (cpu) {
                    const result = await cpu.decode(task, settings)
                    rendered = result.data
                    return result.tile
                  }
                  const decoded = decodePreparedTile(task)
                  if (settings) rendered = renderDecodedTile(decoded, settings)
                  return decoded
                }),
                controller.signal,
              )
              if (rendered) return rendered
              const missing = source.filter((name) => !Object.hasOwn(tile.columns, name))
              const extras = missing.length
                ? await reader.readColumns(
                    tile.rows,
                    missing,
                    controller.signal,
                    cpu
                      ? (task, signal) => {
                          projectionJobs++
                          return cpu.project(task, signal)
                        }
                      : undefined,
                  )
                : {}
              controller.signal.throwIfAborted()
              visible.addColumns(key, tile, extras)
              // New styles never modify the cached geometry or carried columns.
              const columns = Object.fromEntries(
                properties.map((name) => [name, extras[name] ?? tile.columns[name]]),
              )
              const styled = { ...tile, columns }
              return cpu ? cpu.render(styled, options) : renderDecodedTile(styled, options)
            }
            if (
              cpu &&
              reader instanceof SpatialReader &&
              request.z <= reader.finestZoom &&
              properties.every((name) => reader.lod.attributes.some((a) => a.name === name))
            ) {
              const task = await reader.prepareTile(
                reader.lodForZoom(request.z),
                request.z,
                request.x,
                request.y,
                controller.signal,
                properties,
              )
              return cpu.run(task, options)
            }
            return renderTile(reader, request.z, request.x, request.y, options)
          })
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
