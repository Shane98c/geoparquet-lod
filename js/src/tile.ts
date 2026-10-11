import { MVT_BUFFER, MVT_EXTENT, geometryColumnFromWkb, toMvt } from '@cogp/reader'
import { GeometryBuilder } from './geometry.js'
import type { Bbox, LodReader, LodRows } from './reader.js'
import { openTileDirectory, TileDirectory } from './tiledir.js'

const VECTOR_TILE_SIZE = 512

export interface TileOptions {
  /** Experimental sidecar URL; unavailable or incompatible sidecars fall back to page indexes. */
  tiledir?: string | TileDirectory
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
  reader: LodReader,
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
  let located: LodRows | undefined
  if (options.tiledir) {
    try {
      const directory =
        typeof options.tiledir === 'string'
          ? await openTileDirectory(reader, options.tiledir)
          : options.tiledir
      if (directory.reader !== reader) throw new Error('tile directory belongs to another reader')
      located = await directory.readLodByTile(lod, z, x, y, names, signal)
    } catch (error) {
      signal?.throwIfAborted()
      console.warn('Experimental tile directory unavailable; using page indexes:', error)
    }
  }
  const { rows, coords, columns } =
    located ?? (await reader.readLod(lod, bbox, names, signal, { refine: options.refine ?? true }))
  signal?.throwIfAborted()
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
