import { geometryColumnFromWkb } from '@cogp/reader'
import type { GeometryBatch } from './batch.js'
import { GeometryBuilder, type LodGeometry } from './geometry.js'
import type { Bbox } from './reader.js'
import type { TileReader } from './spatial.js'
import { HALF_WORLD, mercatorX, mercatorY } from './web-mercator.js'

export interface FeatureQuery {
  /** WGS84 bounds; west > east wraps across the antimeridian. */
  bbox: Bbox
  /** Longitude degrees per pixel; used to choose an overview. */
  resolution: number
  columns?: string[]
  geometry?: 'overview' | 'primary'
  /** Bound work for an excessively detailed query over a large area. Default 256. */
  maxTiles?: number
  signal?: AbortSignal
}

/** Whole, unclipped features selected by their geometry's bounding box, as one
 * columnar batch. Overview results are simplified/quantized and may be thinned.
 * Primary results use original WKB and source bbox selection. Neither is an
 * exact intersection predicate. Row IDs are deduplicated across tiles and
 * wrapped query halves.
 */
export async function readFeatures(
  reader: TileReader,
  query: FeatureQuery,
): Promise<GeometryBatch> {
  const { bbox, signal, resolution } = query
  signal?.throwIfAborted()
  if (
    bbox.length !== 4 ||
    !bbox.every(Number.isFinite) ||
    bbox[1] > bbox[3] ||
    bbox[0] < -180 ||
    bbox[0] > 180 ||
    bbox[2] < -180 ||
    bbox[2] > 180 ||
    bbox[1] < -90 ||
    bbox[3] > 90
  )
    throw new Error('expected a WGS84 bbox')
  if (!(resolution > 0) || !Number.isFinite(resolution))
    throw new Error('resolution must be positive and finite')
  const columns = query.columns ?? []
  const boxes: Bbox[] =
    bbox[0] <= bbox[2]
      ? [bbox]
      : [
          [bbox[0], bbox[1], 180, bbox[3]],
          [-180, bbox[1], bbox[2], bbox[3]],
        ]
  const seen = new Set<number>()
  const rows: number[] = []
  if (query.geometry === 'primary') {
    const wkb: Uint8Array[] = []
    // Primary queries explicitly project original source values, including for
    // names that also have overridden display values in the overview profile.
    for (const box of boxes) {
      const part = await reader.readPrimary(box, [], signal)
      for (let i = 0; i < part.rows.length; i++) {
        if (seen.has(part.rows[i])) continue
        seen.add(part.rows[i])
        rows.push(part.rows[i])
        wkb.push(part.wkb[i])
      }
    }
    return {
      geometry: geometryColumnFromWkb(wkb),
      rowIndex: rows,
      columns: await reader.readColumns(rows, columns, signal),
      source: 'primary',
    }
  }
  // Sparse profiles have no payload or spill tags at intervening zooms.
  // Choose the first declared zoom at least as detailed as the requested one.
  const targetZoom = Math.ceil(Math.log2(360 / (reader.lod.tile_size * resolution)))
  const declared = [...new Set(reader.lod.zooms)].sort((a, b) => a - b)
  const z = declared.find((zoom) => zoom >= targetZoom) ?? declared[declared.length - 1]
  const lod = reader.lodForZoom(z)
  const n = 2 ** z
  const tile = (fraction: number) => Math.max(0, Math.min(n - 1, Math.floor(fraction * n)))
  const tileX = (x: number) => tile((x + HALF_WORLD) / (2 * HALF_WORLD))
  const tileY = (y: number) => tile((HALF_WORLD - y) / (2 * HALF_WORLD))
  // LOD coordinates are EPSG:3857 metres, so the query boxes are compared in metres
  const projected: Bbox[] = boxes.map(([west, south, east, north]) => [
    mercatorX(west),
    mercatorY(south),
    mercatorX(east),
    mercatorY(north),
  ])
  const cells = new Map<string, [number, number]>()
  const limit = query.maxTiles ?? 256
  if (!Number.isSafeInteger(limit) || limit < 1)
    throw new Error('maxTiles must be a positive integer')
  for (const [west, south, east, north] of projected) {
    // A point can round across its source tile boundary. Include the adjacent
    // source cell within one quantization step, then filter actual LOD bounds.
    const x0 = tileX(west - lod.scale[0]),
      x1 = tileX(east + lod.scale[0]),
      y0 = tileY(north + lod.scale[1]),
      y1 = tileY(south - lod.scale[1])
    if ((x1 - x0 + 1) * (y1 - y0 + 1) > limit)
      throw new Error('query exceeds maxTiles; use a coarser resolution or primary geometry')
    for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) cells.set(`${x}/${y}`, [x, y])
  }
  if (cells.size > limit)
    throw new Error('query exceeds maxTiles; use a coarser resolution or primary geometry')
  const builder = new GeometryBuilder()
  const values: Record<string, unknown[]> = Object.fromEntries(columns.map((n) => [n, []]))
  // Bounded batches share in-flight page requests without scheduling an entire
  // world's worth of decodes or retaining every tile result at once.
  const tiles = [...cells.values()]
  for (let start = 0; start < tiles.length; start += 8) {
    signal?.throwIfAborted()
    const parts = await Promise.all(
      tiles
        .slice(start, start + 8)
        .map(([x, y]) => reader.readLodByTile(lod, z, x, y, columns, signal)),
    )
    for (const part of parts)
      for (let i = 0; i < part.rows.length; i++) {
        const id = part.rows[i]
        if (seen.has(id)) continue
        const bounds = featureBounds(part.geometry, i)
        if (
          !projected.some(
            (b) => bounds[0] <= b[2] && bounds[2] >= b[0] && bounds[1] <= b[3] && bounds[3] >= b[1],
          )
        )
          continue
        seen.add(id)
        rows.push(id)
        builder.append(part.geometry, i)
        for (const name of columns) values[name].push(part.columns[name][i])
      }
  }
  signal?.throwIfAborted()
  return {
    geometry: builder.finish(lod.scale, lod.offset),
    rowIndex: rows,
    columns: values,
    source: 'overview',
  }
}

/** The bounds of feature `i` in EPSG:3857 metres; empty bounds for an empty feature. */
function featureBounds(geometry: LodGeometry, i: number): Bbox {
  const { x, y, scale, offset, ringOffsets: ro, polygonOffsets: po } = geometry
  const start = ro[po[geometry.geometryOffsets[i]]],
    end = ro[po[geometry.geometryOffsets[i + 1]]]
  if (start === end) return [Infinity, Infinity, -Infinity, -Infinity]
  let x0 = x[start],
    y0 = y[start],
    x1 = x0,
    y1 = y0
  for (let j = start + 1; j < end; j++) {
    if (x[j] < x0) x0 = x[j]
    else if (x[j] > x1) x1 = x[j]
    if (y[j] < y0) y0 = y[j]
    else if (y[j] > y1) y1 = y[j]
  }
  const ax = offset[0] + scale[0] * x0,
    bx = offset[0] + scale[0] * x1,
    ay = offset[1] + scale[1] * y0,
    by = offset[1] + scale[1] * y1
  return [Math.min(ax, bx), Math.min(ay, by), Math.max(ax, bx), Math.max(ay, by)]
}
