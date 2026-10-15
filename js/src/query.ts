import type { Bbox, Lod, LodRows, PrimaryRows } from './reader.js'
import type { TileReader } from './spatial.js'

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
export type FeatureResult =
  (LodRows & { geometry: 'overview'; lod: Lod }) | (PrimaryRows & { geometry: 'primary' })

/** Whole, unclipped features selected by their geometry's bounding box.
 * Overview results are simplified/quantized and may be thinned. Primary results
 * use original WKB and source bbox selection. Neither is an exact intersection
 * predicate. Row IDs are deduplicated across tiles and wrapped query halves.
 */
export async function readFeatures(
  reader: TileReader,
  query: FeatureQuery,
): Promise<FeatureResult> {
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
  if (query.geometry === 'primary') {
    const result: PrimaryRows & { geometry: 'primary' } = {
      geometry: 'primary',
      rows: [],
      wkb: [],
      columns: Object.fromEntries(columns.map((n) => [n, []])),
    }
    // Primary queries explicitly project original source values, including for
    // names that also have overridden display values in the overview profile.
    for (const box of boxes) {
      const part = await reader.readPrimary(box, [], signal)
      for (let i = 0; i < part.rows.length; i++) {
        if (seen.has(part.rows[i])) continue
        seen.add(part.rows[i])
        result.rows.push(part.rows[i])
        result.wkb.push(part.wkb[i])
      }
    }
    result.columns = await reader.readColumns(result.rows, columns, signal)
    return result
  }
  // Sparse profiles have no payload or spill tags at intervening zooms.
  // Choose the first declared zoom at least as detailed as the requested one.
  const targetZoom = Math.ceil(Math.log2(360 / (reader.lod.tile_size * resolution)))
  const declared = [...new Set(reader.lod.zooms)].sort((a, b) => a - b)
  const z = declared.find((zoom) => zoom >= targetZoom) ?? declared[declared.length - 1]
  const lod = reader.lodForZoom(z)
  const n = 2 ** z
  const tileX = (lon: number) => Math.max(0, Math.min(n - 1, Math.floor(((lon + 180) / 360) * n)))
  const tileY = (lat: number) =>
    Math.max(
      0,
      Math.min(
        n - 1,
        Math.floor(
          ((1 -
            Math.asinh(
              Math.tan((Math.max(-85.05112878, Math.min(85.05112878, lat)) * Math.PI) / 180),
            ) /
              Math.PI) /
            2) *
            n,
        ),
      ),
    )
  const cells = new Map<string, [number, number]>()
  const limit = query.maxTiles ?? 256
  if (!Number.isSafeInteger(limit) || limit < 1)
    throw new Error('maxTiles must be a positive integer')
  for (const [west, south, east, north] of boxes) {
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
  const result: LodRows & { geometry: 'overview'; lod: Lod } = {
    geometry: 'overview',
    lod,
    rows: [],
    coords: [],
    columns: Object.fromEntries(columns.map((n) => [n, []])),
  }
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
        const bounds = coordinateBounds(part.coords[i], lod)
        if (
          !boxes.some(
            (b) => bounds[0] <= b[2] && bounds[2] >= b[0] && bounds[1] <= b[3] && bounds[3] >= b[1],
          )
        )
          continue
        seen.add(id)
        result.rows.push(id)
        result.coords.push(part.coords[i])
        for (const name of columns) result.columns[name].push(part.columns[name][i])
      }
  }
  signal?.throwIfAborted()
  return result
}

function coordinateBounds(coords: unknown, lod: Lod): Bbox {
  const bounds: Bbox = [Infinity, Infinity, -Infinity, -Infinity]
  const visit = (value: unknown): void => {
    if (!Array.isArray(value)) return
    if (typeof value[0] === 'number' && typeof value[1] === 'number') {
      const x = value[0] * lod.scale[0] + lod.offset[0],
        y = value[1] * lod.scale[1] + lod.offset[1]
      bounds[0] = Math.min(bounds[0], x)
      bounds[1] = Math.min(bounds[1], y)
      bounds[2] = Math.max(bounds[2], x)
      bounds[3] = Math.max(bounds[3], y)
    } else value.forEach(visit)
  }
  visit(coords)
  return bounds
}
