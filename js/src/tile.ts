import { toMvt } from './mvt.js'
import { MVT_BUFFER, MVT_EXTENT, geometryColumnFromWkb, toMvt as primaryToMvt } from '@cogp/reader'
import { GeometryBuilder } from './geometry.js'
import type { Bbox, Lod } from './reader.js'
import { decodeTile, type PreparedTile, type TileReader } from './spatial.js'

const VECTOR_TILE_SIZE = 512

export interface TileOptions {
  /** Tile directory sidecar URL or instance; a missing or mismatched one falls back to page indexes. */
  /** MVT layer name for shape LODs (default `features`). */
  layer?: string
  /** MVT layer name for point LODs (default: same as `layer`). */
  pointLayer?: string
  /** Attribute names to copy into MVT properties (default: all carried attributes). */
  properties?: string[]
  /**
   * Read the bbox values of candidate pages and keep only rows that intersect
   * the tile before fetching their LOD pages (default true). Costs a few page
   * reads of the covering, saves most of the LOD bytes at fine zooms, where a
   * bbox page holds thousands of rows and a tile wants a few hundred.
   */
  refine?: boolean
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
    return primaryToMvt(
      { geometry: geometryColumnFromWkb(wkb), rowIndex: rows, columns },
      { z, x, y, layer, signal },
    )
  }
  if (reader.readGeometryByTile) {
    const data = await reader.readGeometryByTile(lod, z, x, y, names, signal)
    return renderGeometryTile(lod, data, z, x, y, options)
  }
  const { rows, coords, columns } = await reader.readLodByTile(lod, z, x, y, names, signal)
  if (pointFamily) {
    const px: number[] = []
    const py: number[] = []
    const keptRows: number[] = []
    const kept: Record<string, unknown[]> = Object.fromEntries(names.map((n) => [n, []]))
    for (let i = 0; i < rows.length; i++) {
      const [qx, qy] = coords[i] as [number, number]
      const lon = lod.offset[0] + lod.scale[0] * qx
      const lat = lod.offset[1] + lod.scale[1] * qy
      if (lon < bbox[0] || lon > bbox[2] || lat < bbox[1] || lat > bbox[3]) continue
      px.push(lon)
      py.push(lat)
      keptRows.push(rows[i])
      for (const n of names) kept[n].push(columns[n][i])
    }
    const n = px.length
    const offsets = Uint32Array.from({ length: n + 1 }, (_, i) => i)
    const geometry = {
      length: n,
      types: new Uint8Array(n).fill(1),
      x: Float64Array.from(px),
      y: Float64Array.from(py),
      geometryOffsets: offsets,
      polygonOffsets: offsets,
      ringOffsets: offsets,
      scale: [1, 1] as const,
      offset: [0, 0] as const,
    }
    return toMvt(
      { geometry: geometry as never, rowIndex: keptRows, columns: kept },
      { z, x, y, layer, signal },
    )
  }
  const builder = new GeometryBuilder()
  for (let i = 0; i < rows.length; i++) builder.add(lod.geometry_type, coords[i])
  const geometry = builder.finish(lod.scale, lod.offset)
  return toMvt({ geometry: geometry as never, rowIndex: rows, columns }, { z, x, y, layer, signal })
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

export function decodePreparedTile(
  task: PreparedTile,
  names = task.defaultAttributes ?? task.fields.map((f) => f.name),
  signal?: AbortSignal,
): DecodedTile {
  const builder = new GeometryBuilder()
  const { rows, columns } = decodeTile(task, names, builder, signal)
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

export function renderPreparedTile(task: PreparedTile, options: TileOptions = {}): ArrayBuffer {
  return renderDecodedTile(decodePreparedTile(task, options.properties, options.signal), options)
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
  const bbox = tileBounds(z, x, y)
  const names = Object.keys(data.columns)
  const pointFamily = lod.geometry_type === 'Point'
  const { rows, columns } = data
  let { geometry } = data
  if (pointFamily) {
    // Keep the existing lon/lat bbox rule before projection. Point offsets
    // are the identity sequence; compact coordinates and attributes in place.
    let count = 0
    for (let i = 0; i < rows.length; i++) {
      const lon = lod.offset[0] + lod.scale[0] * geometry.x[i]
      const lat = lod.offset[1] + lod.scale[1] * geometry.y[i]
      if (lon < bbox[0] || lon > bbox[2] || lat < bbox[1] || lat > bbox[3]) continue
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
