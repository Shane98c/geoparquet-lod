import { geometryColumnFromWkb, toGeoArrow, toGeoJSON } from '@cogp/reader'
import type { GeometryColumn, GeoArrowOptions } from '@cogp/reader'
import { GeometryBuilder } from './geometry.js'
import { readFeatures, type FeatureQuery, type FeatureResult } from './query.js'
import { toMvt, type MvtTileOptions } from './mvt.js'
import type { TileReader } from './spatial.js'

/** Renderer-neutral buffers. Coordinates are offset + scale * [x, y] in WGS84.
 * Overview buffers retain quantized int32 coordinates; primary buffers use float64.
 * Offsets describe feature -> polygon -> ring/line -> coordinate ranges.
 */
export interface GeometryBatch {
  geometry: GeometryColumn
  rowIndex: number[]
  columns: Record<string, unknown[]>
  source: 'overview' | 'primary'
}

/** Normalize either readFeatures result without converting geometry to objects. */
export function toGeometryBatch(result: FeatureResult): GeometryBatch {
  if (Object.values(result.columns).some((column) => column.length !== result.rows.length))
    throw new Error('attribute column length does not match row IDs')
  let geometry: GeometryColumn
  if (result.geometry === 'primary') {
    if (result.wkb.length !== result.rows.length)
      throw new Error('geometry length does not match row IDs')
    geometry = geometryColumnFromWkb(result.wkb)
  } else {
    if (result.coords.length !== result.rows.length)
      throw new Error('geometry length does not match row IDs')
    const builder = new GeometryBuilder()
    for (const coords of result.coords) builder.add(result.lod.geometry_type, coords)
    geometry = builder.finish(result.lod.scale, result.lod.offset)
  }
  return { geometry, rowIndex: result.rows, columns: result.columns, source: result.geometry }
}

export async function readGeometryBatch(
  reader: TileReader,
  query: FeatureQuery,
): Promise<GeometryBatch> {
  return toGeometryBatch(await readFeatures(reader, query))
}

/** Small-data adapter; properties retain their JS types (including bigint). */
export function batchToGeoJSON(batch: GeometryBatch) {
  return toGeoJSON(batch)
}

/** GeoArrow IPC adapter; converter supplied by the @cogp/reader dependency.
 * Attribute conversion follows COGP: nested/mixed values become display strings.
 */
export function batchToGeoArrow(batch: GeometryBatch, options?: GeoArrowOptions): ArrayBuffer {
  return toGeoArrow(batch, options)
}

export type { GeometryColumn, GeoArrowOptions }

/** Convert the same buffers to a clipped MVT tile; default layer is `shapes`. */
export function batchToMvt(batch: GeometryBatch, options: MvtTileOptions): ArrayBuffer {
  return toMvt(batch, { layer: 'shapes', ...options })
}
export type { MvtTileOptions }
