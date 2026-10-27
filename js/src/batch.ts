import { geometryColumnFromWkb, toGeoArrow, toGeoJSON } from '@cogp/reader'
import type { GeometryColumn, GeoArrowOptions } from '@cogp/reader'
import { GeometryBuilder } from './geometry.js'
import { readFeatures, type FeatureQuery, type FeatureResult } from './query.js'
import { toMvt, type MvtTileOptions } from './mvt.js'
import type { TileReader } from './spatial.js'
import { LOD_CRS, latitudeOf, longitudeOf } from './web-mercator.js'

/** A geometry column whose `crs` marks EPSG:3857 LOD coordinates; absent, they are lon/lat. */
export type LodGeometryColumn = GeometryColumn & { readonly crs?: typeof LOD_CRS }

/** Renderer-neutral buffers; a coordinate is offset + scale * [x, y].
 * Overview buffers retain quantized int32 coordinates in EPSG:3857 metres, with
 * `geometry.crs` set; primary buffers are float64 longitude/latitude.
 * Offsets describe feature -> polygon -> ring/line -> coordinate ranges.
 */
export interface GeometryBatch {
  geometry: LodGeometryColumn
  rowIndex: number[]
  columns: Record<string, unknown[]>
  source: 'overview' | 'primary'
}

/** Normalize either readFeatures result without converting geometry to objects. */
export function toGeometryBatch(result: FeatureResult): GeometryBatch {
  if (Object.values(result.columns).some((column) => column.length !== result.rows.length))
    throw new Error('attribute column length does not match row IDs')
  let geometry: LodGeometryColumn
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

/** The batch with overview coordinates as float64 longitude/latitude. */
export function batchInDegrees(batch: GeometryBatch): GeometryBatch {
  const { crs, ...geometry } = batch.geometry
  if (crs !== LOD_CRS) return batch
  const { x, y, scale, offset } = geometry
  const lon = new Float64Array(x.length)
  const lat = new Float64Array(y.length)
  for (let i = 0; i < x.length; i++) {
    lon[i] = longitudeOf(offset[0] + scale[0] * x[i])
    lat[i] = latitudeOf(offset[1] + scale[1] * y[i])
  }
  return { ...batch, geometry: { ...geometry, x: lon, y: lat, scale: [1, 1], offset: [0, 0] } }
}

/** Small-data adapter in longitude/latitude; properties retain their JS types (including bigint). */
export function batchToGeoJSON(batch: GeometryBatch) {
  return toGeoJSON(batchInDegrees(batch))
}

/** GeoArrow IPC adapter in longitude/latitude; converter supplied by the @cogp/reader dependency.
 * Attribute conversion follows COGP: nested/mixed values become display strings.
 */
export function batchToGeoArrow(batch: GeometryBatch, options?: GeoArrowOptions): ArrayBuffer {
  return toGeoArrow(batchInDegrees(batch), options)
}

export type { GeometryColumn, GeoArrowOptions }

/** Convert the same buffers to a clipped MVT tile; default layer is `shapes`. */
export function batchToMvt(batch: GeometryBatch, options: MvtTileOptions): ArrayBuffer {
  return toMvt(batch, { layer: 'shapes', ...options })
}
export type { MvtTileOptions }
