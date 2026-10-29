import { MVT_BUFFER, MVT_EXTENT, toMvt } from './mvt.js'
import { geometryColumnFromWkb } from '@cogp/reader'
import { GeometryBuilder } from './geometry.js'
import type { Bbox, Lod } from './reader.js'
import { decodeTile, type PreparedTile, type TileReader } from './spatial.js'
import { HALF_WORLD } from './web-mercator.js'

const VECTOR_TILE_SIZE = 512

export interface TileOptions {
  /** MVT layer name for shape LODs (default `features`). */
  layer?: string
  /** MVT layer name for point LODs (default: same as `layer`). */
  pointLayer?: string
  /** Attribute names to copy into MVT properties (default: all carried attributes). */
  properties?: string[]
  signal?: AbortSignal
}

const tileYToLatitude = (y: number, tiles: number) =>
  (Math.atan(Math.sinh(Math.PI * (1 - (2 * y) / tiles))) * 180) / Math.PI

export const tileBounds = (z: number, x: number, y: number): Bbox => {
  const tiles = 2 ** z
  const padding = MVT_BUFFER / MVT_EXTENT
  return [
    ((x - padding) / tiles) * 360 - 180,
    tileYToLatitude(y + 1 + padding, tiles),
    ((x + 1 + padding) / tiles) * 360 - 180,
    tileYToLatitude(y - padding, tiles),
  ]
}

/** The buffered tile in EPSG:3857 metres, the CRS LOD coordinates are stored in. */
const mercatorTileBounds = (z: number, x: number, y: number): Bbox => {
  const cell = (2 * HALF_WORLD) / 2 ** z
  const padding = (MVT_BUFFER / MVT_EXTENT) * cell
  return [
    -HALF_WORLD + x * cell - padding,
    HALF_WORLD - (y + 1) * cell - padding,
    -HALF_WORLD + (x + 1) * cell + padding,
    HALF_WORLD - y * cell + padding,
  ]
}

/** One MVT tile from the LOD that serves zoom `z`. Feature ids are file row indexes. */
export const renderTile = async (
  reader: TileReader,
  z: number,
  x: number,
  y: number,
  options: TileOptions = {},
): Promise<ArrayBuffer> => {
  const { signal } = options
  const bbox = tileBounds(z, x, y)
  const names = options.properties ?? reader.lod.attributes.map((a) => a.name)
  const lod = reader.lodForZoom(z)
  const pointFamily = lod.geometry_type === 'Point'
  const layer = (pointFamily ? options.pointLayer : undefined) ?? options.layer ?? 'features'
  if (z > reader.finestZoom) {
    // finer than any LOD: the exact primary geometry of the rows in the tile
    const { rows, wkb, columns } = await reader.readPrimary(bbox, names, signal)
    signal?.throwIfAborted()
    return toMvt(
      { geometry: geometryColumnFromWkb(wkb), rowIndex: rows, columns },
      { z, x, y, layer, signal },
    )
  }
  if (reader.readGeometryByTile) {
    const data = await reader.readGeometryByTile(lod, z, x, y, names, signal)
    return renderGeometryTile(lod, data, z, x, y, options)
  }
  const { rows, coords, columns } = await reader.readLodByTile(lod, z, x, y, names, signal)
  const builder = new GeometryBuilder()
  for (let i = 0; i < rows.length; i++) builder.add(lod.geometry_type, coords[i])
  const geometry = builder.finish(lod.scale, lod.offset)
  return renderGeometryTile(lod, { rows, columns, geometry }, z, x, y, options)
}

export const tileResolution = (z: number, y: number) => {
  const latitude = tileYToLatitude(y + 0.5, 2 ** z)
  return (360 * Math.cos((latitude * Math.PI) / 180)) / (VECTOR_TILE_SIZE * 2 ** z)
}

export interface DecodedTile {
  lod: Lod
  z: number
  x: number
  y: number
  rows: number[]
  columns: Record<string, unknown[]>
  geometry: ReturnType<GeometryBuilder['finish']>
}

export function decodePreparedTile(task: PreparedTile): DecodedTile {
  const builder = new GeometryBuilder()
  const { rows, columns } = decodeTile(
    task,
    task.fields.map((f) => f.name),
    builder,
  )
  return normalizeTile(
    task.lod,
    { rows, columns, geometry: builder.finish(task.lod.scale, task.lod.offset) },
    task.z,
    task.x,
    task.y,
  )
}

export function renderDecodedTile(tile: DecodedTile, options: TileOptions = {}): ArrayBuffer {
  const { lod, rows, geometry, z, x, y } = tile
  const names = options.properties ?? Object.keys(tile.columns)
  const columns = Object.fromEntries(
    names.map((name) => {
      if (!(name in tile.columns)) throw new Error(`missing hydrated attribute ${name}`)
      return [name, tile.columns[name]]
    }),
  )
  const layer =
    (lod.geometry_type === 'Point' ? options.pointLayer : undefined) ?? options.layer ?? 'features'
  return toMvt(
    { geometry: geometry as never, rowIndex: rows, columns },
    { z, x, y, layer, signal: options.signal },
  )
}

function renderGeometryTile(
  lod: Lod,
  data: Awaited<ReturnType<NonNullable<TileReader['readGeometryByTile']>>>,
  z: number,
  x: number,
  y: number,
  options: TileOptions,
): ArrayBuffer {
  options.signal?.throwIfAborted()
  return renderDecodedTile(normalizeTile(lod, data, z, x, y), options)
}

/** Point filtering happens once before caching; rendering is read-only. */
function normalizeTile(
  lod: Lod,
  data: Pick<DecodedTile, 'rows' | 'columns' | 'geometry'>,
  z: number,
  x: number,
  y: number,
): DecodedTile {
  const bbox = mercatorTileBounds(z, x, y)
  const names = Object.keys(data.columns)
  const pointFamily = lod.geometry_type === 'Point'
  const { rows, columns } = data
  let { geometry } = data
  if (pointFamily) {
    // Keep points inside the buffered tile. Point offsets are the identity
    // sequence; compact coordinates and attributes in place.
    let count = 0
    for (let i = 0; i < rows.length; i++) {
      const mx = lod.offset[0] + lod.scale[0] * geometry.x[i]
      const my = lod.offset[1] + lod.scale[1] * geometry.y[i]
      if (mx < bbox[0] || mx > bbox[2] || my < bbox[1] || my > bbox[3]) continue
      geometry.x[count] = geometry.x[i]
      geometry.y[count] = geometry.y[i]
      rows[count] = rows[i]
      for (const name of names) columns[name][count] = columns[name][i]
      count++
    }
    rows.length = count
    for (const name of names) columns[name].length = count
    geometry = {
      ...geometry,
      length: count,
      x: geometry.x.subarray(0, count),
      y: geometry.y.subarray(0, count),
      types: geometry.types.subarray(0, count),
      geometryOffsets: geometry.geometryOffsets.subarray(0, count + 1),
      polygonOffsets: geometry.polygonOffsets.subarray(0, count + 1),
      ringOffsets: geometry.ringOffsets.subarray(0, count + 1),
    }
  }
  return { lod, z, x, y, rows, columns, geometry }
}
